"""Deletion reports create owner-bound review rows and never touch S3 or the file index."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4, uuid5

import boto3
import pytest
import yaml
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from device_fixtures import approval_for_event, fixture_database, sign_event
from test_upload import AUTH_TIME, app, event_for

CONTEXT = SimpleNamespace(aws_request_id='delete-observation')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def deletion():
    """Provide immutable metadata for one confirmed local Markdown deletion."""
    project = str(uuid4())

    return {
        'username': 'alice',
        'action': 'report_deleted',
        'deletion': {
            'event_id': str(uuid4()),
            'project_id': project,
            'document_id': str(uuid5(UUID(project), '/nested/note.md')),
            'relative_path': 'nested/note.md',
            'observed_at': '2026-10-27T12:30:00Z',
            'last_file_id': str(uuid4()),
            'known_sha256': 'a' * 64,
            'stored_sha256': 'b' * 64,
            'uncertain_hash': 'c' * 64,
        },
    }


@pytest.fixture
def runtime(monkeypatch):
    """Use real signature validation and stateful conditional DynamoDB doubles."""
    database, rows = fixture_database()
    session = Mock()
    session.resource.return_value = database

    monkeypatch.setattr(app.boto3, 'Session', lambda: session)

    monkeypatch.setenv('DELETIONS_TABLE', 'deletions')

    # Deliberately omit S3/index configuration: reporting must not even require it.
    monkeypatch.delenv('DOCUMENTS_BUCKET', raising=False)

    monkeypatch.delenv('USER_FILES_TABLE', raising=False)
    registration = event_for({'username': 'alice', 'action': 'register_device'})

    database.Table('softrobots-native-device-approvals').put_item(
        Item=approval_for_event(registration)
    )
    assert app.lambda_handler(registration, CONTEXT)['statusCode'] == 200

    session.reset_mock()

    return session, database, rows


def test_record_retry_and_future_review_preserved(runtime, deletion):
    """A lost response retry records once and never resets a later UI keep decision."""
    session, database, rows = runtime
    first = app.lambda_handler(event_for(deletion), CONTEXT)
    assert first['statusCode'] == 200
    receipt = json.loads(first['body'])['deletion']
    assert receipt == {
        **{key: deletion['deletion'][key] for key in ('event_id', 'project_id', 'document_id')},
        'status': 'RECORDED',
    }
    assert len(rows['deletions']) == 1
    item = next(iter(rows['deletions'].values()))
    assert item['review_status'] == 'PENDING_REVIEW'
    assert not {'source', 'request_id', 'device_id'} & set(item)
    assert item['subject'] == 'fixture-user'
    assert item['relative_path'] == 'nested/note.md'
    item['review_status'] = 'KEEP'
    item['reviewed_at'] = 'later'
    assert app.lambda_handler(event_for(deletion), CONTEXT)['statusCode'] == 200
    assert len(rows['deletions']) == 1
    assert item['review_status'] == 'KEEP'

    session.client.assert_not_called()
    assert all(call.args[0] != 'files' for call in database.Table.call_args_list)


@pytest.mark.parametrize(
    'field,value',
    [('known_sha256', 'd' * 64), ('observed_at', '2026-10-28T12:30:00Z'), ('last_file_id', None)],
)
def test_conflicting_retry_never_overwrites_or_touches_s3(
    runtime, deletion, field, value, monkeypatch
):
    """Conflicting event reuse is rejected without changing its original immutable row."""
    session, database, rows = runtime
    assert app.lambda_handler(event_for(deletion), CONTEXT)['statusCode'] == 200
    original = deepcopy(rows['deletions'])

    # Retrying after a clock boundary must not invent a new session auth_time.
    monkeypatch.setattr('test_upload.time', lambda: AUTH_TIME + 2)

    if value is None:
        deletion['deletion'].pop(field)
    else:
        deletion['deletion'][field] = value
    assert app.lambda_handler(event_for(deletion), CONTEXT)['statusCode'] == 409
    assert rows['deletions'] == original

    session.client.assert_not_called()


@pytest.mark.parametrize(
    'field,value',
    [
        ('event_id', 'not-uuid'),
        ('project_id', str(uuid4()).upper()),
        ('document_id', str(uuid4())),
        ('relative_path', '../escape.md'),
        ('relative_path', '/absolute.md'),
        ('relative_path', 'a//b.md'),
        ('relative_path', 'file.txt'),
        ('relative_path', 'bad\n.md'),
        ('relative_path', 'a' * 4096 + '.md'),
        ('observed_at', 'not-a-time'),
        ('observed_at', '2026-10-27T12:30:00+01:00'),
        ('known_sha256', 'bad'),
        ('last_file_id', 'other-owner'),
        ('review_status', 'REMOVE'),
    ],
)
def test_invalid_observation_has_no_aws_calls(monkeypatch, deletion, field, value):
    """Reject hostile paths, mismatched IDs and client-supplied review state before AWS access."""
    deletion['deletion'][field] = value
    session_factory = Mock()

    monkeypatch.setattr(app.boto3, 'Session', session_factory)
    assert app.lambda_handler(event_for(deletion), CONTEXT)['statusCode'] == 400

    session_factory.assert_not_called()


def test_missing_or_revoked_authorization_cannot_record(runtime, deletion):
    """Deletion reporting retains both user ownership and approved native device gates."""
    session, database, rows = runtime
    deletion['username'] = 'bob'
    assert app.lambda_handler(event_for(deletion), CONTEXT)['statusCode'] == 403
    deletion['username'] = 'alice'
    event = event_for(deletion)
    approval = approval_for_event(event, status='REVOKED')

    database.Table('softrobots-native-device-approvals').put_item(Item=approval)
    assert app.lambda_handler(event, CONTEXT)['statusCode'] == 403
    assert 'deletions' not in rows

    session.client.assert_not_called()


def test_database_failure_and_fresh_nonce_retry(runtime, deletion):
    """A request nonce is single use while an event ID remains retryable after DB failure."""
    session, database, rows = runtime
    table = database.Table('deletions')
    original = table.put_item.side_effect
    table.put_item.side_effect = ClientError({'Error': {'Code': 'InternalServerError'}}, 'PutItem')
    event = event_for(deletion)
    assert app.lambda_handler(event, CONTEXT)['statusCode'] == 500
    table.put_item.side_effect = original
    assert app.lambda_handler(event, CONTEXT)['statusCode'] == 403
    assert app.lambda_handler(sign_event(event), CONTEXT)['statusCode'] == 200

    session.client.assert_not_called()


def test_table_and_iam_have_metadata_only_permissions():
    """The retained review table is separate and the runtime explicitly cannot remove S3 objects."""
    template = yaml.load((ROOT / 'template.yaml').read_text(), Loader=yaml.BaseLoader)
    table = template['Resources']['LocalDeletions']
    assert table['DeletionPolicy'] == table['UpdateReplacePolicy'] == 'Retain'
    assert table['Properties']['BillingMode'] == 'PAY_PER_REQUEST'
    assert table['Properties']['KeySchema'] == [
        {'AttributeName': 'owner_id', 'KeyType': 'HASH'},
        {'AttributeName': 'event_id', 'KeyType': 'RANGE'},
    ]
    policy = json.loads((ROOT / 'iam/runtime-policy.json').read_text())['Statement']
    statement = next(item for item in policy if item['Sid'] == 'RecordLocalDeletionObservations')
    assert set(statement['Action']) == {'dynamodb:PutItem', 'dynamodb:GetItem'}
    deny = next(item for item in policy if item['Sid'] == 'NeverDeleteDocumentObjects')
    assert deny['Effect'] == 'Deny'
    assert set(deny['Action']) == {'s3:DeleteObject', 's3:DeleteObjectVersion'}


def test_same_event_id_is_partitioned_by_verified_owner(runtime, deletion):
    """An independently authorized account cannot overwrite the first owner's observation."""
    session, database, rows = runtime
    assert app.lambda_handler(event_for(deletion), CONTEXT)['statusCode'] == 200
    registration = event_for({'username': 'bob', 'action': 'register_device'})
    claims = registration['requestContext']['authorizer']['jwt']['claims']

    claims.update(username='bob', sub='other-user')

    sign_event(registration)

    database.Table('softrobots-native-device-approvals').put_item(
        Item=approval_for_event(registration)
    )
    assert app.lambda_handler(registration, CONTEXT)['statusCode'] == 200
    deletion['username'] = 'bob'
    event = event_for(deletion)

    event['requestContext']['authorizer']['jwt']['claims'].update(username='bob', sub='other-user')
    assert app.lambda_handler(sign_event(event), CONTEXT)['statusCode'] == 200
    assert len(rows['deletions']) == 2
    assert {item['subject'] for item in rows['deletions'].values()} == {
        'fixture-user',
        'other-user',
    }

    session.client.assert_not_called()


def test_deletion_dynamodb_sdk_contract(deletion):
    """Serialize the conditional observation insert without credentials or network access."""
    session = boto3.Session(
        aws_access_key_id='test', aws_secret_access_key='test', region_name='ca-central-1'
    )
    database = session.resource('dynamodb')
    proof = app.validate_native_proof(event_for(deletion))

    with Stubber(database.meta.client) as stub:
        stub.add_response(
            'put_item',
            {},
            {
                'TableName': 'deletions',
                'Item': ANY,
                'ConditionExpression': 'attribute_not_exists(owner_id)',
            },
        )
        assert (
            app.record_deletion(deletion, proof, database.Table('deletions'))['status']
            == 'RECORDED'
        )

        stub.assert_no_pending_responses()
