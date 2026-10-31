"""Store latest Markdown bytes at a stable owner/path identity without S3 versions.

S3 and DynamoDB acknowledgements are required for READY receipts. Interrupted
writes return INCOMPLETE; there is no durable lease or automatic recovery claim.
"""

from datetime import datetime, timezone
from hashlib import sha256

from botocore.exceptions import ClientError

from services.document_storage_service import publish_record, source_binding, storage_path
from services.task_service import plan_task, task_summary
from services.validation_service import path_document_identity as document_identity
from services.validation_service import validate_relative_path

LAYOUT = 'project-path-v1'


def store_path_document(payload: dict, document: dict, s3, table, bucket: str) -> dict:
    """Overwrite one canonical path, retaining stable identity and independently edited tasks."""
    username = payload['username']
    source = document['relative_path']
    relative = validate_relative_path(source)
    file_id, s3_key = document_identity(username, source)
    key = {'username': username, 'file_id': file_id}
    existing = table.get_item(Key=key, ConsistentRead=True).get('Item')

    if existing and (existing.get('upload_token') or existing.get('status') != 'READY'):
        raise ValueError('An earlier upload is unresolved; inspect before replacing')
    binding = source_binding(source, document.get('metadata', {}))

    if existing and 's3_path' not in existing:
        if existing.get('source_path') != source or existing.get('metadata', {}).get(
            'project_id'
        ) != document.get('metadata', {}).get('project_id'):
            raise ValueError('Another source already occupies this canonical path')
    content = document['content'].encode('utf-8')
    digest = sha256(content).hexdigest()
    group = document.get(
        'file-group', payload.get('file-group', document.get('category', 'workflow-docs'))
    )
    options = {
        'Bucket': bucket,
        'Key': s3_key,
        'Body': content,
        'ContentType': 'text/markdown; charset=utf-8',
        'ServerSideEncryption': 'AES256',
        'Metadata': {'source-binding': binding},
    }

    if existing:
        head = s3.head_object(Bucket=bucket, Key=s3_key)

        if head.get('Metadata', {}).get('source-binding', binding) != binding:
            raise ValueError('Another source already occupies this canonical path')
        options['IfMatch'] = head['ETag']
    else:
        options['IfNoneMatch'] = '*'

    try:
        s3.put_object(**options)
    except ClientError as error:
        if existing or error.response['Error']['Code'] not in {'PreconditionFailed', '412'}:
            raise
        # A definite precondition failure wrote nothing. Inspect an existing object
        # before allowing an explicit retry to repair a missing index acknowledgement.
        head = s3.head_object(Bucket=bucket, Key=s3_key)

        metadata = head.get('Metadata', {})
        legacy_raw = (
            payload.get('storage_layout') != LAYOUT
            and not metadata.get('source-binding')
            and not metadata.get('upload-token')
        )

        if metadata.get('source-binding') != binding and not legacy_raw:
            raise ValueError('Existing object belongs to another source') from None

        options.pop('IfNoneMatch')

        s3.put_object(**options, IfMatch=head['ETag'])
    record = {
        **key,
        's3_path': storage_path(bucket, s3_key),
        'file-group': group,
        'created_at': existing['created_at']
        if existing
        else datetime.now(timezone.utc).isoformat(),
        'status': 'READY',
    }
    task = plan_task(document, username, record['created_at'])

    if task is not None:
        record['task'] = task
    stored = publish_record(table, record, existing)
    result = {
        'name': document['name'],
        'file_id': file_id,
        'status': 'READY',
        'storage_layout': LAYOUT,
        'relative_path': relative,
        'sha256': digest,
        's3_key': s3_key,
    }

    if isinstance(stored.get('task'), dict):
        result['task'] = task_summary(stored, username)

    return result
