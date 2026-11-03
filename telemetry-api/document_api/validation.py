"""Validate HTTP bodies and document paths before any storage operation."""

import base64
import binascii
import json
import re
from datetime import datetime
from hashlib import sha256
from pathlib import PurePosixPath
from time import time
from uuid import NAMESPACE_URL, UUID, uuid5

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

DEFAULT_FILE_GROUP = 'workflow-docs'
MAX_BODY_BYTES = 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
SEGMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
FILENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.md\Z')


def validate_event(event: dict) -> dict:
    """Decode and validate a bounded JSON upload, returning normalized files."""
    headers = {key.lower(): value for key, value in event.get('headers', {}).items()}

    if headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise ValueError('Content-Type must be application/json')
    body = event.get('body', '')

    if not isinstance(body, str) or len(body) > MAX_BODY_BYTES * 2:
        raise ValueError('Request body exceeds the limit')

    try:
        raw = (
            base64.b64decode(body, validate=True)
            if event.get('isBase64Encoded')
            else body.encode('utf-8')
        )

        if len(raw) > MAX_BODY_BYTES:
            raise ValueError('Request body exceeds 1 MiB')
        payload = json.loads(raw.decode('utf-8'))
    except (
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise ValueError('Request body must be valid UTF-8 JSON') from error

    if (
        isinstance(payload, dict)
        and set(payload) == {'username', 'action'}
        and payload['action'] == 'register_device'
    ):
        if not isinstance(payload['username'], str) or not SEGMENT.fullmatch(payload['username']):
            raise ValueError('Invalid username')

        return payload

    if isinstance(payload, dict) and payload.get('action') == 'report_deleted':
        return validate_deletion(payload)

    if (
        not isinstance(payload, dict)
        or not {'username', 'files'} <= set(payload)
        or set(payload) - {'username', 'files', 'file-group', 'storage_layout'}
    ):
        raise ValueError('Provide username, files and optional file-group only')
    username = payload['username']

    if 'storage_layout' in payload and payload['storage_layout'] != 'project-path-v1':
        raise ValueError('Unsupported upload layout')
    path_layout = payload.get('storage_layout') == 'project-path-v1'

    if not isinstance(username, str) or not SEGMENT.fullmatch(username):
        raise ValueError('Invalid username: use 1–64 ASCII letters, digits, underscores or hyphens')
    files = payload['files']

    if not isinstance(files, list) or not 1 <= len(files) <= 10:
        raise ValueError('Provide between 1 and 10 files')

    if 'file-group' in payload:
        validate_file_group(payload['file-group'])
    names = set()

    for document in files:
        if (
            not isinstance(document, dict)
            or not {'name', 'content', 'metadata'} <= set(document)
            or set(document)
            - {'name', 'content', 'metadata', 'category', 'file-group', 'relative_path'}
        ):
            raise ValueError(
                'Each file requires name, content and metadata; category/file-group are optional'
            )
        name = document['name']

        if path_layout:
            canonical = validate_relative_path(document.get('relative_path'))

            if name != canonical.rsplit('/', 1)[-1]:
                raise ValueError('Filename must match relative path')
        elif 'relative_path' in document:
            raise ValueError('relative_path requires project-path-v1')
        elif not isinstance(name, str) or not FILENAME.fullmatch(name) or '..' in name:
            raise ValueError('Each filename must be a safe .md basename')

        identity = canonical if path_layout else name

        if identity in names:
            raise ValueError('Duplicate filenames in one request are not allowed')

        names.add(identity)

        if 'category' in document:
            validate_file_group(document['category'])
        group = document.get(
            'file-group',
            payload.get('file-group', document.get('category', DEFAULT_FILE_GROUP)),
        )

        validate_file_group(group)
        document['file-group'] = group
        document['category'] = group
        content = document['content']

        try:
            if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_FILE_BYTES:
                raise ValueError('File content must be text of at most 256 KiB')
            metadata = document['metadata']

            if not isinstance(metadata, dict) or len(metadata) > 50:
                raise ValueError('Metadata must be an object with at most 50 fields')

            for key, value in metadata.items():
                if not SEGMENT.fullmatch(key) or not isinstance(
                    value, (str, bool, int, type(None))
                ):
                    raise ValueError('Metadata values must be strings, booleans, integers or null')

                if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 10**18:
                    raise ValueError('Metadata integer exceeds the supported range')

            if len(json.dumps(metadata, ensure_ascii=False).encode('utf-8')) > 16 * 1024:
                raise ValueError('Metadata exceeds 16 KiB')

            if (
                path_layout
                and metadata.get('relative_path', document['relative_path'])
                != document['relative_path']
            ):
                raise ValueError('Source path metadata differs from relative_path')
        except UnicodeError as error:
            raise ValueError('Content and metadata must be valid UTF-8') from error

    return payload


def validate_relative_path(relative: str) -> str:
    """Validate a bounded S3-safe Markdown path and strip only one leading docs/."""

    if not isinstance(relative, str):
        raise ValueError('relative_path is required')
    parts = relative.split('/')

    if (
        not relative
        or len(relative.encode('utf-8')) > 900
        or len(parts) > 32
        or any(part in ('', '.', '..') or len(part.encode('utf-8')) > 255 for part in parts)
        or '\\' in relative
        or any(ord(char) < 32 or ord(char) == 127 for char in relative)
        or not relative.lower().endswith('.md')
    ):
        raise ValueError('Invalid Markdown relative_path')

    return relative[5:] if relative.startswith('docs/') else relative


def path_document_identity(username: str, relative: str) -> tuple[str, str]:
    """Derive stable owner/path identity independent of classification or file contents."""
    canonical = validate_relative_path(relative)
    identity = str(uuid5(NAMESPACE_URL, json.dumps([username, canonical], separators=(',', ':'))))

    return identity, f'agent-work/raw-markdown/{username}/{canonical}'


def validate_deletion(payload: dict) -> dict:
    """Validate bounded observation metadata without granting authority over remote objects."""

    if set(payload) != {'username', 'action', 'deletion'}:
        raise ValueError('Provide username, action and deletion only')

    if not isinstance(payload['username'], str) or not SEGMENT.fullmatch(payload['username']):
        raise ValueError('Invalid username')
    deletion = payload['deletion']
    required = {'event_id', 'project_id', 'document_id', 'relative_path', 'observed_at'}
    optional = {'last_file_id', 'known_sha256', 'stored_sha256', 'uncertain_hash'}

    if not isinstance(deletion, dict) or not required <= set(deletion) <= required | optional:
        raise ValueError('Invalid deletion observation fields')

    try:
        for field in ('event_id', 'project_id', 'document_id', 'last_file_id'):
            if field not in deletion:
                continue
            value = deletion[field]

            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError()

            if (field == 'event_id' and UUID(value).version != 4) or (
                field == 'last_file_id' and UUID(value).version not in (4, 5)
            ):
                raise ValueError()
        relative = deletion['relative_path']

        if (
            not isinstance(relative, str)
            or not 1 <= len(relative.encode('utf-8')) <= 4096
            or not relative.lower().endswith('.md')
            or relative.startswith('/')
            or any(part in ('', '.', '..') for part in relative.split('/'))
            or PurePosixPath(relative).as_posix() != relative
            or any(ord(char) < 32 or ord(char) == 127 for char in relative)
        ):
            raise ValueError()
        identity = relative[4:] if relative.startswith('doc/') else '/' + relative

        if deletion['document_id'] != str(uuid5(UUID(deletion['project_id']), identity)):
            raise ValueError()
        observed = deletion['observed_at']

        if not isinstance(observed, str) or not re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z',
            observed,
        ):
            raise ValueError()
        parsed = datetime.fromisoformat(observed[:-1] + '+00:00')

        if parsed.utcoffset().total_seconds() != 0:
            raise ValueError()

        for field in ('known_sha256', 'stored_sha256', 'uncertain_hash'):
            if field in deletion and (
                not isinstance(deletion[field], str)
                or not re.fullmatch('[0-9a-f]{64}', deletion[field])
            ):
                raise ValueError()
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise ValueError('Invalid deletion identity, path, timestamp or hash') from None

    return payload


