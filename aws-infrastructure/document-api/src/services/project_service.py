"""Store a shared portal project catalog in its dedicated retained DynamoDB table."""

import base64
import json
from datetime import datetime, timezone

from botocore.exceptions import ClientError

PROJECT_FIELDS = ('name', 'description', 'created_at', 'owner')


class ProjectExists(Exception):
    """A different create request already owns this case-insensitive project name."""


def project_summary(record: dict) -> dict:
    """Return only the public project contract, never internal table fields."""

    return {'id': record['project_id'], **{field: record[field] for field in PROJECT_FIELDS}}


def list_projects(payload: dict, table) -> dict:
    """Read one bounded shared catalog page with a caller-bound continuation cursor."""
    options = {
        'Limit': 50,
        'ConsistentRead': True,
        'ProjectionExpression': 'project_id, #name, description, created_at, #owner',
        'ExpressionAttributeNames': {'#name': 'name', '#owner': 'owner'},
    }

    if payload.get('start_key'):
        options['ExclusiveStartKey'] = payload['start_key']
    page = table.scan(**options)
    key = page.get('LastEvaluatedKey')
    cursor = (
        base64.urlsafe_b64encode(
            json.dumps(
                {'username': payload['username'], 'id': key['project_id']}, separators=(',', ':')
            ).encode()
        ).decode()
        if key
        else None
    )

    return {
        'projects': [project_summary(record) for record in page.get('Items', [])],
        'cursor': cursor,
    }


def create_project(payload: dict, table) -> tuple[dict, int]:
    """Atomically create a unique normalized name; identical owner retries are idempotent."""
    project = payload['project']
    record = {
        'project_id': project['id'],
        'name': project['name'],
        'description': project['description'],
        'created_at': datetime.now(timezone.utc).isoformat(),
        'owner': payload['username'],
    }

    try:
        table.put_item(Item=record, ConditionExpression='attribute_not_exists(project_id)')
    except ClientError as error:
        if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        existing = table.get_item(
            Key={'project_id': record['project_id']}, ConsistentRead=True
        ).get('Item')

        if (
            existing
            and existing.get('owner') == record['owner']
            and existing.get('description') == record['description']
        ):
            return {'project': project_summary(existing)}, 200
        raise ProjectExists() from None

    return {'project': project_summary(record)}, 201
