"""Bind verified Cognito refresh families to reported devices with a fixed deadline.

Call only after signature/issuer/client validation. Device IDs and MAC addresses
are client-reported metadata, not hardware attestation or substitute credentials.
"""

import json
import re
from hashlib import sha256
from time import time
from uuid import UUID

from botocore.exceptions import ClientError

ABSOLUTE_SECONDS = 90 * 24 * 60 * 60


def validate_session_age(authenticated_at: int, now: float) -> None:
    """Enforce the original 90-day boundary; transport adapters normalize types first."""

    if (
        type(authenticated_at) is not int
        or authenticated_at <= 0
        or authenticated_at > now + 10
        or now >= authenticated_at + ABSOLUTE_SECONDS
    ):
        raise ValueError('Original authentication is missing or outside the 90-day session limit')


def session_record(claims: dict, device_id: str, mac: str = '', native: bool = False) -> dict:
    """Validate reported metadata and original signed timestamps before any AWS call."""

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

    # API Gateway may expose numeric JWT claims as strings. Accept only decimal integers.
    if isinstance(auth_time, str) and re.fullmatch(r'[0-9]{1,12}', auth_time):
        auth_time = int(auth_time)

    try:
        validate_session_age(auth_time, time())
    except ValueError:
        raise PermissionError('Session expired') from None

    parts = [claims.get(key) for key in ('iss', 'sub', 'client_id', 'origin_jti')]

    if any(not isinstance(part, str) or not part or len(part) > 512 for part in parts):
        raise PermissionError('Session identity missing')

    return {
        'session_id': sha256(json.dumps(parts).encode()).hexdigest(),
        'device_id': device_id,
        'device_mac': mac,
        'device_type': 'native' if native else 'browser',
        'auth_time': auth_time,
        'expires_at': auth_time + ABSOLUTE_SECONDS,
    }


def check_device(table, record: dict, register: bool = False) -> None:
    """Atomically register once or require an existing identical, unexpired binding."""

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
