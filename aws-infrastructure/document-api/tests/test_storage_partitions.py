"""Verify canonical partitions, legacy reads and invalid routing without AWS access."""

import io
import json
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import app
import boto3
import pytest
from moto import mock_aws
from services.document_read_service import get_document
from services.document_service import store_documents
from services.validation_service import validate_event
from test_upload import configure_storage, event_for


@pytest.mark.parametrize(
    'category', ['workflow-docs', 'worksummary', 'knowledge', 'custom-group', 'docs']
)
def test_single_document_round_trip(category: str) -> None:
    """Store exact bytes once, then read the recorded new-layout document."""
    payload = {
        'username': 'gopal',
        'file-group': category,
        'files': [
            {
                'name': 'partition-check.md',
                'content': '# Partition check\nUnicode: café\n',
                'category': category,
                'metadata': {},
            }
        ],
    }
    validated = validate_event(event_for(payload))
    operations = Mock()
    table, s3 = operations.table, operations.s3
    rows = configure_storage(s3, table)
    receipt = store_documents(validated, s3, table, 'bucket', 'request')[0]
    record = rows[receipt['file_id']]
    key = f'agent-work/raw-markdown/gopal/{category}/partition-check.md'
    assert receipt['name'] == 'partition-check.md'
    assert receipt['file_id'] == record['file_id']
    assert receipt['status'] == 'READY'
    assert receipt['s3_key'] == key
    assert 's3_version_id' not in receipt
    assert set(record) == {'username', 'file_id', 's3_path', 'file-group', 'created_at', 'status'}
    assert [call[0] for call in operations.mock_calls] == [
        'table.get_item',
        's3.put_object',
        'table.update_item',
    ]
    write = s3.put_object.call_args.kwargs
    assert write['Key'] == key
    assert write['Body'] == payload['files'][0]['content'].encode()
    assert write['ServerSideEncryption'] == 'AES256'
    assert write['IfNoneMatch'] == '*'
    table.get_item.side_effect = None
    table.get_item.return_value = {'Item': record}
    stream = io.BytesIO(write['Body'])
    s3.get_object.return_value = {'Body': stream, 'ContentLength': len(write['Body'])}
    result = get_document(
        {'scope': 'mine', 'username': 'gopal', 'owner': 'gopal', 'file_id': record['file_id']},
        table,
        s3,
        'bucket',
    )
    assert result['document']['content'] == payload['files'][0]['content']
    assert stream.closed

    s3.get_object.assert_called_once_with(Bucket='bucket', Key=key)


@pytest.mark.parametrize('category', ['../knowledge', '', None, [], {}, 'a/b', 'a' * 65])
def test_unknown_categories_rejected_before_storage(category, monkeypatch) -> None:
    """A category cannot create an arbitrary partition or trigger any AWS call."""
    payload = {
        'username': 'gopal',
        'files': [{'name': 'notes.md', 'content': '# Notes', 'category': category, 'metadata': {}}],
    }
    session = Mock()
    session.client.return_value.put_object.return_value = {}

    monkeypatch.setattr(app.boto3, 'Session', session)
    response = app.lambda_handler(event_for(payload), SimpleNamespace(aws_request_id='r'))
    assert response['statusCode'] == 400

    session.assert_not_called()


@pytest.mark.parametrize(
    'change',
    [
        {'category': '../knowledge'},
        {'category': 'unknown'},
        {'s3_key': 'agent-work/raw-markdown/other/knowledge/notes.md'},
        {'username': 'other'},
        {'filename': '../notes.md'},
        {'storage_layout': 'unknown'},
    ],
)
def test_new_record_cannot_redirect_read(change: dict) -> None:
    """Tampered row identity or category fails before S3 access."""
    file_id = 'b1cdfdcf-1a9c-4f25-9387-d185132abb17'
    record = {
        'username': 'gopal',
        'file_id': file_id,
        'filename': 'notes.md',
        'category': 'knowledge',
        'status': 'READY',
        'storage_layout': 'raw-markdown-v1',
        's3_bucket': 'bucket',
        's3_key': f'agent-work/raw-markdown/gopal/knowledge/notes-{file_id}.md',
    }
    table, s3 = Mock(), Mock()
    table.get_item.return_value = {'Item': {**record, **change}}

    with pytest.raises(ValueError):
        get_document(
            {'scope': 'mine', 'username': 'gopal', 'owner': 'gopal', 'file_id': file_id},
            table,
            s3,
            'bucket',
        )

    s3.get_object.assert_not_called()


def test_policy_limits_new_writes_and_preserves_legacy_reads() -> None:
    """Check source policy object grants without claiming live IAM simulation."""
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / 'iam/runtime-policy.json').read_text()
    )
    prefix = 'arn:aws:s3:::agent-work-history--123456789012-ca-central-1/'
    legacy_prefix = 'arn:aws:s3:::softrobots-document-api-123456789012-ca-central-1/'
    grants = {}

    for statement in policy['Statement']:
        actions = statement['Action']
        resources = statement['Resource']

        if isinstance(actions, str):
            actions = [actions]

        if isinstance(resources, str):
            resources = [resources]

        for action in actions:
            if action.startswith('s3:'):
                grants.setdefault((statement['Effect'], action), []).extend(resources)
    new_keys = [
        f'agent-work/raw-markdown/gopal/{category}/notes.md'
        for category in ('workflow-docs', 'worksummary', 'knowledge', 'caller-group')
    ]

    for key in new_keys:
        for action in ('s3:PutObject', 's3:GetObject'):
            assert any(fnmatchcase(prefix + key, pattern) for pattern in grants['Allow', action])
    legacy = 'docs/user-docs/gopal/notes.md'
    assert any(
        fnmatchcase(legacy_prefix + legacy, pattern) for pattern in grants['Allow', 's3:GetObject']
    )

    for key in (
        legacy,
        'docs/category/notes/gopal/notes.md',
        'unrelated/other/notes.md',
        'unrelated/notes.md',
    ):
        assert not any(
            fnmatchcase(prefix + key, pattern) for pattern in grants['Allow', 's3:PutObject']
        )

    for key in new_keys:
        for action in ('s3:DeleteObject', 's3:DeleteObjectVersion'):
            assert any(fnmatchcase(prefix + key, pattern) for pattern in grants['Deny', action])


