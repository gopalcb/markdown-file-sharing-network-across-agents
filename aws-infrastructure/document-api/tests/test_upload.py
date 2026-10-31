"""Exercise validation, persistence ordering and failures without AWS access."""

import base64
import json
import sys
from copy import deepcopy
from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from device_fixtures import approval_for_event, fixture_database, sign_event
from session_device_service import session_record

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import app
from services.document_service import store_documents
from services.validation_service import validate_event

# A refresh family retains its original authentication time across every request.
AUTH_TIME = int(time())


def configure_storage(s3, table) -> dict:
    """Model latest-only object lookup and compact metadata publication for boundary tests."""
    rows = {}
    table.get_item.side_effect = lambda Key, ConsistentRead: (
        {'Item': rows[Key['file_id']]} if Key['file_id'] in rows else {}
    )
    s3.head_object.return_value = {'ETag': '"fixture-etag"'}
    s3.put_object.return_value = {}

    def update_item(
        Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues, ReturnValues
    ):
        """Return persisted compact attributes while retaining any established creation date."""
        assert ReturnValues == 'ALL_NEW'
        row = rows.setdefault(Key['file_id'], dict(Key))

        for alias, field in ExpressionAttributeNames.items():
            if alias.startswith('#old'):
                row.pop(field, None)
            elif field in {'created_at', 'task'}:
                row.setdefault(field, ExpressionAttributeValues[':' + alias[1:]])
            else:
                row[field] = ExpressionAttributeValues[':' + alias[1:]]

        return {'Attributes': dict(row)}

    table.update_item.side_effect = update_item

    return rows


@pytest.fixture
def payload() -> dict:
    """Supply a valid document with all supported metadata scalar types."""

    return {
        'username': 'alice',
        'files': [
            {
                'name': 'notes.md',
                'content': '# Notes\nHello 🌍',
                'category': 'knowledge',
                'file-group': 'knowledge',
                'metadata': {'type': 'learning', 'reviewed': False, 'revision': 1, 'source': None},
            }
        ],
    }


def event_for(payload: dict) -> dict:
    """Create a realistic HTTP API proxy event."""

    return sign_event(
        {
            'rawPath': '/documents',
            'headers': {
                'authorization': 'Bearer fixture-token',
                'content-type': 'application/json',
                'x-device-id': '11111111-1111-4111-8111-111111111111',
                'x-device-mac': '02:11:22:33:44:55',
            },
            'body': json.dumps(payload),
            'requestContext': {
                'http': {'method': 'POST'},
                'authorizer': {
                    'jwt': {
                        'claims': {
                            'token_use': 'access',
                            'username': 'alice',
                            'sub': 'fixture-user',
                            'iss': 'fixture-issuer',
                            'client_id': 'fixture-client',
                            'origin_jti': 'fixture-family',
                            'auth_time': AUTH_TIME,
                        }
                    }
                },
            },
        }
    )


def test_valid_base64_and_utf8(payload: dict) -> None:
    """Both API Gateway body encodings preserve document content."""
    event = event_for(payload)
    assert validate_event(event) == payload

    event.update(body=base64.b64encode(event['body'].encode()).decode(), isBase64Encoded=True)
    assert validate_event(event) == payload


@pytest.mark.parametrize(
    'field,value',
    [
        ('name', '../escape.md'),
        ('name', 'a.txt'),
        ('name', 'a/b.md'),
        ('category', '../private'),
        ('content', 4),
        ('content', 'x' * (256 * 1024 + 1)),
        ('metadata', {'score': 0.5}),
        ('metadata', {'nested': {}}),
        ('metadata', {'bad': float('nan')}),
        ('metadata', {'big': 'x' * 17000}),
        ('content', '\ud800'),
    ],
)
def test_invalid_file_rejected(payload: dict, field: str, value) -> None:
    """Reject unsafe paths, invalid data and oversized content before persistence."""
    payload['files'][0][field] = value

    with pytest.raises(ValueError):
        validate_event(event_for(payload))


@pytest.mark.parametrize('body', ['null', '[]', '{', '{}', '"text"'])
def test_malformed_request(body: str) -> None:
    """Non-object and malformed request bodies never pass validation."""

    with pytest.raises(ValueError):
        validate_event({'headers': {'content-type': 'application/json'}, 'body': body})


def test_batch_limits_and_username(payload: dict) -> None:
    """Reject duplicate names, unbounded batches and path-like usernames."""

    for change in (
        {'files': []},
        {'files': payload['files'] * 11},
        {'files': payload['files'] * 2},
        {'username': '../bob'},
    ):
        with pytest.raises(ValueError):
            validate_event(event_for({**payload, **change}))

    with pytest.raises(ValueError):
        validate_event(
            {'headers': {'content-type': 'application/json'}, 'body': ' ' * (1024 * 1024 + 1)}
        )


