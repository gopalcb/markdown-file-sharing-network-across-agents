"""Exercise the essential telemetry runtime, storage, MCP, and packaging contracts."""

import base64
import importlib
import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import boto3
import jwt
import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from moto import mock_aws

RUNTIME = Path(__file__).resolve().parents[1] / 'telemetry-api'

sys.path.insert(0, str(RUNTIME))

import services.analytics_service as analytics_service  # noqa: E402
from app import create_app as create_portal_app  # noqa: E402
from document_api.s3_document_service import DocumentStorage, store_documents  # noqa: E402
from document_api.validation import (  # noqa: E402
    validate_native_proof,
    validate_upload_identity,
)
from mcp_api.auth_service import verify_access_token  # noqa: E402
from server import create_app as create_combined_app  # noqa: E402
from services.analytics_service import (  # noqa: E402
    AnalyticsResolutionIncomplete,
    delete_portfolio_events,
    validate_analytics_delete,
)
from services.authentication_service import COOKIE_NAME, JwksCache, session_record  # noqa: E402
from werkzeug.datastructures import MultiDict  # noqa: E402

DEVICE_ID = '11111111-1111-4111-8111-111111111111'
PORTAL_ORIGIN = 'https://d2f8oftnpxmjan.cloudfront.net'
PORTAL_ISSUER = 'https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_Test123'
PORTAL_CONFIG = {
    'COGNITO_ISSUER': PORTAL_ISSUER,
    'COGNITO_CLIENT_ID': 'testclient123',
    'PORTAL_ORIGIN': PORTAL_ORIGIN,
}
MCP_CONFIG = {
    'enabled': True,
    'PUBLIC_BASE_URL': 'https://mcp.example.com',
    'COGNITO_ISSUER': 'https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_Mcp',
    'COGNITO_CLIENT_ID': 'mcp-client',
    'MCP_RESOURCE': 'https://mcp.example.com/mcp/',
    'MCP_WRITE_SCOPE': 'markdown-api/write',
    'ALLOWED_SUB': 'owner',
    'S3_BUCKET': 'bucket',
    'USER_FILES_TABLE': 'index',
    'COGNITO_DOMAIN': 'https://fixture.auth.ca-central-1.amazoncognito.com',
    'OAUTH_STATE_TABLE': 'oauth',
    'OAUTH_SECRET_ARN': 'secret',
}


class FixtureRuntime(BaseHTTPRequestHandler):
    """Model Lambda Runtime API invocations for the real web-adapter binary."""

    def log_message(self, format: str, *args) -> None:
        """Keep the opt-in adapter test output focused."""

    def do_POST(self) -> None:
        """Register the extension or capture an invocation response."""
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))

        if self.path == '/2020-01-01/extension/register':
            self.send_response(200)

            self.send_header('Lambda-Extension-Identifier', 'fixture')

            self.end_headers()

            self.wfile.write(b'{"functionName":"fixture","functionVersion":"$LATEST"}')

            return

        self.server.responses.put((self.path, body))

        self.send_response(202)

        self.end_headers()

    def do_GET(self) -> None:
        """Return queued API Gateway v2 events and extension events."""

        if self.path != '/2018-06-01/runtime/invocation/next':
            self.send_response(200)

            self.end_headers()

            self.wfile.write(
                b'{"eventType":"INVOKE","deadlineMs":9999999999999,"requestId":"fixture"}'
            )

            return
        event = self.server.events.get(timeout=10)

        self.send_response(200)

        self.send_header('Lambda-Runtime-Aws-Request-Id', event['rawPath'].rsplit('/', 1)[-1])

        self.send_header('Lambda-Runtime-Deadline-Ms', str(int(time.time() * 1000) + 28000))

        self.send_header(
            'Lambda-Runtime-Invoked-Function-Arn',
            'arn:aws:lambda:ca-central-1:249766001220:function:fixture',
        )

        self.end_headers()

        self.wfile.write(json.dumps(event).encode())


