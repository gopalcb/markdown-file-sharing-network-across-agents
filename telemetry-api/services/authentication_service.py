"""Own portal authentication, browser sessions, and fixed device bindings."""

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from http.client import HTTPException, HTTPSConnection
from threading import Lock
from time import monotonic, time
from urllib.parse import urlsplit
from uuid import UUID

import jwt
from botocore.exceptions import ClientError
from jwt import InvalidTokenError

ABSOLUTE_SECONDS = 90 * 24 * 60 * 60
CACHE_SECONDS = 300
REFRESH_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 2
MAX_JWKS_BYTES = 65536
MAX_JWKS_KEYS = 16
CLOCK_TOLERANCE_SECONDS = 10
COOKIE_NAME = '__Host-wo-session'
TABLE_NAME = 'softrobots-browser-sessions'
TOKEN_PATTERN = re.compile(r'[A-Za-z0-9_-]{43}\Z')


@dataclass(frozen=True)
class BrowserSessionService:
    """Bundle the clients and verified config shared by browser-session operations."""

    table: object
    cognito: object
    config: dict
    keys: object


class AuthenticationError(Exception):
    """A bearer token could not be safely authenticated."""


class AuthorizationError(Exception):
    """A verified caller does not belong to the required application group."""


def validate_session_age(authenticated_at: int, now: float) -> None:
    """Enforce the original 90-day session deadline."""

    if (
        type(authenticated_at) is not int
        or authenticated_at <= 0
        or authenticated_at > now + 10
        or now >= authenticated_at + ABSOLUTE_SECONDS
    ):
        raise ValueError('Original authentication is missing or outside the 90-day session limit')


def validate_auth_config(values: dict) -> dict:
    """Require an exact Cognito issuer and client; never enable an open mode."""
    issuer = values.get('COGNITO_ISSUER', '')
    client_id = values.get('COGNITO_CLIENT_ID', '')
    group = values.get('COGNITO_GROUP', 'portal-users')
    audience = values.get('COGNITO_AUDIENCE') or None
    match = (
        re.fullmatch(
            r'https://cognito-idp\.([a-z]{2}(?:-[a-z]+)+-\d)\.amazonaws\.com(?:\.cn)?/'
            r'([a-z]{2}(?:-[a-z]+)+-\d)_[A-Za-z0-9]+',
            issuer,
        )
        if isinstance(issuer, str)
        else None
    )

    if not match or match[1] != match[2]:
        raise ValueError('COGNITO_ISSUER must be an exact HTTPS Cognito user-pool issuer')

    if not isinstance(client_id, str) or not re.fullmatch(r'[a-z0-9]{1,128}', client_id):
        raise ValueError('COGNITO_CLIENT_ID must be a Cognito app client ID')

    if (
        not isinstance(group, str)
        or not group.strip()
        or group != group.strip()
        or len(group) > 128
    ):
        raise ValueError('COGNITO_GROUP must be a nonempty group name')

    if audience is not None and (
        not isinstance(audience, str) or not audience.strip() or audience != audience.strip()
    ):
        raise ValueError('COGNITO_AUDIENCE must be a nonempty audience when provided')

    return {
        'issuer': issuer,
        'client_id': client_id,
        'group': group,
        'audience': audience,
    }


def validate_access_claims(claims: dict, config: dict) -> None:
    """Reject claims outside the client, token-use, and fixed-session contract."""

    if claims.get('token_use') != 'access' or claims.get('client_id') != config['client_id']:
        raise InvalidTokenError('Wrong token use or client')

    if not isinstance(claims.get('sub'), str) or not claims['sub'].strip():
        raise InvalidTokenError('Missing subject')

    try:
        validate_session_age(claims.get('auth_time'), time())
    except ValueError as error:
        raise InvalidTokenError(str(error)) from None

    if any(type(claims[name]) is not int for name in ('exp', 'iat', 'nbf') if name in claims):
        raise InvalidTokenError('Invalid token timestamp')
    groups = claims.get('cognito:groups', [])

    if not isinstance(groups, list) or any(not isinstance(group, str) for group in groups):
        raise InvalidTokenError('Invalid groups')


