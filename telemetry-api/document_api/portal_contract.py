"""Bind browser document commands to verified identity and public response shapes."""

import json
import re

from document_api.portal_validation import PROJECT_ID

MAX_CONTENT_BYTES = 256 * 1024
OWNER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
FILE_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[45][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z')
SUMMARY_FIELDS = ('owner', 'file_id', 'filename', 'category', 'created_at', 'size_bytes', 'status')
TASK_TEXT = {'title': 120, 'project': 80, 'assigned_to': 120, 'created_at': 128, 'due': 10}
TASK_STATE = {'status': {'pending', 'in-progress', 'done'}, 'priority': {'High', 'Medium', 'Low'}}
PROJECT_FIELDS = {'id', 'name', 'description', 'created_at', 'owner'}
PROJECT_TEXT = {'name': (1, 80), 'description': (0, 2000), 'created_at': (1, 128)}


def validate_document_request(
    query, username: str, owner: str | None = None, file_id: str | None = None
) -> dict:
    """Build an identity-bound document read command from query input."""
    allowed = {'scope', 'cursor'} if owner is None else {'scope'}

    if set(query) - allowed or any(len(query.getlist(key)) != 1 for key in query):
        raise ValueError('Invalid document query')
    scope = query.get('scope', 'all')

    if scope not in ('all', 'mine'):
        raise ValueError('Invalid scope')
    payload = {
        'operation': 'list_documents' if owner is None else 'get_document',
        'scope': scope,
        'username': username,
    }

    if owner is not None:
        if not OWNER.fullmatch(owner) or not FILE_ID.fullmatch(file_id or ''):
            raise ValueError('Invalid document identifier')

        payload.update(owner=owner, file_id=file_id)
    elif 'cursor' in query:
        cursor = query['cursor']

        if not 1 <= len(cursor) <= 2048 or not re.fullmatch(r'[A-Za-z0-9_=-]+', cursor):
            raise ValueError('Invalid cursor')
        payload['cursor'] = cursor

    return payload


def validate_document_response(body: dict, payload: dict) -> dict:
    """Project bounded reads without leaking cross-owner or storage fields."""

    if not isinstance(body, dict):
        raise ValueError('Invalid document response')
    listing = payload['operation'] == 'list_documents'
    records = body.get('documents') if listing else [body.get('document')]

    if not isinstance(records, list) or len(records) > 50:
        raise ValueError('Invalid document records')
    documents = []

    for record in records:
        if not isinstance(record, dict) or not set(SUMMARY_FIELDS) <= set(record):
            raise ValueError('Invalid document record')

        if any(
            not isinstance(record[field], str) or not record[field] or len(record[field]) > 256
            for field in ('owner', 'file_id', 'filename', 'category', 'created_at', 'status')
        ):
            raise ValueError('Invalid document field')

        if (
            not OWNER.fullmatch(record['owner'])
            or not FILE_ID.fullmatch(record['file_id'])
            or record['status'] != 'READY'
            or (
                not (listing and record['size_bytes'] is None)
                and (
                    type(record['size_bytes']) is not int
                    or not 0 <= record['size_bytes'] <= MAX_CONTENT_BYTES
                )
            )
            or (payload['scope'] == 'mine' and record['owner'] != payload['username'])
        ):
            raise ValueError('Invalid document summary')
        document = {key: record[key] for key in SUMMARY_FIELDS}

        if not listing:
            if record['owner'] != payload['owner'] or record['file_id'] != payload['file_id']:
                raise ValueError('Invalid requested document')
            content = record.get('content')

            if (
                not isinstance(content, str)
                or len(content.encode()) > MAX_CONTENT_BYTES
                or len(content.encode()) != record['size_bytes']
            ):
                raise ValueError('Invalid document content')
            document['content'] = content

            if 'etag' in record:
                etag, digest = record.get('etag'), record.get('sha256')

                if (
                    not isinstance(etag, str)
                    or not re.fullmatch(r'"[\x21\x23-\x7e]{1,1022}"', etag)
                    or not isinstance(digest, str)
                    or not re.fullmatch('[0-9a-f]{64}', digest)
                ):
                    raise ValueError('Invalid revision metadata')

                document.update(
                    etag=etag,
                    sha256=digest,
                    editable=record['owner'] == payload['username']
                    and record.get('editable') is True,
                )
                metadata = record.get('metadata', {})

                if not isinstance(metadata, dict):
                    raise ValueError('Invalid metadata')
                allowed = {
                    'kind',
                    'project',
                    'assigned_to',
                    'title',
                    'category',
                    'agent',
                    'description',
                }
                document['metadata'] = {
                    key: value
                    for key, value in metadata.items()
                    if key in allowed and isinstance(value, str) and len(value) <= 8192
                }

        documents.append(document)

    if not listing:
        return {'document': documents[0]}

    cursor = body.get('cursor')

    if 'cursor' not in body or (
        cursor is not None
        and (
            not isinstance(cursor, str)
            or not 1 <= len(cursor) <= 2048
            or not re.fullmatch(r'[A-Za-z0-9_=-]+', cursor)
        )
    ):
        raise ValueError('Invalid document cursor')

    return {'documents': documents, 'cursor': cursor}


def portal_request(
    operation: str,
    username: str,
    query,
    body=None,
    owner=None,
    file_id=None,
) -> dict:
    """Build one identity-bound core command from browser input."""

    if operation in ('list_projects', 'create_project'):
        if set(query) - {'cursor'} or any(len(query.getlist(key)) != 1 for key in query):
            raise ValueError('Invalid project query')
        payload = {'operation': operation, 'username': username}

        if operation == 'create_project':
            valid = (
                not query
                and isinstance(body, dict)
                and set(body) == {'name', 'description'}
                and isinstance(body['name'], str)
                and 1 <= len(body['name'].strip()) <= 80
                and isinstance(body['description'], str)
                and len(body['description']) <= 2000
            )

            if not valid:
                raise ValueError('Invalid project')
            payload['project'] = body
        elif 'cursor' in query:
            if not 1 <= len(query['cursor']) <= 1024:
                raise ValueError('Invalid project cursor')
            payload['cursor'] = query['cursor']

        return payload

    if operation == 'upload_documents':
        valid = (
            not query
            and isinstance(body, dict)
            and not set(body) - {'files', 'file-group'}
            and isinstance(body.get('files'), list)
            and 1 <= len(body['files']) <= 10
            and len(json.dumps(body).encode()) <= 1024 * 1024
        )

        if not valid:
            raise ValueError('Invalid upload')

        return {'operation': operation, 'username': username, 'upload': body}
    payload = validate_document_request(query, username, owner, file_id)
    payload['operation'] = operation

    if operation == 'list_folders':
        if query and set(query) != {'cursor'}:
            raise ValueError('Invalid folder query')
        payload['scope'] = 'mine'

    if operation in {'replace_document', 'update_task'}:
        if owner != username:
            raise PermissionError('Forbidden')

        if query or not isinstance(body, dict):
            raise ValueError('Invalid changes')

        payload.update(scope='mine', changes=body)

    return payload


def safe_task(task: dict, payload: dict, detail: bool = False) -> dict:
    """Project a bounded task owned by the requested document identity."""

    if (
        not isinstance(task, dict)
        or not OWNER.fullmatch(task.get('owner', ''))
        or not FILE_ID.fullmatch(task.get('id', ''))
        or (payload.get('scope') == 'mine' and task['owner'] != payload['username'])
        or (
            payload.get('owner')
            and (task['owner'] != payload['owner'] or task['id'] != payload['file_id'])
        )
    ):
        raise ValueError('Invalid task identity')

    if any(
        not isinstance(task.get(field), str) or len(task[field]) > limit
        for field, limit in TASK_TEXT.items()
    ) or any(task.get(field) not in values for field, values in TASK_STATE.items()):
        raise ValueError('Invalid task state')

    if type(task.get('revision')) is not int or task['revision'] < 1:
        raise ValueError('Invalid task revision')
    result = {
        field: task[field] for field in (*TASK_TEXT, *TASK_STATE, 'revision', 'owner', 'id')
    } | {'editable': task['owner'] == payload['username']}

    if detail:
        description = task.get('description')

        if not isinstance(description, str) or len(description.encode()) > MAX_CONTENT_BYTES:
            raise ValueError('Invalid task description')
        result['description'] = description

    return result


def bounded_cursor(body: dict, limit: int = 2048) -> str | None:
    """Return a bounded nullable cursor from a core response."""
    cursor = body.get('cursor')

    if 'cursor' not in body or (
        cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= limit)
    ):
        raise ValueError('Invalid cursor')

    return cursor