class DeviceTable:
    """Provide the minimal DynamoDB interface used by authenticated fixture sessions."""

    def __init__(self) -> None:
        """Create an empty session table."""
        self.rows = {}

    def put_item(self, Item: dict, ConditionExpression: str) -> None:
        """Store a fixture session record."""
        self.rows[Item['session_id']] = dict(Item)

    def get_item(self, Key: dict, ConsistentRead: bool) -> dict:
        """Read a fixture session record."""

        return {'Item': self.rows.get(Key['session_id'])}


@pytest.fixture(scope='module')
def portal_keys() -> list:
    """Generate independent trusted and attacker signing keys."""

    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


@pytest.fixture
def portal_runtime(portal_keys: list) -> tuple:
    """Build the portal with real JWT verification and in-memory session state."""
    table = DeviceTable()
    public = jwt.algorithms.RSAAlgorithm.to_jwk(portal_keys[0].public_key(), as_dict=True)
    jwks = {'keys': [{**public, 'kid': 'trusted', 'use': 'sig', 'alg': 'RS256'}]}

    def issue(changes: dict | None = None, key_index: int = 0) -> str:
        """Issue a signed fixture access token."""
        now = int(time.time())
        claims = {
            'iss': PORTAL_ISSUER,
            'sub': 'fixture-user',
            'client_id': 'testclient123',
            'token_use': 'access',
            'iat': now,
            'exp': now + 300,
            'auth_time': now,
            'cognito:groups': ['portal-users'],
            'origin_jti': 'fixture-family',
        } | (changes or {})

        try:
            record = session_record(claims, DEVICE_ID)
            table.rows[record['session_id']] = record
        except PermissionError:
            pass

        return jwt.encode(
            claims,
            portal_keys[key_index],
            algorithm='RS256',
            headers={'kid': 'trusted'},
        )

    application = create_portal_app(
        PORTAL_CONFIG,
        JwksCache(PORTAL_ISSUER, fetch=Mock(return_value=jwks)),
        table,
    )

    return application, issue


def test_portal_health_auth_document_and_csrf_contract(portal_runtime, monkeypatch) -> None:
    """Cover public health, signed identity, document ownership, and cookie CSRF denial."""
    application, issue = portal_runtime
    client = application.test_client()
    assert client.get('/api/health').json == {'status': 'ok', 'service': 'telemetry-api'}
    assert client.get('/api/me').status_code == 401
    headers = {'Authorization': 'Bearer ' + issue(), 'X-Device-Id': DEVICE_ID}
    assert client.get('/api/me', headers=headers).json['sub'] == 'fixture-user'
    assert (
        client.get('/api/me', headers={'Authorization': 'Bearer ' + issue(key_index=1)}).status_code
        == 401
    )

    operation = Mock(
        return_value={
            'statusCode': 201,
            'body': json.dumps(
                {
                    'files': [
                        {
                            'name': 'note.md',
                            'status': 'READY',
                            'file_id': 'b1cdfdcf-1a9c-4f25-9387-d185132abb17',
                            's3_key': (
                                'agent-work/raw-markdown/fixture-user/workflow-docs/note.md'
                            ),
                        }
                    ],
                    'request_id': 'fixture-request',
                }
            ),
        }
    )

    monkeypatch.setattr('document_api.handler.lambda_handler', operation)
    uploaded = client.post(
        '/api/documents',
        json={'file-group': 'workflow-docs', 'files': [{'name': 'note.md', 'content': '# Note'}]},
        headers=headers,
    )
    assert uploaded.status_code == 201
    assert operation.call_args.args[0]['username'] == 'fixture-user'

    monkeypatch.setattr('routing.browser_dependencies', Mock)

    monkeypatch.setattr(
        'routing.authenticate_browser_session',
        lambda *args: ({'sub': 'fixture-user'}, {'csrf': 'a' * 64}),
    )

    client.set_cookie(COOKIE_NAME, 'fixture-cookie')
    rejected = client.post(
        '/api/documents',
        json={},
        headers={'X-Device-Id': DEVICE_ID, 'Origin': 'https://attacker.example'},
    )
    assert rejected.status_code == 403
    assert operation.call_count == 1


