"""Own DynamoDB metadata, task, project, device, and deletion persistence."""

import base64
import json
from datetime import datetime, timezone
from decimal import Decimal
from time import time
from urllib.parse import unquote, urlsplit

from botocore.exceptions import ClientError

from document_api.validation import SEGMENT

PAGE_LIMIT = 50
PUBLIC_FIELDS = {
    'username',
    'file_id',
    's3_path',
    'file-group',
    'created_at',
    'status',
    'task',
}
PROJECT_FIELDS = ('name', 'description', 'created_at', 'owner')
TASK_FIELDS = (
    'title',
    'project',
    'assigned_to',
    'status',
    'priority',
    'due',
    'created_at',
    'revision',
)
REGISTRY_TABLE = 'softrobots-native-device-pending'
APPROVAL_TABLE = 'softrobots-native-device-approvals'
NONCE_TABLE = 'softrobots-native-device-nonces'


class DocumentConflict(Exception):
    """The expected document or task revision is no longer current."""


class ProjectExists(Exception):
    """A different request already owns a normalized project name."""


class DeletionConflict(ValueError):
    """An event ID already represents another deletion observation."""


def encode_cursor(payload: dict, key: dict | None) -> str | None:
    """Encode a bounded caller-bound continuation key."""

    if not key:
        return None

    return base64.urlsafe_b64encode(
        json.dumps({**payload, 'key': key}, separators=(',', ':')).encode()
    ).decode()


def get_ready_record(payload: dict, table, require_task: bool = False) -> dict | None:
    """Read one authorized READY index row."""

    if payload.get('scope') == 'mine' and payload['owner'] != payload['username']:
        return None
    record = table.get_item(
        Key={'username': payload['owner'], 'file_id': payload['file_id']},
        ConsistentRead=True,
    ).get('Item')

    if not record or record.get('status') != 'READY':
        return None

    if require_task and not isinstance(record.get('task'), dict):
        return None

    return record


def publish_record(table, record: dict, previous: dict | None = None) -> dict:
    """Publish acknowledged metadata while preserving task edits and creation time."""
    fields = {
        name: value
        for name, value in record.items()
        if name in PUBLIC_FIELDS - {'username', 'file_id'}
    }
    names, values, assignments = {}, {}, []

    for index, (name, value) in enumerate(fields.items()):
        alias, parameter = f'#f{index}', f':f{index}'
        names[alias], values[parameter] = name, value
        assignment = (
            f'if_not_exists({alias}, {parameter})' if name in {'task', 'created_at'} else parameter
        )

        assignments.append(f'{alias} = {assignment}')
    removals = []

    for index, name in enumerate(sorted(set(previous or {}) - PUBLIC_FIELDS)):
        alias = f'#old{index}'
        names[alias] = name

        removals.append(alias)
    expression = 'SET ' + ', '.join(assignments)

    if removals:
        expression += ' REMOVE ' + ', '.join(removals)

    return table.update_item(
        Key={'username': record['username'], 'file_id': record['file_id']},
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues='ALL_NEW',
    )['Attributes']


def document_summary(record: dict) -> dict:
    """Expose document metadata without storage internals."""

    if 's3_path' in record:
        filename = unquote(urlsplit(record['s3_path']).path).rsplit('/', 1)[-1]
        category = record['file-group']
        size = None
    else:
        filename = record['filename']
        category = record['category']
        size = int(record['size_bytes'])

    return {
        'owner': record['username'],
        'file_id': record['file_id'],
        'filename': filename,
        'category': category,
        'created_at': record['created_at'],
        'status': record['status'],
        'size_bytes': size,
    }


def list_documents(payload: dict, table) -> dict:
    """Return one bounded READY document page."""
    options = {
        'Limit': PAGE_LIMIT,
        'FilterExpression': '#status = :ready',
        'ProjectionExpression': (
            'username, file_id, filename, category, created_at, size_bytes, '
            '#status, s3_path, #group'
        ),
        'ExpressionAttributeNames': {'#status': 'status', '#group': 'file-group'},
        'ExpressionAttributeValues': {':ready': 'READY'},
    }

    if payload.get('start_key'):
        options['ExclusiveStartKey'] = payload['start_key']

    if payload['scope'] == 'mine':
        options['KeyConditionExpression'] = 'username = :username'
        options['ExpressionAttributeValues'][':username'] = payload['username']
        page = table.query(**options)
    else:
        page = table.scan(**options)

    documents = [
        document_summary(record)
        for record in page.get('Items', [])
        if record.get('status') == 'READY'
        and (payload['scope'] == 'all' or record.get('username') == payload['username'])
    ]

    return {
        'documents': documents,
        'cursor': encode_cursor(
            {'scope': payload['scope'], 'username': payload['username']},
            page.get('LastEvaluatedKey'),
        ),
    }