def validate_read_event(event: dict) -> dict:
    """Validate trusted direct read commands and a scope-bound opaque cursor."""
    operation = event.get('operation')

    if 'requestContext' in event or operation not in ('list_documents', 'get_document'):
        raise ValueError('Read operations require trusted direct invocation')
    required = {'operation', 'scope', 'username'}
    allowed = required | ({'cursor'} if operation == 'list_documents' else {'owner', 'file_id'})

    if operation == 'get_document':
        required |= {'owner', 'file_id'}

    if not required <= set(event) <= allowed:
        raise ValueError('Invalid read operation fields')

    if event['scope'] not in ('all', 'mine'):
        raise ValueError('Scope must be all or mine')
    # Cognito usernames are identity values, not filesystem paths; legacy ingestion
    # labels are constrained separately. A nonmatching label yields an empty mine view.
    username = event['username']

    if not isinstance(username, str) or not username.strip() or len(username) > 128:
        raise ValueError('Invalid verified username')
    payload = dict(event)

    if operation == 'get_document':
        if not isinstance(event['owner'], str) or not SEGMENT.fullmatch(event['owner']):
            raise ValueError('Invalid document owner')

        if not isinstance(event['file_id'], str) or not re.fullmatch(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[45][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
            event['file_id'],
        ):
            raise ValueError('Invalid file_id')
    elif 'cursor' in event:
        cursor = event['cursor']

        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
                raise ValueError('Invalid cursor')
            decoded = json.loads(
                base64.b64decode(cursor, altchars=b'-_', validate=True).decode('utf-8')
            )

            if (
                not isinstance(decoded, dict)
                or set(decoded) != {'scope', 'username', 'key'}
                or decoded['scope'] != event['scope']
                or decoded['username'] != username
            ):
                raise ValueError('Cursor does not match this view')
            key = decoded['key']

            if (
                not isinstance(key, dict)
                or set(key) != {'username', 'file_id'}
                or not isinstance(key['username'], str)
                or not SEGMENT.fullmatch(key['username'])
                or not isinstance(key['file_id'], str)
                or not re.fullmatch(
                    r'[0-9a-f]{8}-[0-9a-f]{4}-[45][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
                    key['file_id'],
                )
                or (event['scope'] == 'mine' and key['username'] != username)
            ):
                raise ValueError('Invalid cursor key')
            payload['start_key'] = key
        except (ValueError, UnicodeError, binascii.Error, RecursionError) as error:
            raise ValueError('Invalid pagination cursor') from error

    return payload


def validate_upload_identity(event: dict, payload: dict) -> None:
    """Bind storage ownership to claims verified by the API Gateway JWT authorizer."""
    claims = event.get('requestContext', {}).get('authorizer', {}).get('jwt', {}).get('claims', {})

    if claims.get('token_use') != 'access' or not claims.get('sub'):
        raise PermissionError('Cognito access token required')

    if not claims.get('username') or payload['username'] != claims['username']:
        raise PermissionError('Upload username must match the authenticated user')


def validate_native_proof(event: dict) -> dict:
    """Verify a bounded P256 request signature and return its identity-bound registry key."""
    headers = {key.lower(): value for key, value in event.get('headers', {}).items()}
    claims = event['requestContext']['authorizer']['jwt']['claims']

    try:
        if (
            event.get('rawPath') not in ('/documents', '/browser-documents')
            or event['requestContext'].get('http', {}).get('method') != 'POST'
            or event.get('rawQueryString', '')
        ):
            raise ValueError('Wrong request target')
        authorization = headers.get('authorization', '')

        if not isinstance(authorization, str) or not re.fullmatch(
            r'Bearer [A-Za-z0-9._~-]{1,16384}', authorization
        ):
            raise ValueError('Bearer required')
        timestamp = headers.get('x-device-timestamp', '')
        nonce = headers.get('x-device-nonce', '')

        if (
            not isinstance(timestamp, str)
            or not re.fullmatch(r'[1-9][0-9]{0,11}', timestamp)
            or abs(time() - int(timestamp)) > 60
        ):
            raise ValueError('Stale timestamp')

        if not isinstance(nonce, str) or str(UUID(nonce, version=4)) != nonce:
            raise ValueError('Invalid nonce')
        public_text = headers.get('x-device-key', '')
        signature_text = headers.get('x-device-signature', '')

        if (
            not isinstance(public_text, str)
            or not 1 <= len(public_text) <= 256
            or not isinstance(signature_text, str)
            or not 1 <= len(signature_text) <= 128
        ):
            raise ValueError('Invalid proof size')
        public_der = base64.b64decode(public_text, validate=True)
        signature = base64.b64decode(signature_text, validate=True)
        public_key = serialization.load_der_public_key(public_der)

        if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
            public_key.curve, ec.SECP256R1
        ):
            raise ValueError('P256 required')

        if (
            public_key.public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            != public_der
        ):
            raise ValueError('Canonical SPKI required')
        raw_body = (
            base64.b64decode(event['body'], validate=True)
            if event.get('isBase64Encoded')
            else event['body'].encode('utf-8')
        )
        canonical = '\n'.join(
            [
                'WO-DEVICE-V1',
                'POST',
                event['rawPath'],
                timestamp,
                nonce,
                sha256(raw_body).hexdigest(),
                sha256(authorization[7:].encode()).hexdigest(),
                headers.get('x-device-id', ''),
                headers.get('x-device-mac', ''),
            ]
        ).encode('utf-8')

        public_key.verify(signature, canonical, ec.ECDSA(hashes.SHA256()))
        owner_id = sha256(
            json.dumps([claims['iss'], claims['sub']], separators=(',', ':')).encode()
        ).hexdigest()
        fingerprint = sha256(public_der).hexdigest()
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        binascii.Error,
        InvalidSignature,
        UnsupportedAlgorithm,
    ):
        raise PermissionError('Invalid native request proof') from None

    return {
        'owner_id': owner_id,
        'issuer': claims['iss'],
        'subject': claims['sub'],
        'fingerprint': fingerprint,
        'approval_id': f'{owner_id}#{fingerprint}',
        'public_key': public_text,
        'nonce': nonce,
        'timestamp': int(timestamp),
        'device_id': headers['x-device-id'],
        'device_mac': headers.get('x-device-mac', ''),
    }


def validate_file_group(group: str) -> None:
    """Require one bounded safe group segment, including caller-defined groups."""

    if not isinstance(group, str) or not SEGMENT.fullmatch(group):
        raise ValueError('file-group must be 1–64 ASCII letters, digits, underscores or hyphens')