def test_analytics_delete_accepts_only_bounded_public_identities() -> None:
    """Deletion accepts public hashes and UUIDs, never raw keys or duplicate identities."""
    identity = {
        'visitor_key': 'a' * 64,
        'event_id': '22222222-2222-4222-8222-222222222222',
    }
    assert validate_analytics_delete({'events': [identity]}, MultiDict()) == [identity]

    for payload in (
        None,
        {},
        {'events': []},
        {'events': [identity, identity]},
        {'events': [{**identity, 'PK': 'private'}]},
        {'events': [{'visitor_key': 'raw-session', 'event_id': identity['event_id']}]},
        {'events': [identity], 'table': 'other'},
    ):
        with pytest.raises(ValueError):
            validate_analytics_delete(payload, MultiDict())


def test_analytics_delete_resolves_exact_events_and_writes_once() -> None:
    """One transaction contains only the exact selected session/event pair."""
    session = '11111111-1111-4111-8111-111111111111'
    event_id = '22222222-2222-4222-8222-222222222222'
    visitor_key = sha256(session.encode()).hexdigest()
    client = Mock()
    table = Mock()
    table.name = 'gb-portfolio-visitors-table'
    table.meta = SimpleNamespace(client=client)
    table.scan.return_value = {
        'Items': [
            {
                'PK': 'SESSION#' + session,
                'SK': 'EVENT#' + event_id,
                'session': session,
                'event_id': event_id,
            }
        ]
    }
    events = [{'visitor_key': visitor_key, 'event_id': event_id}]

    assert delete_portfolio_events(events, table) == {
        'removed': events,
        'deleted_count': 1,
        'already_absent_count': 0,
    }
    assert client.transact_write_items.call_args.kwargs['TransactItems'] == [
        {
            'Delete': {
                'TableName': 'gb-portfolio-visitors-table',
                'Key': {
                    'PK': {'S': 'SESSION#' + session},
                    'SK': {'S': 'EVENT#' + event_id},
                },
            }
        }
    ]


def test_analytics_delete_scan_bound_fails_before_any_write(monkeypatch) -> None:
    """An incomplete bounded identity scan cannot partially mutate the table."""
    table = Mock()
    table.meta = SimpleNamespace(client=Mock())
    table.scan.return_value = {
        'Items': [],
        'LastEvaluatedKey': {
            'PK': 'SESSION#11111111-1111-4111-8111-111111111111',
            'SK': 'EVENT#22222222-2222-4222-8222-222222222222',
        },
    }

    monkeypatch.setattr(analytics_service, 'MAX_DELETE_SCAN_PAGES', 1)

    with pytest.raises(AnalyticsResolutionIncomplete):
        delete_portfolio_events(
            [
                {
                    'visitor_key': 'a' * 64,
                    'event_id': '22222222-2222-4222-8222-222222222222',
                }
            ],
            table,
        )

    table.meta.client.transact_write_items.assert_not_called()


def test_analytics_delete_route_requires_auth_and_sanitizes_uncertain_writes(
    portal_runtime, monkeypatch
) -> None:
    """The standard auth boundary precedes deletion and storage errors remain private."""
    application, issue = portal_runtime
    client = application.test_client()
    identity = {
        'visitor_key': 'a' * 64,
        'event_id': '22222222-2222-4222-8222-222222222222',
    }
    path = '/api/analytics/gh-portfolio'
    assert client.delete(path, json={'events': [identity]}).status_code == 401
    headers = {'Authorization': 'Bearer ' + issue(), 'X-Device-Id': DEVICE_ID}
    deleter = Mock(
        return_value={'removed': [identity], 'deleted_count': 1, 'already_absent_count': 0}
    )

    monkeypatch.setattr('routing.delete_portfolio_events', deleter)
    response = client.delete(path, headers=headers, json={'events': [identity]})
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'

    deleter.assert_called_once_with([identity])

    monkeypatch.setattr(
        'routing.delete_portfolio_events',
        Mock(side_effect=AnalyticsResolutionIncomplete('private detail')),
    )
    response = client.delete(path, headers=headers, json={'events': [identity]})
    assert response.status_code == 409
    assert response.json == {'error': 'analytics_delete_resolution_incomplete'}

    monkeypatch.setattr(
        'routing.delete_portfolio_events',
        Mock(
            side_effect=ClientError(
                {'Error': {'Code': 'TransactionCanceledException', 'Message': 'private detail'}},
                'TransactWriteItems',
            )
        ),
    )
    response = client.delete(path, headers=headers, json={'events': [identity]})
    assert response.status_code == 503
    assert response.json == {'error': 'analytics_delete_unconfirmed'}