@pytest.mark.parametrize('group', ['workflow-docs', 'knowledge', 'worksummary', 'Research_2026'])
def test_payload_file_group_and_per_file_override(group: str) -> None:
    """Top-level group applies to all files unless a file explicitly overrides it."""
    payload = {
        'username': 'alice',
        'file-group': group,
        'files': [
            {'name': 'one.md', 'content': '# One', 'metadata': {}},
            {'name': 'two.md', 'content': '# Two', 'metadata': {}, 'file-group': 'special-group'},
        ],
    }
    result = validate_event(event_for(payload))
    assert result['files'][0]['file-group'] == group
    assert result['files'][1]['file-group'] == 'special-group'
    s3, table = Mock(), Mock()
    rows = configure_storage(s3, table)

    store_documents(result, s3, table, 'bucket', 'request')
    assert f'/alice/{group}/' in s3.put_object.call_args_list[0].kwargs['Key']
    assert '/alice/special-group/' in s3.put_object.call_args_list[1].kwargs['Key']


def test_legacy_bucket_read_after_new_bucket_activation() -> None:
    """Old READY records remain readable after the active write bucket changes."""
    file_id = 'b1cdfdcf-1a9c-4f25-9387-d185132abb17'
    key = f'docs/user-docs/alice/notes-{file_id}.md'
    record = {
        'username': 'alice',
        'file_id': file_id,
        'filename': 'notes.md',
        'category': 'old-category',
        'status': 'READY',
        's3_bucket': 'old-bucket',
        's3_key': key,
        'size_bytes': 4,
        'created_at': '2026-10-30',
    }
    table, s3 = Mock(), Mock()
    table.get_item.return_value = {'Item': record}
    s3.get_object.return_value = {'Body': io.BytesIO(b'test')}
    result = get_document(
        {'scope': 'mine', 'username': 'alice', 'owner': 'alice', 'file_id': file_id},
        table,
        s3,
        'new-bucket',
        'old-bucket',
    )
    assert result['document']['content'] == 'test'

    s3.get_object.assert_called_once_with(Bucket='old-bucket', Key=key)


@mock_aws
def test_same_filename_overwrites_one_stable_document() -> None:
    """Repeated uploads retain one compact row and expose the latest unversioned bytes."""
    session = boto3.Session(region_name='us-east-1')
    s3 = session.client('s3')

    s3.create_bucket(Bucket='documents')
    table = session.resource('dynamodb').create_table(
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
    receipts = []
    created_at = None

    for content in ('first content', 'second content'):
        result = store_documents(
            {
                'username': 'gb-agent-user',
                'file-group': 'knowledge',
                'files': [{'name': 'explain-s3-versioning.md', 'content': content, 'metadata': {}}],
            },
            s3,
            table,
            'documents',
            'r',
        )[0]
        assert result['status'] == 'READY'
        assert 's3_version_id' not in result

        receipts.append(result)
        rows = table.scan()['Items']
        assert len(rows) == 1
        assert set(rows[0]) == {
            'username',
            'file_id',
            's3_path',
            'file-group',
            'created_at',
            'status',
        }

        if created_at is None:
            created_at = rows[0]['created_at']
        assert rows[0]['created_at'] == created_at
        document = get_document(
            {
                'scope': 'mine',
                'username': 'gb-agent-user',
                'owner': 'gb-agent-user',
                'file_id': result['file_id'],
            },
            table,
            s3,
            'documents',
        )
        assert document['document']['content'] == content
    assert receipts[0]['file_id'] == receipts[1]['file_id']
    objects = s3.list_objects_v2(Bucket='documents')['Contents']
    assert len(objects) == 1
    assert (
        objects[0]['Key']
        == 'agent-work/raw-markdown/gb-agent-user/knowledge/explain-s3-versioning.md'
    )


@pytest.mark.parametrize(
    'response', [{}, {'VersionId': None}, {'VersionId': ''}, {'VersionId': 'null'}]
)
def test_unversioned_acknowledgement_marks_ready(response: dict) -> None:
    """An acknowledged latest-object write needs no S3 version in its compact row or receipt."""
    s3, table = Mock(), Mock()
    rows = configure_storage(s3, table)
    s3.put_object.return_value = response
    result = store_documents(
        {
            'username': 'gb-agent-user',
            'files': [{'name': 'notes.md', 'content': 'test', 'metadata': {}}],
        },
        s3,
        table,
        'bucket',
        'r',
    )[0]
    assert result['status'] == 'READY'
    assert 's3_version_id' not in result
    assert 's3_version_id' not in rows[result['file_id']]

    table.put_item.assert_not_called()