def fetch_jwks(url: str) -> dict:
    """Fetch a bounded HTTPS key document without redirects or ambient proxy settings."""
    target = urlsplit(url)
    connection = HTTPSConnection(target.hostname, timeout=FETCH_TIMEOUT_SECONDS)

    try:
        connection.request('GET', target.path, headers={'Accept': 'application/json'})
        response = connection.getresponse()

        if response.status != 200:
            raise ValueError('Signing-key endpoint unavailable')
        body = response.read(MAX_JWKS_BYTES + 1)

        if len(body) > MAX_JWKS_BYTES:
            raise ValueError('Signing-key document too large')

        return json.loads(body)
    finally:
        connection.close()


class JwksCache:
    """Serialize refreshes and bound key rotation, negative lookups and outage retries."""

    def __init__(self, issuer: str, fetch=fetch_jwks, clock=monotonic) -> None:
        """Keep one cache per app worker; tests may supply a signed-fixture key source."""
        self.url = issuer + '/.well-known/jwks.json'
        self.fetch = fetch
        self.clock = clock
        self.keys = {}
        self.expires_at = 0.0
        self.retry_at = 0.0
        self.lock = Lock()

    def get_key(self, kid: str) -> jwt.PyJWK:
        """Return a fresh key or refresh once per cooldown; expired keys never bypass outages."""

        with self.lock:
            now = self.clock()

            if now < self.expires_at and kid in self.keys:
                return self.keys[kid]

            if now < self.retry_at:
                raise AuthenticationError('Signing key unavailable')
            self.retry_at = now + REFRESH_SECONDS

            try:
                document = self.fetch(self.url)
                entries = document.get('keys') if isinstance(document, dict) else None

                if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_JWKS_KEYS:
                    raise ValueError('Invalid signing-key document')
                keys = {}

                for entry in entries:
                    if (
                        not isinstance(entry, dict)
                        or entry.get('kty') != 'RSA'
                        or entry.get('use') != 'sig'
                        or entry.get('alg') != 'RS256'
                        or not isinstance(entry.get('kid'), str)
                        or not entry['kid']
                        or len(entry['kid']) > 256
                        or entry['kid'] in keys
                    ):
                        raise ValueError('Invalid signing key')
                    key = jwt.PyJWK.from_dict(entry, algorithm='RS256')

                    if key.key.key_size < 2048:
                        raise ValueError('Signing key too small')
                    keys[entry['kid']] = key
                self.keys = keys
                self.expires_at = self.clock() + CACHE_SECONDS
            except (
                OSError,
                HTTPException,
                ValueError,
                TypeError,
                KeyError,
                RecursionError,
                jwt.PyJWTError,
            ) as error:
                raise AuthenticationError('Signing key unavailable') from error

            if kid not in self.keys:
                raise AuthenticationError('Unknown signing key')

            return self.keys[kid]


