"""Exercise trusted read authorization, pagination and private content offline."""

import base64
import io
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import app
from services.document_read_service import get_document, list_documents
from services.validation_service import MAX_FILE_BYTES, validate_read_event

FILE_ID = 'b1cdfdcf-1a9c-4f25-9387-d185132abb17'


@pytest.fixture
def record() -> dict:
    """Provide a READY row including fields that must never reach read clients."""

    return {
        'username': 'alice',
        'file_id': FILE_ID,
        'filename': 'notes.md',
        'category': 'learning',
        'created_at': '2026-10-27T00:00:00+00:00',
        'size_bytes': Decimal(4),
        'status': 'READY',
        'metadata': {'secret': 'private'},
        'request_id': 'secret',
        's3_bucket': 'bucket',
        's3_key': f'docs/user-docs/alice/notes-{FILE_ID}.md',
    }


def command(operation: str = 'list_documents', scope: str = 'all') -> dict:
    """Create a trusted direct invocation command."""
    result = {'operation': operation, 'scope': scope, 'username': 'alice'}

    if operation == 'get_document':
        result.update(owner='alice', file_id=FILE_ID)

    return result


@pytest.mark.parametrize(
    'changes',
    [
        {'requestContext': {}},
        {'requestContext': None},
        {'operation': 'delete'},
        {'scope': 'private'},
        {'username': ''},
        {'cursor': None},
        {'cursor': 'x' * 2049},
        {'cursor': 'not-base64'},
        {'body': '{}'},
    ],
)
def test_reject_invalid_read_before_aws(changes: dict, monkeypatch) -> None:
    """Reject HTTP operation dispatch and malformed direct requests before AWS."""
    session = Mock()

    monkeypatch.setattr(app.boto3, 'Session', session)
    response = app.lambda_handler({**command(), **changes}, SimpleNamespace(aws_request_id='r'))
    assert response['statusCode'] == 400

    session.assert_not_called()


@pytest.mark.parametrize('scope', ['all', 'mine'])
def test_bounded_ready_list_and_cursor(record: dict, scope: str) -> None:
    """Use one bounded page, omit private fields and bind continuation to identity."""
    table = Mock()
    key = {'username': 'alice', 'file_id': FILE_ID}
    method = table.scan if scope == 'all' else table.query
    method.return_value = {
        'Items': [record, {**record, 'status': 'PENDING'}],
        'LastEvaluatedKey': key,
    }
    payload = command(scope=scope)
    result = list_documents(payload, table)
    assert len(result['documents']) == 1
    assert set(result['documents'][0]) == {
        'owner',
        'file_id',
        'filename',
        'category',
        'created_at',
        'size_bytes',
        'status',
    }
    assert method.call_count == 1
    assert method.call_args.kwargs['Limit'] == 50
    next_payload = validate_read_event({**payload, 'cursor': result['cursor']})
    assert next_payload['start_key'] == key

    list_documents(next_payload, table)
    assert method.call_args.kwargs['ExclusiveStartKey'] == key

    for changed in ({'scope': 'mine' if scope == 'all' else 'all'}, {'username': 'bob'}):
        with pytest.raises(ValueError):
            validate_read_event({**payload, 'cursor': result['cursor'], **changed})
    method.return_value['Items'] = []
    assert list_documents(payload, table)['cursor']


def test_cursor_cannot_override_mine_owner() -> None:
    """Reject forged mine continuation keys even with a matching cursor envelope."""
    value = {'scope': 'mine', 'username': 'alice', 'key': {'username': 'bob', 'file_id': FILE_ID}}
    cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()

    with pytest.raises(ValueError):
        validate_read_event({**command(scope='mine'), 'cursor': cursor})


