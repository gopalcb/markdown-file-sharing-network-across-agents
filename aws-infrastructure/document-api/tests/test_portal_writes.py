"""Cover browser writes, ETag conflicts and linked native plan tasks offline."""

import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import app
import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from services.document_service import store_documents
from services.portal_document_service import list_folders, replace_document
from services.portal_validation_service import validate_portal_event
from services.task_service import DocumentConflict, get_task, list_tasks, plan_task, update_task

FILE_ID = 'b1cdfdcf-1a9c-4f25-9387-d185132abb17'


@pytest.fixture
def aws_storage():
    """Exercise actual storage contracts using isolated offline AWS services."""

    with mock_aws():
        table = boto3.resource('dynamodb', region_name='us-east-1').create_table(
            TableName='files',
            KeySchema=[
                {'AttributeName': 'username', 'KeyType': 'HASH'},
                {'AttributeName': 'file_id', 'KeyType': 'RANGE'},
            ],
            AttributeDefinitions=[
                {'AttributeName': 'username', 'AttributeType': 'S'},
                {'AttributeName': 'file_id', 'AttributeType': 'S'},
            ],
            BillingMode='PAY_PER_REQUEST',
        )
        s3 = boto3.client('s3', region_name='us-east-1')

        s3.create_bucket(Bucket='bucket')
        yield Mock(wraps=table), Mock(wraps=s3)


def upload() -> dict:
    """Supply a valid browser upload with plan metadata."""

    return {
        'operation': 'upload_documents',
        'username': 'alice',
        'upload': {
            'file-group': 'workflow-docs',
            'files': [
                {
                    'name': 'example-plan.md',
                    'content': '# Example\n\n- Do work',
                    'metadata': {'project': 'Website', 'assigned_to': 'agent-one'},
                }
            ],
        },
    }


def command(operation: str, changes=None) -> dict:
    """Build an owner-bound detail command."""
    value = {
        'operation': operation,
        'username': 'alice',
        'owner': 'alice',
        'file_id': FILE_ID,
        'scope': 'mine',
    }

    if changes is not None:
        value['changes'] = changes

    return value


def ready_record() -> dict:
    """Provide a legacy READY plan to verify compact migration preserves its task."""

    return {
        'username': 'alice',
        'file_id': FILE_ID,
        'filename': 'example-plan.md',
        'category': 'workflow-docs',
        'file-group': 'workflow-docs',
        'status': 'READY',
        'created_at': '2026-10-30T00:00:00Z',
        'size_bytes': 6,
        'sha256': 'a' * 64,
        's3_bucket': 'bucket',
        's3_key': 'agent-work/raw-markdown/alice/workflow-docs/example-plan.md',
        's3_version_id': 'v1',
        'storage_layout': 'raw-markdown-v2',
        'metadata': {},
        'task': {
            'title': 'Example',
            'project': 'Website',
            'assigned_to': 'agent-one',
            'status': 'pending',
            'priority': 'Medium',
            'due': '',
            'created_at': '2026-10-30T00:00:00Z',
            'revision': 1,
        },
    }


@pytest.mark.parametrize(
    'change',
    [
        {'requestContext': {}},
        {'username': '../alice'},
        {'upload': {'files': [], 'username': 'bob'}},
        {'upload': {'files': [], 's3_key': 'other'}},
    ],
)
def test_untrusted_or_cross_owner_upload_never_reaches_aws(change, monkeypatch) -> None:
    """The public gateway cannot invoke trusted operations or choose upload identity."""
    session = Mock()

    monkeypatch.setattr(app.boto3, 'Session', session)
    result = app.lambda_handler({**upload(), **change}, SimpleNamespace(aws_request_id='r'))
    assert result['statusCode'] == 400

    session.assert_not_called()


@pytest.mark.parametrize(
    'name,content,group',
    [
        ('../x.md', 'ok', 'docs'),
        ('x.txt', 'ok', 'docs'),
        ('x.md', 'a' * (262144 + 1), 'docs'),
        ('x.md', 'ok', '../other'),
    ],
)
def test_upload_limits_and_partition_paths(name, content, group) -> None:
    """All filename, UTF-8 size and partition validation precedes storage."""
    event = upload()
    event['upload']['file-group'] = group

    event['upload']['files'][0].update(name=name, content=content)

    with pytest.raises(ValueError):
        validate_portal_event(event)


