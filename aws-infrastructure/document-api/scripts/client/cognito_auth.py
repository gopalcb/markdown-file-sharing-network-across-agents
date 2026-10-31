"""Authenticate privately with transient access tokens and optional Keychain refresh persistence."""

import getpass
import importlib
import os
import stat
import sys
import time
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path


class LoginRequired(RuntimeError):
    """A human must start a new foreground session; automated work must not prompt."""


@dataclass(repr=False)
class CognitoSession:
    """Hold access/refresh tokens without persistence, repr exposure or automatic login."""

    client: object
    client_id: str
    username: str
    access_token: str
    refresh_token: str
    expires_at: float
    absolute_at: float = field(default_factory=lambda: time.monotonic() + 90 * 24 * 60 * 60)

    def invalidate(self) -> None:
        """Discard the in-memory session after rejection or an uncertain refresh."""
        self.access_token = ''
        self.refresh_token = ''
        self.expires_at = 0

    def credentials(self) -> tuple[str, str]:
        """Refresh before expiry with rotation support; never reuse an uncertain refresh."""

        if time.monotonic() >= self.absolute_at:
            self.invalidate()

        if not self.access_token:
            raise LoginRequired('Human login required')

        if time.monotonic() >= self.expires_at - 60:
            try:
                if not self.refresh_token:
                    raise LoginRequired('Human login required')
                result = self.client.get_tokens_from_refresh_token(
                    ClientId=self.client_id, RefreshToken=self.refresh_token
                )['AuthenticationResult']
                self.access_token = result['AccessToken']
                self.refresh_token = result.get('RefreshToken', self.refresh_token)
                self.expires_at = time.monotonic() + result['ExpiresIn']
            except Exception:
                # Refresh may have rotated remotely even if its response was lost.
                self.invalidate()
                raise LoginRequired('Human login required') from None

        return self.access_token, self.username


def email_otp_login(client, client_id: str, username: str) -> dict:
    """Complete one email challenge privately without fallback or automatic resend."""
    result = client.initiate_auth(
        AuthFlow='USER_AUTH',
        ClientId=client_id,
        AuthParameters={'USERNAME': username, 'PREFERRED_CHALLENGE': 'EMAIL_OTP'},
    )

    if result.get('ChallengeName') == 'SELECT_CHALLENGE':
        if 'EMAIL_OTP' not in result.get('AvailableChallenges', []):
            raise LoginRequired('Email sign-in is unavailable')

        username = result.get('ChallengeParameters', {}).get('USERNAME', username)
        result = client.respond_to_auth_challenge(
            ClientId=client_id,
            ChallengeName='SELECT_CHALLENGE',
            Session=result['Session'],
            ChallengeResponses={'USERNAME': username, 'ANSWER': 'EMAIL_OTP'},
        )

    if result.get('ChallengeName') != 'EMAIL_OTP' or not result.get('Session'):
        raise LoginRequired('Email sign-in is unavailable')

    username = result.get('ChallengeParameters', {}).get('USERNAME', username)
    code = getpass.getpass('Cognito email sign-in code: ')

    try:
        result = client.respond_to_auth_challenge(
            ClientId=client_id,
            ChallengeName='EMAIL_OTP',
            Session=result['Session'],
            ChallengeResponses={'USERNAME': username, 'EMAIL_OTP_CODE': code},
        )
    finally:
        code = None

    if result.get('ChallengeName') or 'AuthenticationResult' not in result:
        raise LoginRequired('Email sign-in did not complete')

    return result


