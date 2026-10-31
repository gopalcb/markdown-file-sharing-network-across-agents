"""Read READY document summaries and bounded private Markdown for the portal.

The caller must be the trusted portal role; verified usernames arrive from its
Cognito middleware. Validation owns the direct invocation command shape.
"""

import base64
import json
from hashlib import sha256
from urllib.parse import unquote, urlsplit

from services.document_storage_service import storage_location
from services.validation_service import (
    FILENAME,
    MAX_FILE_BYTES,
    path_document_identity,
    validate_file_group,
    validate_relative_path,
)

PAGE_LIMIT = 50
SUMMARY_FIELDS = ('file_id', 'filename', 'category', 'created_at', 'status')


def summarize_document(record: dict) -> dict:
    """Expose library fields without storage locations, metadata or request IDs."""

    if 's3_path' in record:
        filename = unquote(urlsplit(record['s3_path']).path).rsplit('/', 1)[-1]

        return {
            'owner': record['username'],
            'file_id': record['file_id'],
            'filename': filename,
            'category': record['file-group'],
            'created_at': record['created_at'],
            'status': record['status'],
            'size_bytes': None,
        }

    return {
        'owner': record['username'],
        **{key: record[key] for key in SUMMARY_FIELDS},
        'size_bytes': int(record['size_bytes']),
    }


def list_documents(payload: dict, table) -> dict:
    """Evaluate at most one bounded DynamoDB page and return only READY entries."""
    options = {
        'Limit': PAGE_LIMIT,
        'FilterExpression': '#status = :ready',
        'ProjectionExpression': 'username, file_id, filename, category, created_at, size_bytes, #status, s3_path, #group',
        'ExpressionAttributeNames': {'#status': 'status', '#group': 'file-group'},
        'ExpressionAttributeValues': {':ready': 'READY'},
    }

    if 'start_key' in payload:
        options['ExclusiveStartKey'] = payload['start_key']

    if payload['scope'] == 'mine':
        options['KeyConditionExpression'] = 'username = :username'
        options['ExpressionAttributeValues'][':username'] = payload['username']
        page = table.query(**options)
    else:
        page = table.scan(**options)
    documents = [
        summarize_document(item)
        for item in page.get('Items', [])
        if item.get('status') == 'READY'
        and (payload['scope'] == 'all' or item.get('username') == payload['username'])
    ]
    cursor = None

    if page.get('LastEvaluatedKey'):
        value = {
            'scope': payload['scope'],
            'username': payload['username'],
            'key': page['LastEvaluatedKey'],
        }
        cursor = base64.urlsafe_b64encode(
            json.dumps(value, separators=(',', ':')).encode()
        ).decode()

    return {'documents': documents, 'cursor': cursor}


def get_document(
    payload: dict, table, s3, bucket: str, legacy_bucket: str | None = None
) -> dict | None:
    """Load an authorized READY row and read at most 256 KiB of UTF-8 content."""

    if payload['scope'] == 'mine' and payload['owner'] != payload['username']:
        return None
    record = table.get_item(
        Key={'username': payload['owner'], 'file_id': payload['file_id']}, ConsistentRead=True
    ).get('Item')

    if not record or record.get('status') != 'READY':
        return None

    if 's3_path' in record:
        options = storage_location(record, bucket, legacy_bucket)
    else:
        filename = record.get('filename')

        if (
            record.get('username') != payload['owner']
            or record.get('file_id') != payload['file_id']
            or not isinstance(filename, str)
            or (
                record.get('storage_layout') != 'project-path-v1'
                and (not FILENAME.fullmatch(filename) or '..' in filename)
            )
        ):
            raise ValueError('Invalid stored document identity')
        expected_key = (
            f'docs/user-docs/{payload["owner"]}/{record["filename"][:-3]}-{payload["file_id"]}.md'
        )

        layout = record.get('storage_layout')

        if layout == 'project-path-v1':
            identity, expected_key = path_document_identity(
                payload['owner'], record.get('source_path')
            )
            canonical = validate_relative_path(record.get('source_path'))

            if (
                identity != payload['file_id']
                or canonical != record.get('relative_path')
                or filename != canonical.rsplit('/', 1)[-1]
            ):
                raise ValueError('Invalid path document identity')
        elif layout in ('raw-markdown-v1', 'raw-markdown-v2'):
            validate_file_group(record.get('file-group', record.get('category')))

            if record.get('file-group', record.get('category')) != record.get('category'):
                raise ValueError('Stored file group differs from category')
            expected_key = f'agent-work/raw-markdown/{payload["owner"]}/{record["category"]}/' + (
                filename
                if layout == 'raw-markdown-v2'
                else f'{filename[:-3]}-{payload["file_id"]}.md'
            )
        elif layout is not None:
            raise ValueError('Unknown stored document layout')

        expected_bucket = (
            bucket
            if layout in ('raw-markdown-v1', 'raw-markdown-v2', 'project-path-v1')
            else (legacy_bucket or bucket)
        )

        if record['s3_bucket'] != expected_bucket or record['s3_key'] != expected_key:
            raise ValueError('Invalid stored document location')
        options = {'Bucket': expected_bucket, 'Key': expected_key}

    response = s3.get_object(**options)
    body = response['Body']

    try:
        if response.get('ContentLength', 0) > MAX_FILE_BYTES:
            raise ValueError('Stored document exceeds content limit')
        content = body.read(MAX_FILE_BYTES + 1)

        if len(content) > MAX_FILE_BYTES:
            raise ValueError('Stored document exceeds content limit')
        text = content.decode('utf-8')
    finally:
        body.close()

    details = {
        **summarize_document(record),
        'content': text,
        'size_bytes': len(content),
        'sha256': sha256(content).hexdigest(),
        'metadata': {},
        'editable': record['username'] == payload['username']
        and options['Key'].startswith(f'agent-work/raw-markdown/{record["username"]}/'),
    }

    if isinstance(response.get('ETag'), str):
        details['etag'] = response['ETag']

    return {'document': details}