def test_browser_and_native_storage_create_linked_plan_once(aws_storage) -> None:
    """Stable uploads publish compact rows and preserve independently edited tasks."""
    payload = validate_portal_event(upload())['upload']
    table, s3 = aws_storage
    result = store_documents(payload, s3, table, 'bucket', 'r')
    key = {'username': 'alice', 'file_id': result[0]['file_id']}
    record = table.get_item(Key=key, ConsistentRead=True)['Item']
    assert set(record) == {
        'username',
        'file_id',
        's3_path',
        'file-group',
        'created_at',
        'status',
        'task',
    }
    assert UUID(record['file_id']).version == 5
    assert record['task']['project'] == 'Website'
    assert record['task']['assigned_to'] == 'agent-one'
    assert record['task']['title'] == 'Example'
    assert 'description' not in record['task']
    assert result[0]['task']['id'] == record['file_id']
    assert result[0]['status'] == 'READY'
    assert table.update_item.call_args.kwargs['ReturnValues'] == 'ALL_NEW'
    assert s3.put_object.call_args.kwargs['IfNoneMatch'] == '*'

    s3.head_object.assert_not_called()
    assert s3.put_object.call_args.kwargs['Key'] == (
        'agent-work/raw-markdown/alice/workflow-docs/example-plan.md'
    )
    edited_task = {**record['task'], 'title': 'Human title', 'status': 'done', 'revision': 2}

    table.update_item(
        Key=key,
        UpdateExpression='SET task = :task',
        ExpressionAttributeValues={':task': edited_task},
    )
    repeated = store_documents(payload, s3, table, 'bucket', 'r-again')
    current = table.get_item(Key=key, ConsistentRead=True)['Item']
    assert repeated[0]['file_id'] == record['file_id']
    assert repeated[0]['task']['title'] == 'Human title'
    assert current['task'] == edited_task
    assert current['created_at'] == record['created_at']
    assert isinstance(s3.put_object.call_args.kwargs['IfMatch'], str)

    table.put_item.assert_not_called()
    document = {
        'name': 'notes.md',
        'content': '---\nkind: plan\nproject: Backend\nassigned_to: codex\n---\n# Plan',
        'metadata': {},
    }
    assert plan_task(document, 'alice', 'now')['assigned_to'] == 'codex'
    assert (
        plan_task({'name': 'notes.md', 'content': '# Notes', 'metadata': {}}, 'alice', 'now')
        is None
    )


def test_upload_without_index_preserves_existing_s3_object(aws_storage) -> None:
    """An absent index row never authorizes overwriting token-bound native upload intent."""
    table, s3 = aws_storage
    key = 'agent-work/raw-markdown/alice/workflow-docs/example-plan.md'

    s3.put_object(
        Bucket='bucket', Key=key, Body=b'# Existing', Metadata={'upload-token': 'native-intent'}
    )
    result = store_documents(validate_portal_event(upload())['upload'], s3, table, 'bucket', 'r')
    assert result[0]['status'] == 'INCOMPLETE'
    assert 'task' not in result[0]
    assert s3.put_object.call_args.kwargs['IfNoneMatch'] == '*'
    stored = s3.get_object(Bucket='bucket', Key=key)

    try:
        assert stored['Body'].read() == b'# Existing'
    finally:
        stored['Body'].close()

    table.update_item.assert_not_called()

    table.put_item.assert_not_called()

    s3.head_object.assert_called_once_with(Bucket='bucket', Key=key)
    assert s3.put_object.call_count == 2


def test_incomplete_plan_never_returns_ready_task() -> None:
    """Failed S3 writes publish no index row or success task receipt."""
    s3, table = Mock(), Mock()
    table.get_item.return_value = {}
    s3.put_object.side_effect = ClientError({'Error': {'Code': 'AccessDenied'}}, 'PutObject')
    result = store_documents(validate_portal_event(upload())['upload'], s3, table, 'bucket', 'r')
    assert result[0]['status'] == 'INCOMPLETE'
    assert 'task' not in result[0]

    table.update_item.assert_not_called()

    table.put_item.assert_not_called()


def test_folder_browse_is_bounded_and_owner_partitioned() -> None:
    """Folder listing queries only the verified partition and offers safe group names."""
    table = Mock()
    table.query.return_value = {'Items': [{'category': 'custom'}, {'category': '../bad'}]}
    result = list_folders({'username': 'alice'}, table)
    assert {item['name'] for item in result['folders']} == {
        'workflow-docs',
        'worksummary',
        'knowledge',
        'custom',
    }
    assert all(
        item['prefix'].startswith('agent-work/raw-markdown/alice/') for item in result['folders']
    )
    assert table.query.call_args.kwargs['Limit'] == 100
    assert table.query.call_args.kwargs['ExpressionAttributeValues'] == {':username': 'alice'}


@pytest.mark.parametrize('code', ['PreconditionFailed', 'ConditionalRequestConflict', '412', '409'])
def test_replace_stale_etag_and_cross_owner_rejected(code) -> None:
    """S3 rejects stale ETags atomically without publishing index metadata."""
    table, s3 = Mock(), Mock()
    record = ready_record()
    table.get_item.return_value = {'Item': record}
    s3.head_object.return_value = {'ETag': '"current"', 'Metadata': {}}
    s3.put_object.side_effect = ClientError({'Error': {'Code': code}}, 'PutObject')
    payload = command('replace_document', {'content': '# Changed', 'expected_etag': '"old"'})

    with pytest.raises(DocumentConflict):
        replace_document(payload, table, s3, 'bucket')
    assert s3.put_object.call_args.kwargs['IfMatch'] == '"old"'
    assert s3.put_object.call_args.kwargs['Key'] == record['s3_key']

    table.update_item.assert_not_called()

    s3.get_object.assert_not_called()

    with pytest.raises(PermissionError):
        validate_portal_event({**payload, 'username': 'bob'})