def list_folders(payload: dict, table) -> dict:
    """Return owner-scoped document groups without listing S3."""
    options = {
        'KeyConditionExpression': 'username = :username',
        'ExpressionAttributeValues': {':username': payload['username']},
        'ProjectionExpression': 'category, #group',
        'ExpressionAttributeNames': {'#group': 'file-group'},
        'Limit': 100,
    }

    if payload.get('start_key'):
        options['ExclusiveStartKey'] = payload['start_key']
    page = table.query(**options)
    groups = {'workflow-docs', 'worksummary', 'knowledge'}

    groups.update(
        group
        for record in page.get('Items', [])
        if isinstance((group := record.get('file-group', record.get('category'))), str)
        and SEGMENT.fullmatch(group)
    )

    return {
        'owner': payload['username'],
        'folders': [
            {
                'name': group,
                'prefix': f'agent-work/raw-markdown/{payload["username"]}/{group}/',
            }
            for group in sorted(groups)
        ],
        'cursor': encode_cursor(
            {'scope': 'mine', 'username': payload['username']},
            page.get('LastEvaluatedKey'),
        ),
    }


def plan_task(document: dict, username: str, created_at: str) -> dict | None:
    """Build linked task metadata when Markdown represents a plan."""
    metadata, frontmatter = document.get('metadata', {}), {}
    content = document['content']

    if content.startswith('---\n') and (end := content.find('\n---', 4, 4096)) != -1:
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

    def text(key: str, fallback: str, limit: int) -> str:
        """Return one bounded display field."""
        value = fields.get(key)

        return (value.strip() if isinstance(value, str) and value.strip() else fallback)[:limit]

    return {
        'title': text('title', heading, 120),
        'project': text('project', 'Agent plans', 80),
        'assigned_to': text('assigned_to', username, 120),
        'status': 'pending',
        'priority': 'Medium',
        'due': '',
        'created_at': created_at,
        'revision': 1,
    }


def task_summary(record: dict, username: str) -> dict:
    """Expose bounded linked-task metadata."""
    task = record['task']
    summary = {
        key: int(task[key]) if isinstance(task[key], Decimal) else task[key] for key in TASK_FIELDS
    }

    return summary | {
        'id': record['file_id'],
        'owner': record['username'],
        'editable': record['username'] == username,
    }


def list_tasks(payload: dict, table) -> dict:
    """Return one bounded page of tasks linked to READY documents."""
    options = {
        'Limit': PAGE_LIMIT,
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

    return {
        'tasks': tasks,
        'cursor': encode_cursor(
            {'scope': payload['scope'], 'username': payload['username']},
            page.get('LastEvaluatedKey'),
        ),
    }


def update_task(payload: dict, table) -> dict | None:
    """Conditionally update linked task metadata and return the resulting row."""
    record = get_ready_record(payload, table, require_task=True)

    if record is None:
        return None
    changes = dict(payload['changes'])
    expected = changes.pop('expected_revision')

    if record['task']['revision'] != expected:
        raise DocumentConflict()
    task = {**record['task'], **changes, 'revision': expected + 1}

    try:
        return table.update_item(
            Key={'username': payload['owner'], 'file_id': payload['file_id']},
            UpdateExpression='SET task = :task',
            ConditionExpression='#status = :ready AND task.revision = :expected',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':task': task,
                ':ready': 'READY',
                ':expected': expected,
            },
            ReturnValues='ALL_NEW',
        )['Attributes']
    except ClientError as error:
        if error.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise DocumentConflict() from None
        raise


def project_summary(record: dict) -> dict:
    """Expose public project fields."""

    return {
        'id': record['project_id'],
        **{field: record[field] for field in PROJECT_FIELDS},
    }


def list_projects(payload: dict, table) -> dict:
    """Return one bounded shared project page."""
    options = {
        'Limit': PAGE_LIMIT,
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
                {'username': payload['username'], 'id': key['project_id']},
                separators=(',', ':'),
            ).encode()
        ).decode()
        if key
        else None
    )

    return {
        'projects': [project_summary(item) for item in page.get('Items', [])],
        'cursor': cursor,
    }


def create_project(payload: dict, table) -> tuple[dict, int]:
    """Create a unique project or return an identical owner retry."""
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

        if existing and all(
            existing.get(field) == record[field] for field in ('owner', 'description')
        ):
            return {'project': project_summary(existing)}, 200
        raise ProjectExists() from None

    return {'project': project_summary(record)}, 201


def authorize_native_device(database, proof: dict, register: bool = False) -> bool:
    """Consume one nonce and require approval or record immutable enrollment."""
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

    if approved:
        return True

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
    } | {'pending_id': f'PENDING#{proof["approval_id"]}', 'created_at': int(time())}

    try:
        database.Table(REGISTRY_TABLE).put_item(
            Item=item,
            ConditionExpression='attribute_not_exists(pending_id)',
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

    return False


def record_deletion(payload: dict, proof: dict, table) -> dict:
    """Conditionally record one immutable owner-bound deletion observation."""
    observation = {'username': payload['username'], **payload['deletion']}
    item = observation | {
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
        optional = {'last_file_id', 'known_sha256', 'stored_sha256', 'uncertain_hash'}

        if (
            not existing
            or any(existing.get(key) != value for key, value in observation.items())
            or any(key in existing and key not in observation for key in optional)
        ):
            raise DeletionConflict('Deletion event conflicts with an existing observation')

    return {
        'event_id': observation['event_id'],
        'project_id': observation['project_id'],
        'document_id': observation['document_id'],
        'status': 'RECORDED',
    }
