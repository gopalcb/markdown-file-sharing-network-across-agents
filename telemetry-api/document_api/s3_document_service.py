"""Own Markdown object paths, writes, reads, edits, and storage orchestration."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import quote, unquote, urlsplit

from botocore.exceptions import BotoCoreError, ClientError

from document_api.dynamodb_document_service import (
    DocumentConflict,
    document_summary,
    get_ready_record,
    list_folders,
    list_tasks,
    plan_task,
    publish_record,
    task_summary,
    update_task,
)
from document_api.validation import (
    FILENAME,
    MAX_FILE_BYTES,
    path_document_identity,
    validate_file_group,
    validate_relative_path,
)

LOGGER = logging.getLogger(__name__)
LAYOUT = 'project-path-v1'


@dataclass(frozen=True)
class DocumentStorage:
    """Bundle the AWS resources and bucket names shared by document operations."""

    table: object
    s3: object
    bucket: str
    legacy_bucket: str | None = None


def source_binding(source_path: str, metadata: dict) -> str:
    """Bind a canonical path to its original source project."""
    value = str(metadata.get('project_id', '')) + '\n' + source_path

    return sha256(value.encode()).hexdigest()


def storage_path(bucket: str, key: str) -> str:
    """Build a plain S3 URI without revision or query parameters."""

    return f's3://{bucket}/{quote(key, safe="/")}'


def storage_location(record: dict, storage: DocumentStorage) -> dict:
    """Resolve compact or legacy rows and reject foreign storage locations."""

    if 's3_path' in record:
        uri = urlsplit(record['s3_path'])

        if uri.scheme != 's3' or uri.query or uri.fragment or not uri.netloc:
            raise ValueError('Invalid stored S3 path')
        stored_bucket, key = uri.netloc, unquote(uri.path.lstrip('/'))
    else:
        stored_bucket, key = record['s3_bucket'], record['s3_key']
    owner = record['username']
    current_prefix = f'agent-work/raw-markdown/{owner}/'
    legacy_prefix = f'docs/user-docs/{owner}/'

    if key.startswith(current_prefix) and stored_bucket == storage.bucket:
        relative = key[len(current_prefix) :]
    elif key.startswith(legacy_prefix) and stored_bucket == (
        storage.legacy_bucket or storage.bucket
    ):
        relative = key[len(legacy_prefix) :]
    else:
        raise ValueError('Invalid stored document location')

    validate_relative_path(relative)

    return {'Bucket': stored_bucket, 'Key': key}


def legacy_storage_location(
    record: dict,
    payload: dict,
    storage: DocumentStorage,
) -> dict:
    """Validate a legacy expanded index row and return its exact object location."""
    filename = record.get('filename')

    if (
        record.get('username') != payload['owner']
        or record.get('file_id') != payload['file_id']
        or not isinstance(filename, str)
        or (
            record.get('storage_layout') != LAYOUT
            and (not FILENAME.fullmatch(filename) or '..' in filename)
        )
    ):
        raise ValueError('Invalid stored document identity')
    layout = record.get('storage_layout')

    if layout == LAYOUT:
        identity, key = path_document_identity(payload['owner'], record.get('source_path'))
        relative = validate_relative_path(record.get('source_path'))

        if (
            identity != payload['file_id']
            or relative != record.get('relative_path')
            or filename != relative.rsplit('/', 1)[-1]
        ):
            raise ValueError('Invalid path document identity')
    elif layout in ('raw-markdown-v1', 'raw-markdown-v2'):
        group = record.get('file-group', record.get('category'))

        validate_file_group(group)

        if group != record.get('category'):
            raise ValueError('Stored file group differs from category')
        suffix = (
            filename if layout == 'raw-markdown-v2' else f'{filename[:-3]}-{payload["file_id"]}.md'
        )
        key = f'agent-work/raw-markdown/{payload["owner"]}/{group}/{suffix}'
    elif layout is None:
        key = f'docs/user-docs/{payload["owner"]}/{filename[:-3]}-{payload["file_id"]}.md'
    else:
        raise ValueError('Unknown stored document layout')
    expected_bucket = (
        storage.bucket
        if layout in ('raw-markdown-v1', 'raw-markdown-v2', LAYOUT)
        else (storage.legacy_bucket or storage.bucket)
    )

    if record['s3_bucket'] != expected_bucket or record['s3_key'] != key:
        raise ValueError('Invalid stored document location')

    return {'Bucket': expected_bucket, 'Key': key}


def store_path_document(payload: dict, document: dict, storage: DocumentStorage) -> dict:
    """Overwrite one canonical path while retaining stable identity and task edits."""
    username, source = payload['username'], document['relative_path']
    relative = validate_relative_path(source)
    file_id, s3_key = path_document_identity(username, source)
    key = {'username': username, 'file_id': file_id}
    existing = storage.table.get_item(Key=key, ConsistentRead=True).get('Item')

    if existing and (existing.get('upload_token') or existing.get('status') != 'READY'):
        raise ValueError('An earlier upload is unresolved; inspect before replacing')
    binding = source_binding(source, document.get('metadata', {}))

    if (
        existing
        and 's3_path' not in existing
        and (
            existing.get('source_path') != source
            or existing.get('metadata', {}).get('project_id')
            != document.get('metadata', {}).get('project_id')
        )
    ):
        raise ValueError('Another source already occupies this canonical path')
    content = document['content'].encode()
    group = document.get(
        'file-group',
        payload.get('file-group', document.get('category', 'workflow-docs')),
    )
    options = {
        'Bucket': storage.bucket,
        'Key': s3_key,
        'Body': content,
        'ContentType': 'text/markdown; charset=utf-8',
        'ServerSideEncryption': 'AES256',
        'Metadata': {'source-binding': binding},
    }

    if existing:
        head = storage.s3.head_object(Bucket=storage.bucket, Key=s3_key)

        if head.get('Metadata', {}).get('source-binding', binding) != binding:
            raise ValueError('Another source already occupies this canonical path')
        options['IfMatch'] = head['ETag']
    else:
        options['IfNoneMatch'] = '*'

    try:
        storage.s3.put_object(**options)
    except ClientError as error:
        if existing or error.response['Error']['Code'] not in {
            'PreconditionFailed',
            '412',
        }:
            raise
        head = storage.s3.head_object(Bucket=storage.bucket, Key=s3_key)
        metadata = head.get('Metadata', {})
        legacy_raw = (
            payload.get('storage_layout') != LAYOUT
            and not metadata.get('source-binding')
            and not metadata.get('upload-token')
        )

        if metadata.get('source-binding') != binding and not legacy_raw:
            raise ValueError('Existing object belongs to another source') from None

        options.pop('IfNoneMatch')

        storage.s3.put_object(**options, IfMatch=head['ETag'])
    record = {
        **key,
        's3_path': storage_path(storage.bucket, s3_key),
        'file-group': group,
        'created_at': existing['created_at']
        if existing
        else datetime.now(timezone.utc).isoformat(),
        'status': 'READY',
    }
    task = plan_task(document, username, record['created_at'])

    if task is not None:
        record['task'] = task
    stored = publish_record(storage.table, record, existing)
    result = {
        'name': document['name'],
        'file_id': file_id,
        'status': 'READY',
        'storage_layout': LAYOUT,
        'relative_path': relative,
        'sha256': sha256(content).hexdigest(),
        's3_key': s3_key,
    }

    if isinstance(stored.get('task'), dict):
        result['task'] = task_summary(stored, username)

    return result


def store_documents(payload: dict, storage: DocumentStorage, request_id: str) -> list:
    """Store independent files and return truthful per-file outcomes."""
    results = []

    for source in payload['files']:
        document = source

        if payload.get('storage_layout') != LAYOUT:
            group = source.get(
                'file-group',
                payload.get('file-group', source.get('category', 'workflow-docs')),
            )
            document = {**source, 'relative_path': f'docs/{group}/{source["name"]}'}
        file_id, _ = path_document_identity(payload['username'], document['relative_path'])

        try:
            results.append(store_path_document(payload, document, storage))
        except (BotoCoreError, ClientError, ValueError) as error:
            LOGGER.warning(
                'Document upload incomplete',
                extra={
                    'event': 'upload_incomplete',
                    'request_id': request_id,
                    'file_id': file_id,
                    'error_type': type(error).__name__,
                },
            )

            results.append(
                {
                    'name': document['name'],
                    'file_id': file_id,
                    'status': 'INCOMPLETE',
                    'error': 'Storage not confirmed; inspect this file_id before retrying',
                }
            )

    return results


def read_document(
    payload: dict,
    storage: DocumentStorage,
) -> dict | None:
    """Read one authorized READY Markdown object with bounded UTF-8 content."""
    record = get_ready_record(payload, storage.table)

    if record is None:
        return None
    location = (
        storage_location(record, storage)
        if 's3_path' in record
        else legacy_storage_location(record, payload, storage)
    )
    response = storage.s3.get_object(**location)
    body = response['Body']

    try:
        if response.get('ContentLength', 0) > MAX_FILE_BYTES:
            raise ValueError('Stored document exceeds content limit')
        content = body.read(MAX_FILE_BYTES + 1)

        if len(content) > MAX_FILE_BYTES:
            raise ValueError('Stored document exceeds content limit')
        text = content.decode()
    finally:
        body.close()
    details = {
        **document_summary(record),
        'content': text,
        'size_bytes': len(content),
        'sha256': sha256(content).hexdigest(),
        'metadata': {},
        'editable': record['username'] == payload['username']
        and location['Key'].startswith(f'agent-work/raw-markdown/{record["username"]}/'),
    }

    if isinstance(response.get('ETag'), str):
        details['etag'] = response['ETag']

    return {'document': details}


def replace_document(payload: dict, storage: DocumentStorage) -> dict | None:
    """Replace current bytes conditionally by S3 ETag."""
    record = get_ready_record(payload, storage.table)

    if record is None:
        return None

    if record.get('upload_token'):
        raise DocumentConflict()
    location = storage_location(record, storage)

    if not location['Key'].startswith(f'agent-work/raw-markdown/{payload["username"]}/'):
        raise ValueError('Legacy document cannot be edited in place')
    metadata = dict(storage.s3.head_object(**location).get('Metadata', {}))

    if record.get('storage_layout') == LAYOUT:
        metadata['source-binding'] = source_binding(
            record['source_path'],
            record.get('metadata', {}),
        )

    try:
        storage.s3.put_object(
            **location,
            Body=payload['changes']['content'].encode(),
            IfMatch=payload['changes']['expected_etag'],
            ContentType='text/markdown; charset=utf-8',
            ServerSideEncryption='AES256',
            Metadata=metadata,
        )
    except ClientError as error:
        if error.response['Error']['Code'] in {
            'PreconditionFailed',
            'ConditionalRequestConflict',
            '412',
            '409',
        }:
            raise DocumentConflict() from None
        raise
    compact = {
        'username': payload['owner'],
        'file_id': payload['file_id'],
        's3_path': storage_path(location['Bucket'], location['Key']),
        'file-group': record.get('file-group', record.get('category')),
        'created_at': record['created_at'],
        'status': 'READY',
    }

    publish_record(storage.table, compact, record)

    return read_document(payload, storage)


def get_task(
    payload: dict,
    storage: DocumentStorage,
    record: dict | None = None,
) -> dict | None:
    """Return task metadata with its edited or source Markdown description."""
    record = record or get_ready_record(payload, storage.table, require_task=True)

    if record is None:
        return None
    description = record['task'].get('description')

    if description is None:
        document = read_document(payload, storage)

        if document is None:
            return None
        description = document['document']['content']

    return {
        'task': {
            **task_summary(record, payload['username']),
            'description': description,
        }
    }


def execute_portal(
    payload: dict,
    storage: DocumentStorage,
    request_id: str,
) -> tuple[dict, int]:
    """Dispatch validated portal document operations."""
    operation = payload['operation']

    if operation == 'upload_documents':
        files = store_documents(payload['upload'], storage, request_id)
        status = 201 if all(item['status'] == 'READY' for item in files) else 207

        return {'files': files, 'request_id': request_id}, status

    if operation == 'list_folders':
        return list_folders(payload, storage.table), 200

    if operation == 'list_tasks':
        return list_tasks(payload, storage.table), 200

    if operation == 'get_task':
        result = get_task(payload, storage)
    elif operation == 'update_task':
        record = update_task(payload, storage.table)
        result = get_task(payload, storage, record) if record else None
    else:
        result = replace_document(payload, storage)

    return (result, 200) if result is not None else ({'error': 'not_found'}, 404)
