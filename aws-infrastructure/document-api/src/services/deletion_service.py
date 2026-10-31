"""Record immutable local deletion observations for future review, without S3 access."""

from datetime import datetime, timezone

from botocore.exceptions import ClientError


class DeletionConflict(ValueError):
    """An event ID already represents a different observation for this owner."""


def record_deletion(payload: dict, proof: dict, table) -> dict:
    """Conditionally insert one owner-bound observation; identical retries never reset review."""
    observation = {'username': payload['username'], **payload['deletion']}
    item = {
        **observation,
        'owner_id': proof['owner_id'],
        'issuer': proof['issuer'],
        'subject': proof['subject'],
        'review_status': 'PENDING_REVIEW',
        'reported_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }

    try:
        table.put_item(Item=item, ConditionExpression='attribute_not_exists(owner_id)')
    except ClientError as error:
        if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise

        existing = table.get_item(
            Key={'owner_id': proof['owner_id'], 'event_id': observation['event_id']},
            ConsistentRead=True,
        ).get('Item')

        if not existing or any(existing.get(key) != value for key, value in observation.items()):
            raise DeletionConflict(
                'Deletion event conflicts with an existing observation'
            ) from None

        # Optional fields may not disappear during a retry, either.
        optional = {'last_file_id', 'known_sha256', 'stored_sha256', 'uncertain_hash'}

        if any(key in existing and key not in observation for key in optional):
            raise DeletionConflict('Deletion event conflicts with an existing observation')

    return {
        'event_id': observation['event_id'],
        'project_id': observation['project_id'],
        'document_id': observation['document_id'],
        'status': 'RECORDED',
    }