@pytest.mark.parametrize('state', ['missing', 'PENDING', 'different-owner'])
def test_get_hidden_documents(record: dict, state: str) -> None:
    """Hide absent, incomplete and other-owner mine documents without S3 reads."""
    table, s3 = Mock(), Mock()
    table.get_item.return_value = (
        {} if state == 'missing' else {'Item': {**record, 'status': state}}
    )
    payload = command('get_document', 'mine')

    if state == 'different-owner':
        payload['owner'] = 'bob'
    assert get_document(payload, table, s3, 'bucket') is None

    s3.get_object.assert_not_called()

    if state == 'different-owner':
        table.get_item.assert_not_called()


@pytest.mark.parametrize(
    'data,length',
    [
        (b'<script>alert(1)</script>', 25),
        (b'x' * (MAX_FILE_BYTES + 1), 0),
        (b'\xff', 1),
        (b'x', MAX_FILE_BYTES + 1),
    ],
)
def test_private_bounded_utf8_and_cleanup(record: dict, data: bytes, length: int) -> None:
    """Return raw text and close the S3 stream on success, size or UTF-8 failures."""
    table, s3 = Mock(), Mock()
    table.get_item.return_value = {'Item': record}
    stream = Mock(wraps=io.BytesIO(data))
    s3.get_object.return_value = {'Body': stream, 'ContentLength': length}

    if length > MAX_FILE_BYTES or len(data) > MAX_FILE_BYTES or data == b'\xff':
        with pytest.raises((ValueError, UnicodeError)):
            get_document(command('get_document'), table, s3, 'bucket')
    else:
        assert (
            get_document(command('get_document'), table, s3, 'bucket')['document']['content']
            == data.decode()
        )

        stream.read.assert_called_once_with(MAX_FILE_BYTES + 1)

    stream.close.assert_called_once()
    assert s3.get_object.call_args.kwargs == {'Bucket': 'bucket', 'Key': record['s3_key']}


def test_read_handler_outcomes(record: dict, monkeypatch) -> None:
    """Map valid reads, missing rows and dependency failures to safe proxy responses."""
    session = Mock()

    monkeypatch.setattr(app.boto3, 'Session', lambda: session)

    monkeypatch.setenv('USER_FILES_TABLE', 'table')

    monkeypatch.setenv('DOCUMENTS_BUCKET', 'bucket')
    table = session.resource.return_value.Table.return_value
    table.scan.return_value = {'Items': [record]}
    context = SimpleNamespace(aws_request_id='r')
    assert app.lambda_handler(command(), context)['statusCode'] == 200
    table.get_item.return_value = {}
    assert app.lambda_handler(command('get_document'), context)['statusCode'] == 404
    table.scan.side_effect = RuntimeError('private AWS details')
    response = app.lambda_handler(command(), context)
    assert response['statusCode'] == 500
    assert 'private' not in response['body']


@pytest.mark.parametrize(
    'changes',
    [
        {'owner': '../bob'},
        {'file_id': '../notes.md'},
        {'file_id': 7},
        {'owner': 'alice', 'cursor': 'ignored'},
    ],
)
def test_get_command_validation(changes: dict) -> None:
    """Reject unsafe identifiers and unexpected continuation fields on get."""

    with pytest.raises(ValueError):
        validate_read_event({**command('get_document'), **changes})


def test_stored_location_and_stream_failure(record: dict) -> None:
    """Block unexpected storage locations and close a stream whose read fails."""
    table, s3 = Mock(), Mock()
    table.get_item.return_value = {'Item': {**record, 's3_bucket': 'other-bucket'}}

    with pytest.raises(ValueError):
        get_document(command('get_document'), table, s3, 'bucket')

    s3.get_object.assert_not_called()
    table.get_item.return_value = {'Item': record}
    stream = Mock()
    stream.read.side_effect = OSError('failed network read')
    s3.get_object.return_value = {'Body': stream, 'ContentLength': 4}

    with pytest.raises(OSError):
        get_document(command('get_document'), table, s3, 'bucket')

    stream.close.assert_called_once()
