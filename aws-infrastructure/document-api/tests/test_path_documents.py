"""Exercise latest-only uploads against emulated S3 and DynamoDB service behavior."""

from hashlib import sha256
from unittest.mock import Mock
from uuid import UUID

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from services.document_read_service import get_document, list_documents
from services.document_service import store_documents
from services.path_document_service import document_identity
from services.portal_document_service import replace_document
from services.task_service import DocumentConflict
from services.validation_service import validate_event, validate_relative_path
from test_upload import event_for


def payload(path: str = 'docs/tasks/example/plan.md', content: str = '# Plan\n') -> dict:
    """Create a path upload with source-project ownership metadata."""

    return {
        'username': 'alice',
        'storage_layout': 'project-path-v1',
        'file-group': 'workflow-docs',
        'files': [
            {
                'name': path.rsplit('/', 1)[-1],
                'relative_path': path,
                'content': content,
                'metadata': {'project_id': 'project-a', 'relative_path': path},
            }
        ],
    }


@pytest.fixture
def storage():
    """Use unversioned S3 and real SDK expression serialization through Moto."""

    with mock_aws():
        s3 = boto3.client('s3', region_name='us-east-1')

        s3.create_bucket(Bucket='documents')
        table = boto3.resource('dynamodb', region_name='us-east-1').create_table(
            TableName='files',
            BillingMode='PAY_PER_REQUEST',
            KeySchema=[
                {'AttributeName': name, 'KeyType': kind}
                for name, kind in [('username', 'HASH'), ('file_id', 'RANGE')]
            ],
            AttributeDefinitions=[
                {'AttributeName': name, 'AttributeType': 'S'} for name in ['username', 'file_id']
            ],
        )
        yield table, s3


def test_same_path_reuses_document_and_task_and_updates_bytes(storage):
    """Unversioned repeated uploads retain one compact row and edited work item."""
    table, s3 = storage
    first = store_documents(payload(), s3, table, 'documents', 'r')[0]
    assert first['status'] == 'READY' and UUID(first['file_id']).version == 5
    key = {'username': 'alice', 'file_id': first['file_id']}
    row = table.get_item(Key=key)['Item']
    assert set(row) == {
        'username',
        'file_id',
        's3_path',
        'file-group',
        'created_at',
        'status',
        'task',
    }
    assert 's3_version_id' not in first

    table.update_item(
        Key=key,
        UpdateExpression='SET task.#s = :done, task.revision = :revision',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':done': 'done', ':revision': 4},
    )
    second = store_documents(payload(content='# Changed plan\n'), s3, table, 'documents', 'r')[0]
    assert second['file_id'] == first['file_id'] and second['task']['status'] == 'done'
    assert second['task']['revision'] == 4
    assert table.scan()['Count'] == 1
    obj = s3.get_object(Bucket='documents', Key=first['s3_key'])
    assert obj['Body'].read() == b'# Changed plan\n'
    assert 'VersionId' not in obj
    assert second['sha256'] == sha256(b'# Changed plan\n').hexdigest()


def test_bulk_paths_partial_failure_and_retry(storage):
    """Distinct folders remain distinct and one failed file does not stop the batch."""
    table, s3 = storage
    value = payload('docs/one/plan.md')
    value['files'] += payload('docs/two/plan.md')['files']
    real_put = s3.put_object

    def fail_one(**kwargs):
        """Fail one object write without mutating it."""

        if '/one/' in kwargs['Key']:
            raise ClientError({'Error': {'Code': 'AccessDenied'}}, 'PutObject')

        return real_put(**kwargs)

    s3.put_object = Mock(side_effect=fail_one)
    receipts = store_documents(validate_event(event_for(value)), s3, table, 'documents', 'r')
    assert [item['status'] for item in receipts] == ['INCOMPLETE', 'READY']
    assert table.scan()['Count'] == 1
    s3.put_object = real_put
    receipts = store_documents(value, s3, table, 'documents', 'r')
    assert all(item['status'] == 'READY' for item in receipts)
    assert table.scan()['Count'] == 2


def test_source_alias_and_other_project_cannot_overwrite(storage):
    """The S3 source binding rejects aliases even without duplicate index metadata."""
    table, s3 = storage
    assert store_documents(payload(), s3, table, 'documents', 'r')[0]['status'] == 'READY'
    assert (
        store_documents(payload('tasks/example/plan.md'), s3, table, 'documents', 'r')[0]['status']
        == 'INCOMPLETE'
    )
    other = payload()
    other['files'][0]['metadata']['project_id'] = 'different'
    assert store_documents(other, s3, table, 'documents', 'r')[0]['status'] == 'INCOMPLETE'


def test_index_failure_is_incomplete_and_explicit_retry_repairs(storage):
    """A failed index acknowledgement never returns READY; an explicit retry is stable."""
    table, s3 = storage
    update = table.update_item
    table.update_item = Mock(
        side_effect=ClientError({'Error': {'Code': 'InternalServerError'}}, 'UpdateItem')
    )
    first = store_documents(payload(), s3, table, 'documents', 'r')[0]
    assert first['status'] == 'INCOMPLETE' and table.scan()['Count'] == 0
    table.update_item = update
    second = store_documents(payload(), s3, table, 'documents', 'r')[0]
    assert second['status'] == 'READY' and second['file_id'] == first['file_id']