def login(config: dict, username: str, client=None, remember: bool = False) -> CognitoSession:
    """Prompt privately for the selected email-code or legacy password sign-in flow."""

    if not sys.stdin.isatty() or not username:
        raise LoginRequired('Login requires a human terminal and username')

    method = config.get('cognito_auth_method', 'password')

    if method not in ('password', 'email_otp'):
        raise LoginRequired('Unsupported sign-in method')

    try:
        if client is None:
            boto3 = importlib.import_module('boto3')
            botocore = importlib.import_module('botocore')
            sdk_config = importlib.import_module('botocore.config')
            client = boto3.client(
                'cognito-idp',
                region_name=config['cognito_region'],
                config=sdk_config.Config(
                    signature_version=botocore.UNSIGNED,
                    retries={'total_max_attempts': 1},
                    connect_timeout=5,
                    read_timeout=30,
                    ignore_configured_endpoint_urls=True,
                ),
            )

        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            if method == 'email_otp':
                result = email_otp_login(client, config['cognito_client_id'], username)
            else:
                password = getpass.getpass('Cognito password: ')

                try:
                    result = client.initiate_auth(
                        AuthFlow='USER_PASSWORD_AUTH',
                        ClientId=config['cognito_client_id'],
                        AuthParameters={'USERNAME': username, 'PASSWORD': password},
                    )
                finally:
                    password = None
                challenge = result.get('ChallengeName')

                if challenge == 'NEW_PASSWORD_REQUIRED':
                    canonical = result.get('ChallengeParameters', {}).get('USERNAME', username)
                    new_password = getpass.getpass('New permanent Cognito password: ')
                    confirmation = getpass.getpass('Confirm new password: ')

                    try:
                        if new_password != confirmation:
                            raise LoginRequired('Passwords did not match')
                        result = client.respond_to_auth_challenge(
                            ClientId=config['cognito_client_id'],
                            ChallengeName=challenge,
                            Session=result['Session'],
                            ChallengeResponses={
                                'USERNAME': canonical,
                                'NEW_PASSWORD': new_password,
                            },
                        )
                    finally:
                        new_password = confirmation = None
                    challenge = result.get('ChallengeName')

                if challenge in {'SOFTWARE_TOKEN_MFA', 'SMS_MFA'}:
                    canonical = result.get('ChallengeParameters', {}).get('USERNAME', username)
                    code_field = (
                        'SOFTWARE_TOKEN_MFA_CODE'
                        if challenge == 'SOFTWARE_TOKEN_MFA'
                        else 'SMS_MFA_CODE'
                    )
                    code = getpass.getpass('Cognito MFA code: ')

                    try:
                        result = client.respond_to_auth_challenge(
                            ClientId=config['cognito_client_id'],
                            ChallengeName=challenge,
                            Session=result['Session'],
                            ChallengeResponses={'USERNAME': canonical, code_field: code},
                        )
                    finally:
                        code = None
            tokens = result['AuthenticationResult']

            # Device enrollment is deliberately not implemented; require a pool without remembering.
            if tokens.get('NewDeviceMetadata'):
                raise LoginRequired('Device remembering is not supported')
            identity = client.get_user(AccessToken=tokens['AccessToken'])
            session = CognitoSession(
                client,
                config['cognito_client_id'],
                identity['Username'],
                tokens['AccessToken'],
                tokens['RefreshToken'],
                time.monotonic() + tokens['ExpiresIn'],
            )

            if remember:
                from native_session import remember_session

                return remember_session(config, username, session)

            return session
    except Exception:
        # Never expose SDK response bodies, request parameters, secrets or chained tracebacks.
        raise LoginRequired(
            'Login failed; check dependency, account and MFA setup privately'
        ) from None


def device_headers() -> dict:
    """Read/create a private nonsecret installation ID and report a usable MAC if available."""
    directory = Path.home() / '.local' / 'state' / 'work-organizer'

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise ValueError('Unsafe device state directory')
    path = directory / 'device-id'

    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(str(uuid.uuid4()))

            stream.flush()

            os.fsync(stream.fileno())
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())

        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Unsafe device state file')
        device_id = stream.read(37)

    if str(uuid.UUID(device_id, version=4)) != device_id:
        raise ValueError('Invalid saved device ID')
    node = uuid.getnode()
    # uuid.getnode sets the multicast bit when it synthesizes a random fallback.
    mac = (
        ':'.join(f'{(node >> shift) & 255:02x}' for shift in range(40, -1, -8))
        if node and not (node >> 40) & 1
        else ''
    )

    return {'X-Device-Id': device_id, 'X-Device-Mac': mac}
