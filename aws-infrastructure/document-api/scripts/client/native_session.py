"""Resume native Cognito sessions from device-local Keychain without extending their deadline."""

import base64
import importlib
import json
import secrets
import time

from device_signing import credential_id, credential_lock, native

ABSOLUTE_SECONDS = 90 * 86400


def token_identity(token: str, client_id: str) -> dict:
    """Read claims only from Cognito TLS responses; the upload API verifies JWT signatures."""

    try:
        part = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        origin = claims['auth_time']

        if (
            claims['client_id'] != client_id
            or claims['token_use'] != 'access'
            or isinstance(origin, bool)
            or not isinstance(origin, int)
            or origin <= 0
            or origin > time.time() + 10
            or time.time() >= origin + ABSOLUTE_SECONDS
            or not all(
                isinstance(claims.get(key), str) and claims[key]
                for key in ('iss', 'sub', 'origin_jti')
            )
        ):
            raise ValueError()

        return {key: claims[key] for key in ('iss', 'sub', 'client_id', 'auth_time', 'origin_jti')}
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError('Cognito returned an invalid session identity') from None


def cognito_client(config: dict):
    """Use the public regional AWS endpoint without ambient credential/endpoint overrides."""
    boto3 = importlib.import_module('boto3')
    botocore = importlib.import_module('botocore')
    sdk = importlib.import_module('botocore.config')

    return boto3.client(
        'cognito-idp',
        region_name=config['cognito_region'],
        config=sdk.Config(
            signature_version=botocore.UNSIGNED,
            retries={'total_max_attempts': 1},
            connect_timeout=5,
            read_timeout=30,
            ignore_configured_endpoint_urls=True,
        ),
    )


class PersistentSession:
    """Coordinate shared credentials without writing them to project files or process arguments."""

    def __init__(self, config: dict, username: str, session):
        """Bind a process session to one explicitly selected Keychain account."""
        self.account = credential_id(config, username)
        self.session = session
        self.generation = None

    def credentials(self) -> tuple[str, str]:
        """Check durable logout state and serialize refresh without extending auth_time."""
        from cognito_auth import LoginRequired

        with credential_lock(self.account):
            # A temporary locked Keychain is not proof of invalid credentials.
            try:
                record = native('load', account=self.account)['value']
            except Exception:
                self.session.invalidate()
                raise LoginRequired('Unlock this Mac to resume the saved login') from None

            if not record:
                self.session.invalidate()
                raise LoginRequired('Human login required')

            if self.generation is not None and record['generation'] != self.generation:
                self.session.invalidate()
                raise LoginRequired('Another login replaced this session; restart the watcher')
            self.generation = record['generation']
            expected = record['identity']

            if time.time() >= expected['auth_time'] + ABSOLUTE_SECONDS:
                self.forget_rejected_session(record)
                raise LoginRequired('Human login required')
            # Check Keychain on every call so another project's logout is respected.
            self.session.refresh_token = record['refresh_token']
            self.session.absolute_at = time.monotonic() + max(
                0, expected['auth_time'] + ABSOLUTE_SECONDS - time.time()
            )

            if (
                self.session.access_token
                and token_identity(self.session.access_token, self.session.client_id) != expected
            ):
                self.session.invalidate()

            if not self.session.access_token or time.monotonic() >= self.session.expires_at - 60:
                try:
                    result = self.session.client.get_tokens_from_refresh_token(
                        ClientId=self.session.client_id, RefreshToken=record['refresh_token']
                    )['AuthenticationResult']
                    token = result['AccessToken']

                    if token_identity(token, self.session.client_id) != expected:
                        raise LoginRequired('Session identity changed')
                    record['refresh_token'] = result.get('RefreshToken', record['refresh_token'])

                    # Commit rotation before exposing access; uncertainty requires fresh login.
                    native('save', account=self.account, value=record)
                    self.session.access_token = token
                    self.session.refresh_token = record['refresh_token']
                    self.session.expires_at = time.monotonic() + result['ExpiresIn']
                except Exception:
                    self.forget_rejected_session(record)
                    raise LoginRequired(
                        'Refresh outcome unknown; authenticate privately again'
                    ) from None

            return self.session.access_token, record['username']

    def forget_rejected_session(self, record) -> None:
        """Delete only the matching generation while the caller holds its credential lock."""

        self.session.invalidate()

        if record and record.get('generation') == self.generation:
            native('delete', account=self.account)

    def invalidate(self) -> None:
        """Forget only this rejected login, never a newer login by another project."""

        with credential_lock(self.account):
            record = native('load', account=self.account)['value']

            self.forget_rejected_session(record)

    def close(self) -> None:
        """Release process memory while retaining the valid saved sign-in on normal stop."""

        self.session.invalidate()


def remember_session(config: dict, username: str, session) -> PersistentSession:
    """Save only after a human login and require hardware key creation without software fallback."""
    identity = token_identity(session.access_token, session.client_id)

    with credential_lock('0' * 64):
        native('enroll')
    wrapper = PersistentSession(config, username, session)
    record = {
        'generation': secrets.token_hex(16),
        'identity': identity,
        'username': session.username,
        'refresh_token': session.refresh_token,
    }

    with credential_lock(wrapper.account):
        native('save', account=wrapper.account, value=record)
        wrapper.generation = record['generation']

    return wrapper


def resume_session(config: dict, username: str, client=None):
    """Read only the explicitly selected account; background processes never prompt."""
    from cognito_auth import CognitoSession, LoginRequired

    if not username:
        raise LoginRequired('Select an account before resuming uploads')
    session = CognitoSession(
        client or cognito_client(config), config['cognito_client_id'], username, '', '', 0
    )
    result = PersistentSession(config, username, session)

    result.credentials()

    return result


def logout_saved(config: dict, username: str, client=None) -> None:
    """Delete locally before remote revocation; report failure rather than claim full logout."""
    account = credential_id(config, username)

    with credential_lock(account):
        record = native('load', account=account)['value']

        native('delete', account=account)

        if record:
            (client or cognito_client(config)).revoke_token(
                ClientId=config['cognito_client_id'], Token=record['refresh_token']
            )