def test_read_list_and_conditional_edit(storage):
    """Reads use latest bytes; stale browser edits cannot overwrite a newer upload."""
    table, s3 = storage
    first = store_documents(payload(), s3, table, 'documents', 'r')[0]
    query = {'scope': 'mine', 'username': 'alice', 'owner': 'alice', 'file_id': first['file_id']}
    before = get_document(query, table, s3, 'documents')['document']
    assert 'version' not in before and before['size_bytes'] == 7
    listed = list_documents({'scope': 'mine', 'username': 'alice'}, table)['documents']
    assert listed[0]['size_bytes'] is None and listed[0]['filename'] == 'plan.md'
    saved = replace_document(
        {**query, 'changes': {'content': '# New', 'expected_etag': before['etag']}},
        table,
        s3,
        'documents',
    )
    assert saved['document']['content'] == '# New'

    with pytest.raises(DocumentConflict):
        replace_document(
            {**query, 'changes': {'content': '# Stale', 'expected_etag': before['etag']}},
            table,
            s3,
            'documents',
        )
    assert get_document(query, table, s3, 'documents')['document']['content'] == '# New'
    assert document_identity('alice', 'docs/a.md') != document_identity('bob', 'docs/a.md')


@pytest.mark.parametrize(
    'path',
    [
        '/x.md',
        '../x.md',
        'docs/../x.md',
        'docs//x.md',
        'docs/./x.md',
        'x\\y.md',
        'x\ny.md',
        'x/' + 'a' * 256 + '.md',
        'x.txt',
    ],
)
def test_unsafe_paths_rejected_before_storage(path):
    """Reject ambiguous, traversing and unbounded paths before persistence."""

    with pytest.raises(ValueError):
        validate_event(event_for(payload(path)))


def test_unicode_spaces_and_non_docs_roots_preserved():
    """Strip only the agreed docs root while retaining valid Unicode paths."""
    assert validate_relative_path('docs/tasks/My Plan.md') == 'tasks/My Plan.md'
    assert validate_relative_path('doc/café/README.MD') == 'doc/café/README.MD'


@pytest.mark.parametrize(
    'location',
    [
        's3://foreign/agent-work/raw-markdown/alice/x.md',
        's3://documents/agent-work/raw-markdown/bob/x.md',
        's3://documents/agent-work/raw-markdown/alice/%2e%2e/x.md',
        's3://documents/agent-work/raw-markdown/alice/x.md?versionId=old',
    ],
)
def test_compact_location_cannot_redirect_reads(storage, location):
    """Compact URI handling retains bucket, owner and traversal boundaries."""
    table, s3 = storage
    receipt = store_documents(payload(), s3, table, 'documents', 'r')[0]
    key = {'username': 'alice', 'file_id': receipt['file_id']}

    table.update_item(
        Key=key,
        UpdateExpression='SET s3_path = :path',
        ExpressionAttributeValues={':path': location},
    )
    query = {'scope': 'mine', 'username': 'alice', 'owner': 'alice', 'file_id': receipt['file_id']}

    with pytest.raises(ValueError):
        get_document(query, table, s3, 'documents')


def test_legacy_path_edit_compacts_and_keeps_source_binding(storage):
    """Editing a historical path row removes extras without losing its project ownership."""
    table, s3 = storage
    value = payload()
    receipt = store_documents(value, s3, table, 'documents', 'r')[0]
    key = {'username': 'alice', 'file_id': receipt['file_id']}
    row = table.get_item(Key=key)['Item']

    row.pop('s3_path')

    row.update(
        source_path=value['files'][0]['relative_path'],
        relative_path='tasks/example/plan.md',
        storage_layout='project-path-v1',
        filename='plan.md',
        metadata={'project_id': 'project-a'},
        s3_bucket='documents',
        s3_key=receipt['s3_key'],
        s3_version_id='retired-version',
        category='workflow-docs',
        size_bytes=7,
        sha256='unused',
        request_id='old',
    )

    table.put_item(Item=row)

    s3.put_object(Bucket='documents', Key=receipt['s3_key'], Body=b'# Plan\n')
    etag = s3.head_object(Bucket='documents', Key=receipt['s3_key'])['ETag']
    query = {'scope': 'mine', 'username': 'alice', 'owner': 'alice', 'file_id': receipt['file_id']}

    replace_document(
        {**query, 'changes': {'content': '# Edited', 'expected_etag': etag}}, table, s3, 'documents'
    )
    assert set(table.get_item(Key=key)['Item']) == {
        'username',
        'file_id',
        's3_path',
        'file-group',
        'created_at',
        'status',
        'task',
    }
    other = payload()
    other['files'][0]['metadata']['project_id'] = 'other'
    assert store_documents(other, s3, table, 'documents', 'r')[0]['status'] == 'INCOMPLETE'


def test_legacy_raw_group_overwrite_still_works_without_versions(storage):
    """Owner/group uploads may replace old raw objects, but cannot adopt native path intent."""
    table, s3 = storage
    key = 'agent-work/raw-markdown/alice/knowledge/note.md'

    s3.put_object(Bucket='documents', Key=key, Body=b'old raw bytes')
    value = {
        'username': 'alice',
        'file-group': 'knowledge',
        'files': [{'name': 'note.md', 'content': '# Updated', 'metadata': {}}],
    }
    result = store_documents(value, s3, table, 'documents', 'r')[0]
    assert result['status'] == 'READY'
    assert s3.get_object(Bucket='documents', Key=key)['Body'].read() == b'# Updated'

    s3.put_object(
        Bucket='documents',
        Key=key.replace('note.md', 'native.md'),
        Body=b'pending native bytes',
        Metadata={'upload-token': 'old-native-token'},
    )
    value['files'][0]['name'] = 'native.md'
    assert store_documents(value, s3, table, 'documents', 'r')[0]['status'] == 'INCOMPLETE'
    assert (
        s3.get_object(Bucket='documents', Key=key.replace('note.md', 'native.md'))['Body'].read()
        == b'pending native bytes'
    )
