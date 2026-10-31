"""Real synthetic P256 proofs and stateful DynamoDB doubles for offline boundary tests."""

import base64
import json
from hashlib import sha256
from time import time
from unittest.mock import Mock
from uuid import uuid4

from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

KEY = ec.generate_private_key(ec.SECP256R1())


def sign_event(event: dict, key=KEY) -> dict:
    """Sign actual body and token bytes independently of production canonicalization."""
    headers = event['headers']
    headers['x-device-timestamp'] = str(int(time()))
    headers['x-device-nonce'] = str(uuid4())
    headers['x-device-key'] = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode()
    raw = (
        base64.b64decode(event['body']) if event.get('isBase64Encoded') else event['body'].encode()
    )
    message = '\n'.join(
        [
            'WO-DEVICE-V1',
            'POST',
            event['rawPath'],
            headers['x-device-timestamp'],
            headers['x-device-nonce'],
            sha256(raw).hexdigest(),
            sha256(headers['authorization'][7:].encode()).hexdigest(),
            headers['x-device-id'],
            headers['x-device-mac'],
        ]
    ).encode()
    headers['x-device-signature'] = base64.b64encode(
        key.sign(message, ec.ECDSA(hashes.SHA256()))
    ).decode()

    return event


def approval_for_event(event: dict, status: str = 'APPROVED') -> dict:
    """Construct administrator state for one independently derived owner/key identity."""
    claims = event['requestContext']['authorizer']['jwt']['claims']
    owner = sha256(
        json.dumps([claims['iss'], claims['sub']], separators=(',', ':')).encode()
    ).hexdigest()
    fingerprint = sha256(base64.b64decode(event['headers']['x-device-key'])).hexdigest()

    return {'approval_id': f'{owner}#{fingerprint}', 'status': status}


def fixture_database() -> tuple:
    """Model atomic insert-if-absent and consistent reads using per-table dictionaries."""
    rows = {}
    tables = {}

    def table_for(name):
        """Return one stable mock table with bounded key semantics."""

        if name not in tables:
            contents = rows.setdefault(name, {})
            table = Mock()

            def put_item(Item, ConditionExpression=None):
                """Reject a duplicate conditional key as DynamoDB does."""
                key = (
                    (Item['owner_id'], Item['event_id'])
                    if 'event_id' in Item
                    else next(
                        Item[field]
                        for field in (
                            'approval_id',
                            'pending_id',
                            'nonce_id',
                            'session_id',
                            'file_id',
                        )
                        if field in Item
                    )
                )

                if key in contents and ConditionExpression:
                    raise ClientError(
                        {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem'
                    )
                contents[key] = dict(Item)

                return {}

            def get_item(Key, ConsistentRead):
                """Look up the exact requested identity."""
                assert ConsistentRead is True
                key = (
                    (Key['owner_id'], Key['event_id'])
                    if 'event_id' in Key
                    else next(iter(Key.values()))
                )
                item = contents.get(key)

                return {'Item': dict(item)} if item else {}

            table.put_item.side_effect = put_item
            table.get_item.side_effect = get_item
            tables[name] = table

        return tables[name]

    database = Mock()
    database.Table.side_effect = table_for

    return database, rows