def test_batch_storage_and_order(payload: dict) -> None:
    """Distinct files get stable identities and become READY only after acknowledged writes."""
    second = deepcopy(payload['files'][0])
    second['name'] = 'second.md'

    payload['files'].append(second)
    operations = Mock()
    s3, table = operations.s3, operations.table
    rows = configure_storage(s3, table)
    results = store_documents(payload, s3, table, 'bucket', 'request')
    assert [item['status'] for item in results] == ['READY', 'READY']
    assert results[0]['file_id'] != results[1]['file_id']
    assert [call[0] for call in operations.mock_calls] == [
        'table.get_item',
        's3.put_object',
        'table.update_item',
    ] * 2
    record = rows[results[0]['file_id']]
    assert set(record) == {'username', 'file_id', 's3_path', 'file-group', 'created_at', 'status'}
    assert record['status'] == 'READY'
    assert record['s3_path'] == 's3://bucket/agent-work/raw-markdown/alice/knowledge/notes.md'
    assert record['file-group'] == 'knowledge'

    table.put_item.assert_not_called()
    assert s3.put_object.call_args_list[0].kwargs['Body'] == payload['files'][0]['content'].encode()
    assert s3.put_object.call_args.kwargs['IfNoneMatch'] == '*'


@pytest.mark.parametrize('failure', ['index', 'object', 'ready'])
def test_failure_never_reports_ready(payload: dict, failure: str) -> None:
    """Every persistence boundary leaves an explicit incomplete result on error."""
    s3, table = Mock(), Mock()

    configure_storage(s3, table)
    error = ClientError({'Error': {'Code': 'InternalError', 'Message': 'secret detail'}}, 'write')

    if failure == 'index':
        table.get_item.side_effect = error
    elif failure == 'ready':
        table.update_item.side_effect = error
    else:
        s3.put_object.side_effect = error
    results = store_documents(payload, s3, table, 'bucket', 'request')
    assert results[0]['status'] == 'INCOMPLETE'
    assert 'secret' not in json.dumps(results)

    if failure != 'ready':
        table.update_item.assert_not_called()

    if failure == 'index':
        s3.put_object.assert_not_called()


def test_handler_validates_entire_batch_before_aws(payload: dict, monkeypatch) -> None:
    """Invalid later files prevent earlier files from being written."""

    payload['files'].append({'name': 'invalid.md'})
    session = Mock()
    session.client.return_value.put_object.return_value = {}

    monkeypatch.setattr(app.boto3, 'Session', session)
    response = app.lambda_handler(event_for(payload), SimpleNamespace(aws_request_id='request'))
    assert response['statusCode'] == 400

    session.assert_not_called()


def test_handler_partial_failure_and_success(payload: dict, monkeypatch) -> None:
    """The HTTP boundary distinguishes partial results and fully persisted batches."""

    monkeypatch.setenv('USER_FILES_TABLE', 'table')

    monkeypatch.setenv('DOCUMENTS_BUCKET', 'bucket')
    session = Mock()
    session.client.return_value.put_object.return_value = {}

    monkeypatch.setattr(app.boto3, 'Session', lambda: session)
    event = event_for(payload)
    database, rows = fixture_database()
    session.resource.return_value = database

    configure_storage(session.client.return_value, database.Table('table'))
    approval = approval_for_event(event)

    database.Table('softrobots-native-device-approvals').put_item(Item=approval)
    record = session_record(
        event['requestContext']['authorizer']['jwt']['claims'],
        event['headers']['x-device-id'],
        event['headers']['x-device-mac'],
        native=True,
    )

    database.Table('softrobots-auth-sessions').put_item(Item=record)
    response = app.lambda_handler(event, SimpleNamespace(aws_request_id='request'))
    assert response['statusCode'] == 201
    session.client.return_value.put_object.side_effect = ClientError(
        {'Error': {'Code': 'Denied'}}, 'put'
    )
    response = app.lambda_handler(event_for(payload), SimpleNamespace(aws_request_id='request'))
    assert response['statusCode'] == 207


def test_partial_batch_continues(payload: dict) -> None:
    """A failed file does not hide the result of a later successful file."""
    second = deepcopy(payload['files'][0])
    second['name'] = 'other.md'

    payload['files'].append(second)
    s3, table = Mock(), Mock()

    configure_storage(s3, table)
    s3.put_object.side_effect = [
        ClientError({'Error': {'Code': 'Denied'}}, 'put'),
        {},
    ]
    results = store_documents(payload, s3, table, 'bucket', 'request')
    assert [item['status'] for item in results] == ['INCOMPLETE', 'READY']
    assert table.update_item.call_count == 1


def test_utf16_body_rejected(payload: dict) -> None:
    """Base64 encoding does not permit non-UTF-8 JSON bodies."""
    event = event_for(payload)

    event.update(
        body=base64.b64encode(json.dumps(payload).encode('utf-16')).decode(), isBase64Encoded=True
    )

    with pytest.raises(ValueError):
        validate_event(event)
