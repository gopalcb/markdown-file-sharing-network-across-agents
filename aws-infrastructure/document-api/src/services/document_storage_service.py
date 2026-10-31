"""Translate compact index records to owner-scoped S3 locations and publish metadata."""

from hashlib import sha256
from urllib.parse import quote, unquote, urlsplit

from services.validation_service import validate_relative_path

FIELDS = {'username', 'file_id', 's3_path', 'file-group', 'created_at', 'status', 'task'}


def source_binding(source_path: str, metadata: dict) -> str:
    """Bind an occupied canonical path to its original source/project without storing raw metadata."""

    return sha256((str(metadata.get('project_id', '')) + '\n' + source_path).encode()).hexdigest()


def storage_path(bucket: str, key: str) -> str:
    """Build a plain S3 URI without revision or query parameters."""

    return f's3://{bucket}/{quote(key, safe="/")}'


def storage_location(record: dict, bucket: str, legacy_bucket: str | None = None) -> dict:
    """Resolve compact or legacy rows, rejecting foreign buckets and owner prefixes."""

    if 's3_path' in record:
        uri = urlsplit(record['s3_path'])

        if uri.scheme != 's3' or uri.query or uri.fragment or not uri.netloc:
            raise ValueError('Invalid stored S3 path')
        stored_bucket, key = uri.netloc, unquote(uri.path.lstrip('/'))
    else:
        stored_bucket, key = record['s3_bucket'], record['s3_key']
    owner = record['username']
    prefix = f'agent-work/raw-markdown/{owner}/'
    legacy_prefix = f'docs/user-docs/{owner}/'

    if key.startswith(prefix) and stored_bucket == bucket:
        relative = key[len(prefix) :]
    elif key.startswith(legacy_prefix) and stored_bucket == (legacy_bucket or bucket):
        relative = key[len(legacy_prefix) :]
    else:
        raise ValueError('Invalid stored document location')

    # Prefix checks alone must not admit empty, traversal or ambiguous paths.
    validate_relative_path(relative)

    return {'Bucket': stored_bucket, 'Key': key}


def publish_record(table, record: dict, previous: dict | None = None) -> dict:
    """Publish acknowledged bytes, preserving concurrent human task edits and creation date."""
    fields = {
        key: value for key, value in record.items() if key in FIELDS - {'username', 'file_id'}
    }
    names, values, assignments = {}, {}, []

    for index, (name, value) in enumerate(fields.items()):
        alias, parameter = f'#f{index}', f':f{index}'
        names[alias], values[parameter] = name, value
        expression = (
            f'if_not_exists({alias}, {parameter})' if name in {'task', 'created_at'} else parameter
        )

        assignments.append(f'{alias} = {expression}')
    removals = []

    for index, name in enumerate(sorted(set(previous or {}) - FIELDS)):
        alias = f'#old{index}'
        names[alias] = name

        removals.append(alias)
    expression = 'SET ' + ', '.join(assignments)

    if removals:
        expression += ' REMOVE ' + ', '.join(removals)
    response = table.update_item(
        Key={'username': record['username'], 'file_id': record['file_id']},
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues='ALL_NEW',
    )

    return response['Attributes']