def verify_access_token(authorization: str, config: dict, keys: JwksCache) -> dict:
    """Authenticate an RS256 access token, then authorize its signed group membership."""
    parts = authorization.split()

    if len(parts) != 2 or parts[0].lower() != 'bearer' or len(parts[1]) > 16384:
        raise AuthenticationError('Missing or malformed bearer token')
    token = parts[1]

    try:
        header = jwt.get_unverified_header(token)
        kid = header.get('kid')

        if (
            header.get('alg') != 'RS256'
            or not isinstance(kid, str)
            or not kid
            or len(kid) > 256
            or header.get('crit')
        ):
            raise jwt.InvalidTokenError('Unsupported token header')
        key = keys.get_key(kid)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=['RS256'],
            issuer=config['issuer'],
            audience=config['audience'],
            leeway=CLOCK_TOLERANCE_SECONDS,
            options={
                'require': [
                    'iss',
                    'sub',
                    'exp',
                    'iat',
                    'client_id',
                    'token_use',
                    'auth_time',
                    'origin_jti',
                ],
                'verify_aud': config['audience'] is not None,
            },
        )

        validate_access_claims(claims, config)
    except (
        jwt.PyJWTError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise AuthenticationError('Invalid bearer token') from error
    groups = claims.get('cognito:groups', [])

    if config['group'] not in groups:
        raise AuthorizationError('Group membership required')

    return {
        'issuer': claims['iss'],
        'sub': claims['sub'],
        'groups': groups,
        '_claims': claims,
    }


def session_record(claims: dict, device_id: str, mac: str = '', native: bool = False) -> dict:
    """Validate reported device metadata and bind it to the signed refresh family."""

    try:
        if str(UUID(device_id, version=4)) != device_id:
            raise ValueError('Invalid device ID')
    except (ValueError, TypeError, AttributeError):
        raise PermissionError('Invalid device ID') from None

    if not isinstance(mac, str) or (
        mac and not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac)
    ):
        raise PermissionError('Invalid device MAC')

    if mac and (int(mac[:2], 16) & 1 or mac == '00:00:00:00:00:00'):
        raise PermissionError('Invalid device MAC')

    if not native and mac:
        raise PermissionError('Browser device must not claim a MAC')
    auth_time = claims.get('auth_time')

    # API Gateway may expose numeric JWT claims as decimal strings.
    if isinstance(auth_time, str) and re.fullmatch(r'[0-9]{1,12}', auth_time):
        auth_time = int(auth_time)

    try:
        validate_session_age(auth_time, time())
    except ValueError:
        raise PermissionError('Session expired') from None

    identity = [claims.get(key) for key in ('iss', 'sub', 'client_id', 'origin_jti')]

    if any(not isinstance(part, str) or not part or len(part) > 512 for part in identity):
        raise PermissionError('Session identity missing')

    return {
        'session_id': hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
        'device_id': device_id,
        'device_mac': mac,
        'device_type': 'native' if native else 'browser',
        'auth_time': auth_time,
        'expires_at': auth_time + ABSOLUTE_SECONDS,
    }


def check_device(table, record: dict, register: bool = False) -> None:
    """Atomically register once or require an identical, unexpired binding."""

    if register:
        try:
            table.put_item(Item=record, ConditionExpression='attribute_not_exists(session_id)')

            return
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
    stored = table.get_item(Key={'session_id': record['session_id']}, ConsistentRead=True).get(
        'Item'
    )

    if not stored or any(stored.get(key) != value for key, value in record.items()):
        raise PermissionError('Device association required or mismatched')


def session_key(cookie: str) -> str:
    """Hash an unguessable cookie before addressing its database record."""

    if not isinstance(cookie, str) or not TOKEN_PATTERN.fullmatch(cookie):
        raise AuthenticationError('Invalid browser session')

    return hashlib.sha256(cookie.encode()).hexdigest()


def verify_session_identity(service: BrowserSessionService, token: str, expected: dict) -> dict:
    """Verify every refreshed token and preserve the authenticated identity."""
    identity = verify_access_token('Bearer ' + token, service.config, service.keys)
    claims = identity['_claims']

    if any(
        claims.get(name) != expected[name]
        for name in ('iss', 'sub', 'client_id', 'auth_time', 'origin_jti')
    ):
        raise AuthenticationError('Refreshed identity changed')

    return identity


def create_browser_session(
    service: BrowserSessionService, identity: dict, payload: dict, device_id: str
) -> tuple:
    """Exchange a refresh credential before storing a persistent session."""

    if (
        not isinstance(payload, dict)
        or set(payload) != {'refresh_token'}
        or not isinstance(payload['refresh_token'], str)
        or not 1 <= len(payload['refresh_token']) <= 16384
    ):
        raise ValueError('Invalid session bootstrap')

    claims = identity['_claims']
    device = session_record(claims, device_id)
    result = service.cognito.initiate_auth(
        ClientId=service.config['client_id'],
        AuthFlow='REFRESH_TOKEN_AUTH',
        AuthParameters={'REFRESH_TOKEN': payload['refresh_token']},
    )
    token = result['AuthenticationResult']['AccessToken']
    fresh = verify_session_identity(service, token, claims)
    cookie = secrets.token_urlsafe(32)
    record = {
        'session_id': session_key(cookie),
        'version': secrets.token_hex(16),
        'csrf': secrets.token_urlsafe(32),
        'device_id': device_id,
        'expires_at': device['expires_at'],
        'access_token': token,
        'access_expires_at': fresh['_claims']['exp'],
        'refresh_token': payload['refresh_token'],
        'identity': {
            name: claims[name] for name in ('iss', 'sub', 'client_id', 'auth_time', 'origin_jti')
        },
    }

    service.table.put_item(Item=record, ConditionExpression='attribute_not_exists(session_id)')

    return cookie, record, fresh


