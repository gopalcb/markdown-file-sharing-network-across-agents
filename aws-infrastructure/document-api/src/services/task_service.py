"""Persist linked plan task metadata within the existing document index record."""

import base64
import json
from decimal import Decimal

from botocore.exceptions import ClientError

from services.document_read_service import get_document


class DocumentConflict(Exception):
    """The expected document or task revision is no longer current."""


def plan_task(document: dict, username: str, created_at: str) -> dict | None:
    """Recognize explicit plan metadata/frontmatter or a descriptive plan basename."""
    metadata = document.get('metadata', {})
    frontmatter = {}
    content = document['content']

    if content.startswith('---\n'):
        end = content.find('\n---', 4, 4096)

        if end != -1:
            for line in content[4:end].splitlines():
                key, separator, value = line.partition(':')

                if separator and key in {'kind', 'project', 'assigned_to', 'title'}:
                    frontmatter[key] = value.strip().strip('"\'')
    fields = {**frontmatter, **metadata}
    name = document['name'].lower()

    if fields.get('kind') != 'plan' and name != 'plan.md' and not name.endswith('-plan.md'):
        return None
    heading = next(
        (line[2:].strip() for line in content.splitlines() if line.startswith('# ')),
        document['name'][:-3],
    )

    def text_field(key: str, fallback: str, limit: int) -> str:
        """Use only a bounded nonempty metadata string for display fields."""
        value = fields.get(key)

        return (value.strip() if isinstance(value, str) and value.strip() else fallback)[:limit]

    return {
        'title': text_field('title', heading, 120),
        'project': text_field('project', 'Agent plans', 80),
        'assigned_to': text_field('assigned_to', username, 120),
        'status': 'pending',
        'priority': 'Medium',
        'due': '',
        'created_at': created_at,
        'revision': 1,
    }


def task_summary(record: dict, username: str) -> dict:
    """Expose only public task metadata and the verified ownership edit capability."""
    task = record['task']

    return {
        key: int(task[key]) if isinstance(task[key], Decimal) else task[key]
        for key in (
            'title',
            'project',
            'assigned_to',
            'status',
            'priority',
            'due',
            'created_at',
            'revision',
        )
    } | {
        'id': record['file_id'],
        'owner': record['username'],
        'editable': record['username'] == username,
    }


def list_tasks(payload: dict, table) -> dict:
    """Read one bounded index page; omit unfinished documents and unrelated records."""
    options = {
        'Limit': 50,
        'FilterExpression': '#status = :ready AND attribute_exists(task)',
        'ExpressionAttributeNames': {'#status': 'status'},
        'ExpressionAttributeValues': {':ready': 'READY'},
        'ProjectionExpression': 'username, file_id, task, #status',
    }

    if payload.get('start_key'):
        options['ExclusiveStartKey'] = payload['start_key']

    if payload['scope'] == 'mine':
        options['KeyConditionExpression'] = 'username = :username'
        options['ExpressionAttributeValues'][':username'] = payload['username']
        page = table.query(**options)
    else:
        page = table.scan(**options)
    tasks = [
        task_summary(record, payload['username'])
        for record in page.get('Items', [])
        if record.get('status') == 'READY'
        and isinstance(record.get('task'), dict)
        and (payload['scope'] == 'all' or record['username'] == payload['username'])
    ]
    key = page.get('LastEvaluatedKey')
    cursor = (
        base64.urlsafe_b64encode(
            json.dumps(
                {'scope': payload['scope'], 'username': payload['username'], 'key': key},
                separators=(',', ':'),
            ).encode()
        ).decode()
        if key
        else None
    )

    return {'tasks': tasks, 'cursor': cursor}


def get_task(payload: dict, table, s3, bucket: str, legacy_bucket: str | None) -> dict | None:
    """Read the linked plan Markdown unless the task has an independent edited description."""

    if payload['scope'] == 'mine' and payload['owner'] != payload['username']:
        return None
    record = table.get_item(
        Key={'username': payload['owner'], 'file_id': payload['file_id']}, ConsistentRead=True
    ).get('Item')

    if not record or record.get('status') != 'READY' or not isinstance(record.get('task'), dict):
        return None
    task = task_summary(record, payload['username'])
    description = record['task'].get('description')

    if description is None:
        document = get_document(payload, table, s3, bucket, legacy_bucket)

        if document is None:
            return None
        description = document['document']['content']

    return {'task': {**task, 'description': description}}


def update_task(payload: dict, table, s3, bucket: str, legacy_bucket: str | None) -> dict | None:
    """Conditionally edit task metadata without changing source Markdown or other fields."""
    key = {'username': payload['owner'], 'file_id': payload['file_id']}
    record = table.get_item(Key=key, ConsistentRead=True).get('Item')

    if not record or record.get('status') != 'READY' or not isinstance(record.get('task'), dict):
        return None
    changes = dict(payload['changes'])
    expected = changes.pop('expected_revision')

    if record['task']['revision'] != expected:
        raise DocumentConflict()
    task = {**record['task'], **changes, 'revision': expected + 1}

    try:
        table.update_item(
            Key=key,
            UpdateExpression='SET task = :task',
            ConditionExpression='#status = :ready AND task.revision = :expected',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={':task': task, ':ready': 'READY', ':expected': expected},
        )
    except ClientError as error:
        if error.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise DocumentConflict() from None
        raise

    return get_task(payload, table, s3, bucket, legacy_bucket)
