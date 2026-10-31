"""Execute validated portal uploads, folder browsing and conditional latest-file edits."""

import base64
import json

from botocore.exceptions import ClientError

from services.document_read_service import get_document
from services.document_service import store_documents
from services.document_storage_service import (
    publish_record,
    source_binding,
    storage_location,
    storage_path,
)
from services.task_service import DocumentConflict, get_task, list_tasks, update_task
from services.validation_service import SEGMENT


def list_folders(payload: dict, table) -> dict:
    """Browse one index page of owned partitions without granting S3 bucket enumeration."""
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
        record.get('file-group', record.get('category'))
        for record in page.get('Items', [])
        if isinstance(record.get('file-group', record.get('category')), str)
        and SEGMENT.fullmatch(record.get('file-group', record.get('category')))
    )
    key = page.get('LastEvaluatedKey')
    cursor = (
        base64.urlsafe_b64encode(
            json.dumps(
                {'scope': 'mine', 'username': payload['username'], 'key': key},
                separators=(',', ':'),
            ).encode()
        ).decode()
        if key
        else None
    )

    return {
        'owner': payload['username'],
        'folders': [
            {'name': group, 'prefix': f'agent-work/raw-markdown/{payload["username"]}/{group}/'}
            for group in sorted(groups)
        ],
        'cursor': cursor,
    }


def replace_document(payload: dict, table, s3, bucket: str) -> dict | None:
    """Conditionally replace current bytes using S3 ETag, without storing a revision ID."""
    key = {'username': payload['owner'], 'file_id': payload['file_id']}
    record = table.get_item(Key=key, ConsistentRead=True).get('Item')

    if not record or record.get('status') != 'READY':
        return None

    if record.get('upload_token'):
        raise DocumentConflict()
    location = storage_location(record, bucket)

    if not location['Key'].startswith(f'agent-work/raw-markdown/{payload["username"]}/'):
        raise ValueError('Legacy document cannot be edited in place')
    current = s3.head_object(**location)
    metadata = dict(current.get('Metadata', {}))

    if record.get('storage_layout') == 'project-path-v1':
        metadata['source-binding'] = source_binding(
            record['source_path'], record.get('metadata', {})
        )
    content = payload['changes']['content'].encode('utf-8')

    try:
        s3.put_object(
            **location,
            Body=content,
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
        **key,
        's3_path': storage_path(location['Bucket'], location['Key']),
        'file-group': record.get('file-group', record.get('category')),
        'created_at': record['created_at'],
        'status': 'READY',
    }

    publish_record(table, compact, record)

    return get_document(payload, table, s3, bucket)


def execute_portal(
    payload: dict, table, s3, bucket: str, legacy_bucket: str | None, request_id: str
) -> tuple[dict, int]:
    """Dispatch only validated trusted commands and keep per-file upload outcomes explicit."""
    operation = payload['operation']

    if operation == 'upload_documents':
        files = store_documents(payload['upload'], s3, table, bucket, request_id)

        return {'files': files, 'request_id': request_id}, 201 if all(
            item['status'] == 'READY' for item in files
        ) else 207

    if operation == 'list_folders':
        return list_folders(payload, table), 200

    if operation == 'list_tasks':
        return list_tasks(payload, table), 200

    if operation == 'get_task':
        result = get_task(payload, table, s3, bucket, legacy_bucket)
    elif operation == 'update_task':
        result = update_task(payload, table, s3, bucket, legacy_bucket)
    else:
        result = replace_document(payload, table, s3, bucket)

    return (result, 200) if result is not None else ({'error': 'not_found'}, 404)