def read_browser_session(service: BrowserSessionService, cookie: str, device_id: str) -> dict:
    """Reject deleted, revoked, expired, or different-device cookies."""
    record = service.table.get_item(
        Key={'session_id': session_key(cookie)}, ConsistentRead=True
    ).get('Item')

    if (
        not record
        or record.get('revoked')
        or record.get('device_id') != device_id
        or not isinstance(record.get('identity'), dict)
        or record.get('expires_at') != record['identity'].get('auth_time', 0) + ABSOLUTE_SECONDS
        or time() >= record['expires_at']
    ):
        raise AuthenticationError('Browser session expired or revoked')

    return record


def authenticate_browser_session(
    service: BrowserSessionService, cookie: str, device_id: str
) -> tuple:
    """Refresh short tokens without extending the original session deadline."""
    record = read_browser_session(service, cookie, device_id)

    if time() + 30 >= record['access_expires_at']:
        result = service.cognito.initiate_auth(
            ClientId=service.config['client_id'],
            AuthFlow='REFRESH_TOKEN_AUTH',
            AuthParameters={'REFRESH_TOKEN': record['refresh_token']},
        )
        token = result['AuthenticationResult']['AccessToken']
        fresh = verify_session_identity(service, token, record['identity'])
        updated = {
            **record,
            'version': secrets.token_hex(16),
            'access_token': token,
            'access_expires_at': fresh['_claims']['exp'],
        }

        try:
            service.table.put_item(
                Item=updated,
                ConditionExpression='#version = :version',
                ExpressionAttributeNames={'#version': 'version'},
                ExpressionAttributeValues={':version': record['version']},
            )
            record = updated
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
            # Another refresh won, or logout removed the credentials.
            record = read_browser_session(service, cookie, device_id)

    identity = verify_session_identity(service, record['access_token'], record['identity'])

    return identity, record


def validate_cookie_mutation(origin: str, configured_origin: str, csrf: str, record: dict) -> None:
    """Require exact origin and CSRF values on cookie-authenticated writes."""

    if (
        origin != configured_origin
        or not isinstance(csrf, str)
        or not hmac.compare_digest(csrf, record['csrf'])
    ):
        raise PermissionError('Invalid session request origin or CSRF')


def revoke_browser_session(service: BrowserSessionService, cookie: str, device_id: str) -> None:
    """Persist a credential-free tombstone before best-effort token revocation."""
    record = read_browser_session(service, cookie, device_id)
    tombstone = {
        'session_id': record['session_id'],
        'version': secrets.token_hex(16),
        'revoked': True,
        'expires_at': record['expires_at'],
    }

    service.table.put_item(Item=tombstone)

    try:
        service.cognito.revoke_token(
            ClientId=service.config['client_id'], Token=record['refresh_token']
        )
    except ClientError:
        # The server session is already invalid and no token reaches the browser.
        pass


def browser_session_response(identity: dict, record: dict) -> dict:
    """Return only a safe identity, CSRF value, and fixed expiration."""
    claims = identity['_claims']
    name = claims.get('username', 'Portal account')

    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 160
        or any(ord(char) < 32 for char in name)
    ):
        name = 'Portal account'

    return {
        'issuer': identity['issuer'],
        'sub': identity['sub'],
        'groups': identity['groups'],
        'displayName': name,
        'expiresAt': int(record['expires_at']),
        'csrfToken': record['csrf'],
    }