def portal_response(body: dict, payload: dict) -> dict:
    """Project one core response onto the browser API contract."""

    if not isinstance(body, dict):
        raise ValueError('Invalid portal response')
    operation = payload['operation']

    if operation == 'replace_document':
        return validate_document_response(body, {**payload, 'operation': 'get_document'})

    if operation in {'get_task', 'update_task'}:
        return {'task': safe_task(body.get('task'), payload, True)}

    if operation == 'upload_documents':
        expected, receipts = payload['upload']['files'], body.get('files')

        if not isinstance(receipts, list) or len(receipts) != len(expected):
            raise ValueError('Invalid upload receipts')
        files = []

        for receipt, source in zip(receipts, expected):
            if (
                not isinstance(receipt, dict)
                or receipt.get('name') != source.get('name')
                or not FILE_ID.fullmatch(receipt.get('file_id', ''))
                or receipt.get('status') not in ('READY', 'INCOMPLETE')
            ):
                raise ValueError('Invalid upload receipt')
            item = {field: receipt[field] for field in ('name', 'file_id', 'status')}

            if receipt['status'] == 'READY':
                group = source.get(
                    'file-group',
                    payload['upload'].get('file-group', source.get('category', 'workflow-docs')),
                )
                expected_key = (
                    f'agent-work/raw-markdown/{payload["username"]}/{group}/{source["name"]}'
                )

                if receipt.get('s3_key') != expected_key:
                    raise ValueError('Invalid upload location')
                item['s3_key'] = expected_key

                if 'task' in receipt:
                    item['task'] = safe_task(receipt['task'], payload)
            else:
                item['error'] = 'Storage not confirmed; inspect this file_id before retrying'

            files.append(item)

        return {'files': files, 'request_id': str(body.get('request_id', ''))[:128]}

    if operation in ('list_projects', 'create_project'):
        records = body.get('projects') if operation == 'list_projects' else [body.get('project')]

        if (
            not isinstance(records, list)
            or len(records) > 50
            or (operation == 'create_project' and len(records) != 1)
        ):
            raise ValueError('Invalid projects')
        projects = []

        for record in records:
            if (
                not isinstance(record, dict)
                or set(record) != PROJECT_FIELDS
                or not isinstance(record['id'], str)
                or not PROJECT_ID.fullmatch(record['id'])
                or not isinstance(record['owner'], str)
                or not OWNER.fullmatch(record['owner'])
                or any(
                    not isinstance(record[field], str)
                    or not minimum <= len(record[field]) <= maximum
                    for field, (minimum, maximum) in PROJECT_TEXT.items()
                )
            ):
                raise ValueError('Invalid project')

            projects.append(record)

        if operation == 'create_project':
            if projects[0]['owner'] != payload['username']:
                raise ValueError('Invalid project owner')

            return {'project': projects[0]}

        return {'projects': projects, 'cursor': bounded_cursor(body, 1024)}

    cursor = bounded_cursor(body)

    if operation == 'list_tasks':
        tasks = body.get('tasks')

        if not isinstance(tasks, list) or len(tasks) > 50:
            raise ValueError('Invalid tasks')

        return {'tasks': [safe_task(task, payload) for task in tasks], 'cursor': cursor}

    if operation == 'list_folders':
        folders = body.get('folders')

        if body.get('owner') != payload['username'] or not isinstance(folders, list):
            raise ValueError('Invalid folders')
        names = [folder.get('name') if isinstance(folder, dict) else None for folder in folders]

        if len(names) > 103 or any(
            not isinstance(name, str) or not OWNER.fullmatch(name) for name in names
        ):
            raise ValueError('Invalid folders')

        return {
            'owner': payload['username'],
            'folders': [
                {
                    'name': name,
                    'prefix': f'agent-work/raw-markdown/{payload["username"]}/{name}/',
                }
                for name in names
            ],
            'cursor': cursor,
        }

    raise ValueError('Unsupported operation')
