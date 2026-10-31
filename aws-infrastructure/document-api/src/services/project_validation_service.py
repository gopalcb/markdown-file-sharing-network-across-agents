"""Validate trusted project catalog operations and identity-bound pagination cursors."""

import base64
import binascii
import json
import re
import unicodedata
from hashlib import sha256

from services.validation_service import SEGMENT

PROJECT_ID = re.compile(r'[0-9a-f]{64}\Z')


def validate_project_event(event: dict) -> dict:
    """Validate the complete command before any table read or conditional project creation."""
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

        name.encode('utf-8')

        description.encode('utf-8')
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