def test_replace_uses_conditional_publish_and_preserves_task(aws_storage) -> None:
    """A guarded replacement removes legacy fields without rewriting human tasks."""
    table, s3 = aws_storage
    record = ready_record()

    table.put_item(Item=record)
    original = s3.put_object(
        Bucket='bucket', Key=record['s3_key'], Body=b'# Plan', Metadata={'source-binding': 'bound'}
    )
    result = replace_document(
        command('replace_document', {'content': '# Changed', 'expected_etag': original['ETag']}),
        table,
        s3,
        'bucket',
    )
    current = table.get_item(Key={'username': 'alice', 'file_id': FILE_ID})['Item']
    assert set(current) == {
        'username',
        'file_id',
        's3_path',
        'file-group',
        'created_at',
        'status',
        'task',
    }
    assert current['task'] == record['task']
    assert current['created_at'] == record['created_at']
    call = table.update_item.call_args.kwargs
    assert call['ReturnValues'] == 'ALL_NEW'
    assert 'task' not in call['ExpressionAttributeNames'].values()
    assert s3.put_object.call_args.kwargs['IfMatch'] == original['ETag']
    assert s3.put_object.call_args.kwargs['Metadata'] == {'source-binding': 'bound'}
    assert result['document']['content'] == '# Changed'


def test_task_detail_uses_markdown_and_update_keeps_source() -> None:
    """Task detail reads current plan Markdown; independent edits use a revision guard."""
    record = ready_record()
    table, s3 = Mock(), Mock()
    table.get_item.return_value = {'Item': record}
    s3.get_object.return_value = {'Body': io.BytesIO(b'# Plan'), 'ContentLength': 6}
    detail = get_task(command('get_task'), table, s3, 'bucket', None)
    assert detail['task']['description'] == '# Plan'

    def publish(**kwargs):
        """Model the acknowledged task update for the follow-up consistent read."""
        record['task'] = kwargs['ExpressionAttributeValues'][':task']

    table.update_item.side_effect = publish
    result = update_task(
        command(
            'update_task', {'expected_revision': 1, 'status': 'done', 'description': '# Task notes'}
        ),
        table,
        s3,
        'bucket',
        None,
    )
    assert result['task']['description'] == '# Task notes'
    assert result['task']['revision'] == 2
    assert (
        table.update_item.call_args.kwargs['ConditionExpression']
        == '#status = :ready AND task.revision = :expected'
    )

    s3.put_object.assert_not_called()

    with pytest.raises(DocumentConflict):
        update_task(
            command('update_task', {'expected_revision': 1, 'status': 'pending'}),
            table,
            s3,
            'bucket',
            None,
        )


def test_task_listing_excludes_incomplete_and_does_not_duplicate() -> None:
    """Repeated reads project existing tasks without creating records or reading S3."""
    table = Mock()
    record = ready_record()
    table.scan.return_value = {'Items': [record, {**record, 'status': 'PENDING'}]}
    payload = {'scope': 'all', 'username': 'bob'}
    result = list_tasks(payload, table)
    assert len(result['tasks']) == 1 and not result['tasks'][0]['editable']
    assert list_tasks(payload, table) == result

    table.put_item.assert_not_called()


def test_successful_revision_returns_new_etag_and_exact_markdown(aws_storage) -> None:
    """A save returns the current ETag and UTF-8 byte length read back from S3."""
    table, s3 = aws_storage
    record = ready_record()

    table.put_item(Item=record)
    original = s3.put_object(Bucket='bucket', Key=record['s3_key'], Body=b'# Plan')
    content = '# Revised\n\n**Markdown café**'
    result = replace_document(
        command('replace_document', {'content': content, 'expected_etag': original['ETag']}),
        table,
        s3,
        'bucket',
    )
    assert result['document']['content'] == content
    assert result['document']['etag'] != original['ETag']
    assert (
        result['document']['etag'] == s3.head_object(Bucket='bucket', Key=record['s3_key'])['ETag']
    )
    assert result['document']['size_bytes'] == len(content.encode('utf-8'))
    assert result['document']['editable']
    assert 'version' not in result['document']

    s3.get_object.assert_called_once_with(Bucket='bucket', Key=record['s3_key'])


def test_private_upload_lambda_dispatch_creates_task_receipt(monkeypatch, aws_storage) -> None:
    """A trusted portal command reaches the same storage coordinator without native claims."""
    session = Mock()
    table, s3 = aws_storage
    session.client.return_value = s3
    session.resource.return_value.Table.return_value = table

    monkeypatch.setattr(app.boto3, 'Session', Mock(return_value=session))

    monkeypatch.setenv('DOCUMENTS_BUCKET', 'bucket')

    monkeypatch.setenv('USER_FILES_TABLE', 'files')
    result = app.lambda_handler(upload(), SimpleNamespace(aws_request_id='r'))
    assert result['statusCode'] == 201
    body = json.loads(result['body'])
    assert body['files'][0]['task']['assigned_to'] == 'agent-one'
    assert body['files'][0]['task']['id'] == body['files'][0]['file_id']
