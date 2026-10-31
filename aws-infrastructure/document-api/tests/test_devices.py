"""Verify proof, approval, replay and session association gates before document writes."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from device_fixtures import approval_for_event, fixture_database, sign_event
from test_upload import app, configure_storage, event_for

PAYLOAD = {
    'username': 'alice',
    'files': [
        {'name': 'notes.md', 'content': '# Notes', 'category': 'workflow-docs', 'metadata': {}}
    ],
}
CONTEXT = SimpleNamespace(aws_request_id='request')


@pytest.fixture
def runtime(monkeypatch):
    """Provide stateful AWS doubles without disabling a production authentication check."""
    database, rows = fixture_database()
    session = Mock()

    configure_storage(session.client.return_value, database.Table('files'))
    session.resource.return_value = database

    monkeypatch.setattr(app.boto3, 'Session', lambda: session)

    monkeypatch.setenv('USER_FILES_TABLE', 'files')

    monkeypatch.setenv('DOCUMENTS_BUCKET', 'bucket')

    return session, database, rows


def test_pending_approved_upload_replay_and_revocation(runtime):
    """Only explicitly approved keys can bind sessions and write, and each proof is single-use."""
    session, database, rows = runtime
    registration = event_for({'username': 'alice', 'action': 'register_device'})
    response = app.lambda_handler(registration, CONTEXT)
    assert response['statusCode'] == 202
    assert json.loads(response['body'])['approval_required'] is True
    assert 'softrobots-auth-sessions' not in rows or not rows['softrobots-auth-sessions']
    assert app.lambda_handler(event_for(PAYLOAD), CONTEXT)['statusCode'] == 403

    session.client.assert_not_called()
    assert app.lambda_handler(registration, CONTEXT)['statusCode'] == 403

    approval = approval_for_event(registration)

    database.Table('softrobots-native-device-approvals').put_item(Item=approval)
    assert app.lambda_handler(sign_event(registration), CONTEXT)['statusCode'] == 200
    assert app.lambda_handler(sign_event(registration), CONTEXT)['statusCode'] == 200
    event = event_for(PAYLOAD)
    assert app.lambda_handler(event, CONTEXT)['statusCode'] == 201
    assert session.client.return_value.put_object.call_count == 1

    database.Table('files').put_item.assert_not_called()
    sessions = list(rows['softrobots-auth-sessions'].values())
    assert sessions[0]['device_mac'] == '02:11:22:33:44:55'
    assert sessions[0]['device_id'] == event['headers']['x-device-id']

    session.reset_mock()
    assert app.lambda_handler(event, CONTEXT)['statusCode'] == 403

    session.client.assert_not_called()
    approval['status'] = 'REVOKED'

    database.Table('softrobots-native-device-approvals').put_item(Item=approval)

    for request in (event_for(PAYLOAD), sign_event(registration)):
        assert app.lambda_handler(request, CONTEXT)['statusCode'] == 403

    session.client.assert_not_called()


@pytest.mark.parametrize(
    'change',
    [
        'body',
        'token',
        'timestamp',
        'nonce',
        'valid-nonce',
        'fresh-timestamp',
        'path',
        'method',
        'query',
        'key',
        'mac',
        'device',
        'signature',
        'missing-bearer',
        'curve',
        'old-session',
    ],
)
def test_invalid_proof_before_any_aws(change, monkeypatch):
    """Tampered or malformed protocol inputs never create an AWS session."""
    event = event_for(PAYLOAD)
    headers = event['headers']

    if change == 'body':
        event['body'] += ' '
    elif change == 'token':
        headers['authorization'] = 'Bearer other-token'
    elif change == 'timestamp':
        headers['x-device-timestamp'] = '1'
    elif change == 'nonce':
        headers['x-device-nonce'] = 'not-a-uuid'
    elif change == 'valid-nonce':
        headers['x-device-nonce'] = '22222222-2222-4222-8222-222222222222'
    elif change == 'fresh-timestamp':
        headers['x-device-timestamp'] = str(int(headers['x-device-timestamp']) - 1)
    elif change == 'path':
        event['rawPath'] = '/other'
    elif change == 'method':
        event['requestContext']['http']['method'] = 'GET'
    elif change == 'query':
        event['rawQueryString'] = 'a=1'
    elif change == 'key':
        headers['x-device-key'] = 'AAAA'
    elif change == 'mac':
        headers['x-device-mac'] = '02:aa:bb:cc:dd:ee'
    elif change == 'device':
        headers['x-device-id'] = '22222222-2222-4222-8222-222222222222'
    elif change == 'signature':
        headers['x-device-signature'] = base64.b64encode(b'x' * 70).decode()
    elif change == 'missing-bearer':
        del headers['authorization']
    elif change == 'curve':
        sign_event(event, ec.generate_private_key(ec.SECP384R1()))
    else:
        event['requestContext']['authorizer']['jwt']['claims']['auth_time'] = 1
    session = Mock()
    session.client.return_value.put_object.return_value = {'VersionId': 'version-1'}

    monkeypatch.setattr(app.boto3, 'Session', session)
    assert app.lambda_handler(event, CONTEXT)['statusCode'] == 403

    session.assert_not_called()


def test_owner_key_and_session_binding(runtime):
    """Approval cannot cross principals or keys; signed metadata cannot replace a family binding."""
    session, database, rows = runtime
    event = event_for({'username': 'alice', 'action': 'register_device'})

    database.Table('softrobots-native-device-approvals').put_item(Item=approval_for_event(event))
    assert app.lambda_handler(event, CONTEXT)['statusCode'] == 200

    for change in ('issuer', 'subject', 'key', 'device', 'mac'):
        changed = event_for(PAYLOAD)
        claims = changed['requestContext']['authorizer']['jwt']['claims']

        if change == 'issuer':
            claims['iss'] = 'other-issuer'
        elif change == 'subject':
            claims['sub'] = 'other-subject'
        elif change == 'key':
            sign_event(changed, ec.generate_private_key(ec.SECP256R1()))
        elif change == 'device':
            changed['headers']['x-device-id'] = '22222222-2222-4222-8222-222222222222'

            sign_event(changed)
        else:
            changed['headers']['x-device-mac'] = '02:aa:bb:cc:dd:ee'

            sign_event(changed)
        assert app.lambda_handler(changed, CONTEXT)['statusCode'] == 403

    session.client.assert_not_called()


def test_base64_body_and_nonce_retention(runtime):
    """Gateway body encoding preserves signature bytes and nonce expiry exceeds acceptance windows."""
    session, database, rows = runtime
    event = event_for({'username': 'alice', 'action': 'register_device'})
    event['body'] = base64.b64encode(event['body'].encode()).decode()
    event['isBase64Encoded'] = True
    assert app.lambda_handler(event, CONTEXT)['statusCode'] == 202
    nonce = database.Table('softrobots-native-device-nonces').put_item.call_args.kwargs
    assert nonce['ConditionExpression'] == 'attribute_not_exists(nonce_id)'
    assert nonce['Item']['expires_at'] - int(event['headers']['x-device-timestamp']) >= 180

    session.client.assert_not_called()


def test_dependency_failure_never_reaches_document_storage(runtime):
    """Unavailable approval or replay storage cannot degrade into an authorized upload."""
    session, database, rows = runtime
    event = event_for(PAYLOAD)
    database.Table('softrobots-native-device-approvals').get_item.side_effect = RuntimeError(
        'offline'
    )
    assert app.lambda_handler(event, CONTEXT)['statusCode'] == 500

    session.client.assert_not_called()
    assert not rows.get('files')

    database.Table('files').update_item.assert_not_called()


def test_browser_route_binds_proof_path_and_requires_its_own_owner_approval(runtime):
    """An old-route signature and family-pool approval cannot authorize browser uploads."""
    session, database, rows = runtime
    old = event_for({'username': 'alice', 'action': 'register_device'})

    database.Table('softrobots-native-device-approvals').put_item(Item=approval_for_event(old))
    assert app.lambda_handler(old, CONTEXT)['statusCode'] == 200

    # Changing only the target after signing must fail before any AWS operation.
    tampered = event_for({'username': 'alice', 'action': 'register_device'})
    tampered['rawPath'] = '/browser-documents'

    session.reset_mock()
    assert app.lambda_handler(tampered, CONTEXT)['statusCode'] == 403

    session.resource.assert_not_called()

    registration = event_for({'username': 'alice', 'action': 'register_device'})
    registration['rawPath'] = '/browser-documents'
    claims = registration['requestContext']['authorizer']['jwt']['claims']
    claims['iss'] = 'https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_ExampleBrowser'
    claims['sub'] = 'username-pool-user'
    claims['client_id'] = 'examplebrowserclient'

    sign_event(registration)
    assert app.lambda_handler(registration, CONTEXT)['statusCode'] == 202
    assert approval_for_event(old)['approval_id'] != approval_for_event(registration)['approval_id']

    database.Table('softrobots-native-device-approvals').put_item(
        Item=approval_for_event(registration)
    )
    assert app.lambda_handler(sign_event(registration), CONTEXT)['statusCode'] == 200
    upload = event_for(PAYLOAD)
    upload['rawPath'] = '/browser-documents'
    upload['requestContext']['authorizer']['jwt']['claims'] = dict(claims)
    assert app.lambda_handler(sign_event(upload), CONTEXT)['statusCode'] == 201
    assert session.client.return_value.put_object.call_count == 1