@pytest.mark.skipif(
    os.environ.get('TELEMETRY_LAMBDA_ADAPTER_TESTS') != '1',
    reason='Requires the explicitly built telemetry Lambda image',
)
def test_real_lambda_adapter_preserves_gateway_auth_and_cache_contract() -> None:
    """Exercise the packaged adapter with API Gateway v2 events and clean up Docker."""
    server = ThreadingHTTPServer(('0.0.0.0', 0), FixtureRuntime)
    server.daemon_threads = True
    server.events, server.responses = queue.Queue(), queue.Queue()

    for path in ['/api/health', '/api/me', '/api/session']:
        server.events.put(
            {
                'version': '2.0',
                'routeKey': 'ANY /api/{proxy+}',
                'rawPath': path,
                'rawQueryString': '',
                'headers': {'host': 'fixture.execute-api.ca-central-1.amazonaws.com'},
                'requestContext': {
                    'http': {
                        'method': 'GET',
                        'path': path,
                        'protocol': 'HTTP/1.1',
                        'sourceIp': '127.0.0.1',
                        'userAgent': 'fixture',
                    },
                    'requestId': path,
                },
                'isBase64Encoded': False,
                'cookies': ['__Host-wo-session=invalid'] if path == '/api/session' else [],
            }
        )

    threading.Thread(target=server.serve_forever, daemon=True).start()
    container, process, error = '', None, ''

    try:
        container = subprocess.check_output(
            [
                'docker',
                'run',
                '--rm',
                '-d',
                '--platform',
                'linux/amd64',
                '--add-host',
                'host.docker.internal:host-gateway',
                '-e',
                'COGNITO_ISSUER=https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_fixture',
                '-e',
                'COGNITO_CLIENT_ID=fixture123',
                '-e',
                'MCP_ENABLED=false',
                '-e',
                'AWS_EC2_METADATA_DISABLED=true',
                '-e',
                'AWS_DEFAULT_REGION=ca-central-1',
                os.environ.get('TELEMETRY_LAMBDA_TEST_IMAGE', 'telemetry-lambda:test'),
            ],
            text=True,
        ).strip()
        process = subprocess.Popen(
            [
                'docker',
                'exec',
                '-e',
                f'AWS_LAMBDA_RUNTIME_API=host.docker.internal:{server.server_address[1]}',
                '-e',
                'AWS_LAMBDA_FUNCTION_NAME=fixture',
                '-e',
                'AWS_LAMBDA_FUNCTION_MEMORY_SIZE=512',
                '-e',
                'AWS_LAMBDA_FUNCTION_VERSION=$LATEST',
                container,
                '/opt/extensions/lambda-adapter',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        payloads = [json.loads(server.responses.get(timeout=25)[1]) for _ in range(3)]
        assert [payload['statusCode'] for payload in payloads] == [200, 401, 401]

        for payload in payloads:
            headers = {key.lower(): value for key, value in payload['headers'].items()}
            assert headers['cache-control'] == 'private, no-store'
            assert payload.get('isBase64Encoded', False) is False
        cleared = payloads[2]['cookies'][0]
        assert cleared.startswith('__Host-wo-session=;')
        assert all(flag in cleared for flag in ['Secure', 'HttpOnly', 'SameSite=Lax', 'Path=/'])
    finally:
        if container:
            stopped = subprocess.run(['docker', 'stop', container], stdout=subprocess.DEVNULL)

            if stopped.returncode:
                subprocess.run(
                    ['docker', 'rm', '-f', container], check=True, stdout=subprocess.DEVNULL
                )

        if process:
            try:
                _, error = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                _, error = process.communicate(timeout=5)

        server.shutdown()

        server.server_close()

    if container:
        assert not subprocess.check_output(
            ['docker', 'ps', '-aq', '--filter', 'id=' + container], text=True
        ).strip()
    assert 'panicked' not in error, error[:1000]


def test_document_path_lifecycle_preserves_identity_task_and_bytes() -> None:
    """Verify one path retains its document/task identity while replacing stored bytes."""

    with mock_aws():
        s3 = boto3.client('s3', region_name='us-east-1')

        s3.create_bucket(Bucket='documents')
        table = boto3.resource('dynamodb', region_name='us-east-1').create_table(
            TableName='files',
            BillingMode='PAY_PER_REQUEST',
            KeySchema=[
                {'AttributeName': 'username', 'KeyType': 'HASH'},
                {'AttributeName': 'file_id', 'KeyType': 'RANGE'},
            ],
            AttributeDefinitions=[
                {'AttributeName': 'username', 'AttributeType': 'S'},
                {'AttributeName': 'file_id', 'AttributeType': 'S'},
            ],
        )
        upload = {
            'username': 'alice',
            'storage_layout': 'project-path-v1',
            'file-group': 'workflow-docs',
            'files': [
                {
                    'name': 'plan.md',
                    'relative_path': 'docs/tasks/example/plan.md',
                    'content': '# Plan\n',
                    'metadata': {
                        'project_id': 'project-a',
                        'relative_path': 'docs/tasks/example/plan.md',
                    },
                }
            ],
        }
        storage = DocumentStorage(table, s3, 'documents')
        first = store_documents(upload, storage, 'request')[0]
        assert first['status'] == 'READY' and UUID(first['file_id']).version == 5
        key = {'username': 'alice', 'file_id': first['file_id']}

        table.update_item(
            Key=key,
            UpdateExpression='SET task.#status = :done',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={':done': 'done'},
        )
        upload['files'][0]['content'] = '# Changed\n'
        second = store_documents(upload, storage, 'request')[0]
        assert second['file_id'] == first['file_id'] and second['task']['status'] == 'done'
        stored = s3.get_object(Bucket='documents', Key=second['s3_key'])['Body'].read()
        assert stored == b'# Changed\n' and second['sha256'] == sha256(stored).hexdigest()
        assert table.scan()['Count'] == 1


def test_native_upload_preserves_signed_bytes_and_rejects_tampering(
    portal_runtime, monkeypatch
) -> None:
    """Exercise the native Cognito, gateway-context, and device-proof boundary."""
    application, issue = portal_runtime

    monkeypatch.setenv('NATIVE_UPLOAD_ISSUER', PORTAL_ISSUER)

    monkeypatch.setenv('NATIVE_UPLOAD_CLIENT_ID', 'testclient123')
    application.extensions['native_jwks_caches'] = {
        PORTAL_ISSUER: application.extensions['jwks_cache']
    }
    bearer = issue({'username': 'alice', 'scope': 'aws.cognito.signin.user.admin'})
    claims = jwt.decode(bearer, options={'verify_signature': False})
    raw = b'{"username":"alice","files":[{"name":"note.md","content":"hello"}]}'
    timestamp, nonce = str(int(time.time())), str(uuid4())
    device_key = ec.generate_private_key(ec.SECP256R1())
    public = device_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    canonical = '\n'.join(
        [
            'WO-DEVICE-V1',
            'POST',
            '/documents',
            timestamp,
            nonce,
            sha256(raw).hexdigest(),
            sha256(bearer.encode()).hexdigest(),
            DEVICE_ID,
            '',
        ]
    ).encode()
    context = {
        'requestId': 'gateway-request-123',
        'http': {'method': 'POST', 'path': '/documents'},
        'authorizer': {'jwt': {'claims': claims}},
    }
    headers = {
        'Authorization': 'Bearer ' + bearer,
        'Content-Type': 'application/json',
        'X-Device-Id': DEVICE_ID,
        'X-Device-Key': base64.b64encode(public).decode(),
        'X-Device-Signature': base64.b64encode(
            device_key.sign(canonical, ec.ECDSA(hashes.SHA256()))
        ).decode(),
        'X-Device-Timestamp': timestamp,
        'X-Device-Nonce': nonce,
        'X-Amzn-Request-Context': json.dumps(context),
    }
    calls = []

    def execute(event: dict, invocation) -> dict:
        """Run the production identity and device-proof checks before returning a receipt."""

        try:
            validate_upload_identity(event, {'username': 'alice'})

            validate_native_proof(event)
        except PermissionError:
            return {
                'statusCode': 403,
                'body': json.dumps({'error': 'forbidden'}),
                'headers': {'Content-Type': 'application/json'},
            }

        calls.append(event)

        return {
            'statusCode': 201,
            'body': json.dumps({'files': [{'status': 'READY'}]}),
            'headers': {'Content-Type': 'application/json'},
        }

    monkeypatch.setattr('services.native_upload_service.lambda_handler', execute)
    assert (
        application.test_client().post('/documents', data=raw, headers=headers).status_code == 201
    )
    assert base64.b64decode(calls[0]['body']) == raw
    assert (
        application.test_client().post('/documents', data=raw + b' ', headers=headers).status_code
        == 403
    )
    assert len(calls) == 1


def test_mcp_discovery_auth_and_tool_call(monkeypatch) -> None:
    """Exercise the shared portal/MCP process and one authenticated FastMCP call."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = Mock()
    provider.get_signing_key_from_jwt.return_value = SimpleNamespace(key=key.public_key())

    def verify(token: str) -> dict:
        """Verify the signed fixture through the production MCP verifier."""

        return verify_access_token(token, MCP_CONFIG, provider)

    claims = {
        'iss': MCP_CONFIG['COGNITO_ISSUER'],
        'aud': MCP_CONFIG['MCP_RESOURCE'],
        'sub': 'owner',
        'client_id': 'mcp-client',
        'token_use': 'access',
        'scope': 'markdown-api/write',
        'iat': int(time.time()),
        'exp': int(time.time()) + 300,
    }
    headers = {
        'Authorization': 'Bearer ' + jwt.encode(claims, key, algorithm='RS256'),
        'Accept': 'application/json, text/event-stream',
    }
    storage = importlib.import_module('mcp_api.storage_service')
    writer = Mock(return_value=[{'name': 'note.md', 'status': 'READY', 'file_id': 'fixture'}])

    monkeypatch.setattr(storage, 'write_documents', writer)
    application = create_combined_app(
        portal_values=PORTAL_CONFIG,
        portal_keys=Mock(),
        mcp_config=MCP_CONFIG,
        mcp_verifier=verify,
    )

    with TestClient(application, base_url=MCP_CONFIG['PUBLIC_BASE_URL']) as client:
        assert client.get('/api/health').status_code == 200
        assert client.get('/.well-known/oauth-protected-resource/mcp').status_code == 200
        assert client.post('/mcp/', json={}).status_code == 401
        initialized = client.post(
            '/mcp/',
            headers=headers,
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-03-26',
                    'capabilities': {},
                    'clientInfo': {'name': 'test', 'version': '1'},
                },
            },
        )
        assert initialized.status_code == 200
        called = client.post(
            '/mcp/',
            headers=headers,
            json={
                'jsonrpc': '2.0',
                'id': 2,
                'method': 'tools/call',
                'params': {
                    'name': 'save_markdown',
                    'arguments': {'payload': {'files': [{'name': 'note.md', 'content': '# Note'}]}},
                },
            },
        )
        assert called.status_code == 200 and not called.json()['result'].get('isError')
        assert writer.call_args.args[0]['username'] == 'gopalcbala'


def test_lambda_and_cdk_keep_one_bounded_runtime() -> None:
    """Keep one ASGI worker and reserved Lambda concurrency 10 in deployable source."""
    root = Path(__file__).parents[1]
    dockerfile = (root / 'telemetry-api' / 'Dockerfile.lambda').read_text()
    stack = (root / 'aws-infrastructure' / 'cdk' / 'telemetry_stack.py').read_text()
    assert 'COPY mcp_api ./mcp_api' in dockerfile
    assert '"--workers", "1"' in dockerfile
    assert "function_name='telemetry-lambda'" in stack
    assert 'reserved_concurrent_executions=10' in stack


def test_validation_workflow_and_policy_source_are_bounded(tmp_path, monkeypatch) -> None:
    """Keep CI validation-only while retained deployment policy source stays bounded."""
    root = Path(__file__).parents[1]
    policy_path = root / 'aws-infrastructure/cdk/configure_ci_policy.py'
    specification = importlib.util.spec_from_file_location('release_policy', policy_path)
    module = importlib.util.module_from_spec(specification)

    specification.loader.exec_module(module)
    statements = module.deployment_policy()['Statement']
    assume = next(item for item in statements if item['Sid'] == 'AssumeExactCdkBootstrapRoles')
    bootstrap_version = next(
        item for item in statements if item['Sid'] == 'ReadExactCdkBootstrapVersion'
    )
    policy_inspection = next(
        item for item in statements if item['Sid'] == 'InspectThisReviewedPolicy'
    )
    policy_deny = next(item for item in statements if item['Sid'] == 'DenyDirectPolicyEscalation')
    role_deny = next(item for item in statements if item['Sid'] == 'DenyLegacyMcpRoleMutation')

    assert assume['Action'] == 'sts:AssumeRole' and len(assume['Resource']) == 4
    assert all('cdk-hnb659fds-' in resource for resource in assume['Resource'])
    assert bootstrap_version == {
        'Sid': 'ReadExactCdkBootstrapVersion',
        'Effect': 'Allow',
        'Action': 'ssm:GetParameter',
        'Resource': (
            'arn:aws:ssm:ca-central-1:249766001220:parameter/cdk-bootstrap/hnb659fds/version'
        ),
    }
    assert all(item.get('Resource') != '*' for item in statements)
    assert policy_inspection['Action'] == [
        'iam:GetPolicy',
        'iam:GetPolicyVersion',
        'iam:ListPolicyVersions',
    ]
    mutating_policy_actions = {
        'iam:CreatePolicy',
        'iam:CreatePolicyVersion',
        'iam:DeletePolicyVersion',
        'iam:SetDefaultPolicyVersion',
        'iam:DeletePolicy',
    }
    allowed_actions = {
        action
        for statement in statements
        if statement['Effect'] == 'Allow'
        for action in (
            statement['Action'] if isinstance(statement['Action'], list) else [statement['Action']]
        )
    }
    assert allowed_actions.isdisjoint(mutating_policy_actions)
    assert policy_deny == {
        'Sid': 'DenyDirectPolicyEscalation',
        'Effect': 'Deny',
        'Action': [
            'iam:CreatePolicy',
            'iam:CreatePolicyVersion',
            'iam:SetDefaultPolicyVersion',
            'iam:DeletePolicyVersion',
            'iam:DeletePolicy',
        ],
        'Resource': [module.POLICY_ARN, module.QUOTA_POLICY_ARN],
    }
    assert role_deny['Effect'] == 'Deny'
    assert role_deny['Resource'] == module.LEGACY_MCP_ROLE_ARN
    assert {
        'iam:CreateRole',
        'iam:UpdateAssumeRolePolicy',
        'iam:PutRolePolicy',
    }.issubset(role_deny['Action'])

    iam = Mock()
    iam.get_policy.return_value = {'Policy': {'DefaultVersionId': 'v1'}}
    iam.get_policy_version.return_value = {
        'PolicyVersion': {'Document': {'Version': '2012-10-17', 'Statement': []}}
    }
    iam.list_policy_versions.return_value = {'Versions': [{'VersionId': 'v1'}]}
    iam.create_policy_version.return_value = {'PolicyVersion': {'VersionId': 'v2'}}
    sts = Mock()
    sts.get_caller_identity.return_value = {'Account': module.ACCOUNT}
    policy_session = Mock()
    policy_session.client.side_effect = lambda service: {'iam': iam, 'sts': sts}[service]

    monkeypatch.setattr(module.boto3, 'Session', Mock(return_value=policy_session))

    transition = module.apply_policy()

    assert transition['direct_iam_mutation_denied'] is True

    iam.create_policy_version.assert_called_once()

    workflow = (root / '.github/workflows/validate.yml').read_text()
    assert 'permissions:\n  contents: read' in workflow
    assert 'id-token:' not in workflow
    assert 'aws-actions/configure-aws-credentials' not in workflow
    assert 'deploy.py ' not in workflow

    for stack in [
        'SoftrobotsTelemetry',
        'SoftrobotsPortalEdge',
        'MarkdownMcpEdge',
        'SoftrobotsDocumentData',
    ]:
        assert f'synth {stack}' in workflow

    template = (root / 'aws-infrastructure/document-data.yaml').read_text()
    history = template.split('  AgentWorkHistory:', 1)[1].split('  AgentWorkHistoryPolicy:', 1)[0]
    assert 'VersioningConfiguration' not in history

    deploy_source = (root / 'aws-infrastructure/cdk/deploy.py').read_text()
    assert 'verify_published_assets(manifest, published)' in deploy_source
    assert "runtime_image != prepared.get('runtime_image')" in deploy_source
    assert 'PREPARE_PATH.is_file()' in deploy_source

    deploy_spec = importlib.util.spec_from_file_location(
        'release_deployer', root / 'aws-infrastructure/cdk/deploy.py'
    )
    deploy_module = importlib.util.module_from_spec(deploy_spec)

    deploy_spec.loader.exec_module(deploy_module)
    receipt = {'index.html': {'sha256': 'expected', 'version_id': 'v1'}}

    deploy_module.verify_published_assets(receipt, receipt)

    for corrupted in [
        {},
        {'index.html': {'sha256': 'changed', 'version_id': 'v1'}},
        {'index.html': {'sha256': 'expected', 'version_id': 'v2'}},
    ]:
        with pytest.raises(RuntimeError):
            deploy_module.verify_published_assets(receipt, corrupted)

    prepare_path = tmp_path / 'prepare.json'

    monkeypatch.setattr(deploy_module, 'PREPARE_PATH', prepare_path)

    monkeypatch.setattr(deploy_module, 'MCP_PREPARE_PATH', tmp_path / 'mcp.json')

    with pytest.raises(RuntimeError, match='prepare record'):
        deploy_module.reconcile_mcp(Mock(), None)

    outputs = {
        'ApiHost': 'fixture.execute-api.ca-central-1.amazonaws.com',
        'ApiHealthUrl': 'https://fixture.execute-api.ca-central-1.amazonaws.com/api/health',
        'FunctionName': 'telemetry-lambda',
    }
    image = 'fixture@sha256:' + 'a' * 64

    prepare_path.write_text(
        json.dumps(
            {
                'source_sha256': deploy_module.source_digest(),
                'outputs': outputs,
                'runtime_image': image,
            }
        )
    )
    cloudformation = Mock()
    cloudformation.describe_stacks.return_value = {
        'Stacks': [
            {
                'StackStatus': 'UPDATE_COMPLETE',
                'Outputs': [
                    {'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()
                ],
            }
        ]
    }
    runtime = Mock()
    runtime.get_function.return_value = {
        'Configuration': {'State': 'Active'},
        'Code': {'ResolvedImageUri': image},
    }
    runtime.get_function_concurrency.return_value = {'ReservedConcurrentExecutions': 10}
    session = Mock()
    session.client.side_effect = lambda service: {
        'cloudformation': cloudformation,
        'lambda': runtime,
    }[service]

    monkeypatch.setattr(deploy_module, 'read_health', Mock())

    monkeypatch.setattr(deploy_module, 'inspect_mcp_endpoint', Mock(return_value={'status': 401}))

    assert deploy_module.reconcile_mcp(session, None)['runtime_image'] == image
    runtime.get_function.return_value['Code']['ResolvedImageUri'] = 'fixture@sha256:' + 'b' * 64

    with pytest.raises(RuntimeError, match='image changed'):
        deploy_module.reconcile_mcp(session, None)

    secret_patterns = [
        re.compile(r'AKIA[0-9A-Z]{16}'),
        re.compile(r'ASIA[0-9A-Z]{16}'),
        re.compile(r'gh[pousr]_[A-Za-z0-9_]{20,}'),
        re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
        re.compile(r'eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'),
    ]
    email_pattern = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')
    findings = []
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')

    for name in filter(None, tracked):
        path = root / name

        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_text(errors='ignore')

        if any(pattern.search(text) for pattern in secret_patterns):
            findings.append(f'{name}: credential signature')

        for match in email_pattern.finditer(text):
            if match.group(1).lower() not in {'example.com', 'example.test'}:
                findings.append(f'{name}: non-fixture email')

    assert findings == []
