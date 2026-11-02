"""Validate trusted portal document mutations and linked-task commands before storage."""

import base64
import binascii
import json
import re
import unicodedata
from hashlib import sha256

from document_api.validation import (
    MAX_FILE_BYTES,
    SEGMENT,
    validate_event,
    validate_read_event,
)

PROJECT_ID = re.compile(r'[0-9a-f]{64}\Z')
PORTAL_OPERATIONS = {
    'upload_documents',
    'list_folders',
    'replace_document',
    'list_tasks',
    'get_task',
    'update_task',
    'list_projects',
    'create_project',
}
TASK_FIELDS = {'title': 120, 'project': 80, 'assigned_to': 120, 'description': MAX_FILE_BYTES}


def validate_project_event(event: dict) -> dict:
    """Validate project catalog commands and caller-bound cursors."""
    operation = event.get('operation')
    username = event.get('username')

    if 'requestContext' in event or operation not in ('list_projects', 'create_project'):
        raise ValueError('Projects require trusted portal invocation')

    if not isinstance(username, str) or not SEGMENT.fullmatch(username):
        raise ValueError('Invalid verified owner')
    allowed = (
        {'operation', 'username', 'project'}
        if operation == 'create_project'
        else {'operation', 'username', 'cursor'}
    )

    if set(event) - allowed:
        raise ValueError('Invalid project command fields')
    payload = dict(event)

    if operation == 'create_project':
        project = event.get('project')

        if not isinstance(project, dict) or set(project) != {'name', 'description'}:
            raise ValueError('Provide project name and description only')
        name, description = project['name'], project['description']

        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 80
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise ValueError('Project name must be 1–80 characters without control characters')

        if not isinstance(description, str) or len(description) > 2000:
            raise ValueError('Project description exceeds 2000 characters')

        name.encode()

        description.encode()
        name = name.strip()
        normalized = unicodedata.normalize('NFKC', name).casefold()
        payload['project'] = {
            'name': name,
            'description': description,
            'id': sha256(normalized.encode()).hexdigest(),
        }

        return payload

    if 'cursor' in event:
        cursor = event['cursor']

        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
                raise ValueError('Invalid project cursor')
            value = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))

            if (
                not isinstance(value, dict)
                or set(value) != {'username', 'id'}
                or value['username'] != username
                or not isinstance(value['id'], str)
                or not PROJECT_ID.fullmatch(value['id'])
            ):
                raise ValueError('Project cursor does not match this identity')
            payload['start_key'] = {'project_id': value['id']}
        except (ValueError, UnicodeError, binascii.Error, RecursionError) as error:
            raise ValueError('Invalid project cursor') from error

    return payload


def validate_portal_event(event: dict) -> dict:
    """Reject HTTP dispatch and caller-controlled ownership, validating all mutation fields."""
    operation = event.get('operation')

    if operation in ('list_projects', 'create_project'):
        return validate_project_event(event)
    username = event.get('username')

    if 'requestContext' in event or operation not in PORTAL_OPERATIONS:
        raise ValueError('Portal operations require trusted direct invocation')

    if not isinstance(username, str) or not SEGMENT.fullmatch(username):
        raise ValueError('Invalid verified owner')

    if operation == 'upload_documents':
        if set(event) != {'operation', 'username', 'upload'} or not isinstance(
            event['upload'], dict
        ):
            raise ValueError('Invalid upload')

        if set(event['upload']) - {'files', 'file-group'}:
            raise ValueError('Upload cannot select identity or storage keys')
        upload = validate_event(
            {
                'headers': {'content-type': 'application/json'},
                'body': json.dumps({**event['upload'], 'username': username}),
            }
        )

        return {**event, 'upload': upload}
    listing = operation in {'list_tasks', 'list_folders'}
    allowed = (
        {'operation', 'username', 'scope', 'cursor'}
        if listing
        else {'operation', 'username', 'scope', 'owner', 'file_id'}
    )

    if operation in {'replace_document', 'update_task'}:
        allowed.add('changes')

    if set(event) - allowed:
        raise ValueError('Invalid operation fields')
    query = {key: value for key, value in event.items() if key != 'changes'}
    query['operation'] = 'list_documents' if listing else 'get_document'
    query['scope'] = event.get('scope', 'mine' if operation == 'list_folders' else 'all')

    if operation == 'list_folders' and query['scope'] != 'mine':
        raise ValueError('Folders are owner scoped')
    validated = validate_read_event(query)
    payload = {**validated, 'operation': operation}

    if operation in {'replace_document', 'update_task'}:
        if event.get('owner') != username:
            raise PermissionError('Only the owner can modify this record')
        changes = event.get('changes')

        if not isinstance(changes, dict):
            raise ValueError('Invalid changes')

        if operation == 'replace_document':
            if set(changes) != {'content', 'expected_etag'}:
                raise ValueError('Provide content and expected_etag')

            if (
                not isinstance(changes['content'], str)
                or len(changes['content'].encode('utf-8')) > MAX_FILE_BYTES
            ):
                raise ValueError('Document exceeds 256 KiB')

            if (
                not isinstance(changes['expected_etag'], str)
                or not 1 <= len(changes['expected_etag']) <= 1024
            ):
                raise ValueError('Invalid document ETag')
        else:
            if 'expected_revision' not in changes or set(changes) - (
                set(TASK_FIELDS) | {'expected_revision', 'status', 'priority', 'due'}
            ):
                raise ValueError('Invalid task changes')

            if type(changes['expected_revision']) is not int or changes['expected_revision'] < 1:
                raise ValueError('Invalid task revision')

            for field, limit in TASK_FIELDS.items():
                if field in changes and (
                    not isinstance(changes[field], str)
                    or len(changes[field].encode('utf-8')) > limit
                    or (field in {'title', 'project'} and not changes[field].strip())
                ):
                    raise ValueError('Invalid task text')

            if 'status' in changes and changes['status'] not in ('pending', 'in-progress', 'done'):
                raise ValueError('Invalid status')

            if 'priority' in changes and changes['priority'] not in ('High', 'Medium', 'Low'):
                raise ValueError('Invalid priority')

            if 'due' in changes and (
                not isinstance(changes['due'], str)
                or (changes['due'] and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', changes['due']))
            ):
                raise ValueError('Invalid due date')
        payload['changes'] = changes

    return payload
