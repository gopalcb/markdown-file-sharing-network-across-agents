"""Enforce administrator-approved native keys and one-use signed request nonces.

Signature parsing lives in validation_service. Approval is separate from enrollment;
this runtime never writes approval state and MAC addresses remain metadata only.
"""

from time import time

from botocore.exceptions import ClientError

REGISTRY_TABLE = 'softrobots-native-device-pending'
APPROVAL_TABLE = 'softrobots-native-device-approvals'
NONCE_TABLE = 'softrobots-native-device-nonces'


def authorize_native_device(database, proof: dict, register: bool = False) -> bool:
    """Consume a fresh proof and require approval, or record immutable enrollment."""
    approval = (
        database.Table(APPROVAL_TABLE)
        .get_item(Key={'approval_id': proof['approval_id']}, ConsistentRead=True)
        .get('Item')
    )
    approved = bool(approval and approval.get('status') == 'APPROVED')

    if approval and not approved:
        raise PermissionError('Device revoked')

    if not approved and not register:
        raise PermissionError('Device approval required')

    # TTL deletion is asynchronous. The condition never reuses a stored nonce;
    # retention also covers requests signed at the maximum future clock skew.
    try:
        database.Table(NONCE_TABLE).put_item(
            Item={
                'nonce_id': f'{proof["approval_id"]}#{proof["nonce"]}',
                'expires_at': max(int(time()), proof['timestamp']) + 180,
            },
            ConditionExpression='attribute_not_exists(nonce_id)',
        )
    except ClientError as error:
        if error.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise PermissionError('Request already used') from None
        raise

    if not approved:
        item = {
            key: proof[key]
            for key in (
                'owner_id',
                'issuer',
                'subject',
                'fingerprint',
                'public_key',
                'device_id',
                'device_mac',
            )
        }

        item.update(pending_id=f'PENDING#{proof["approval_id"]}', created_at=int(time()))

        try:
            database.Table(REGISTRY_TABLE).put_item(
                Item=item, ConditionExpression='attribute_not_exists(pending_id)'
            )
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
            existing = (
                database.Table(REGISTRY_TABLE)
                .get_item(Key={'pending_id': item['pending_id']}, ConsistentRead=True)
                .get('Item')
            )

            if not existing or any(
                existing.get(key) != item[key]
                for key in ('owner_id', 'issuer', 'subject', 'fingerprint', 'public_key')
            ):
                raise PermissionError('Enrollment conflict') from None

    return approved
