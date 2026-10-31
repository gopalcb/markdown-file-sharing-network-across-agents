"""Verify AWS SDK parameter contracts and Cognito bearer transport offline."""

import base64
import importlib.util
import io
import json
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock

import boto3
from botocore.stub import ANY, Stubber
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from device_fixtures import KEY

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from services.document_read_service import get_document, list_documents
from services.document_service import store_documents
from services.validation_service import path_document_identity

SPEC = importlib.util.spec_from_file_location(
    'document_upload', Path(__file__).resolve().parents[1] / 'scripts/upload.py'
)
upload = importlib.util.module_from_spec(SPEC)

SPEC.loader.exec_module(upload)
import device_signing
from services.validation_service import validate_native_proof


def test_sdk_contracts() -> None:
    """Use real boto3 serializers with stubbed AWS responses for a complete upload."""
    session = boto3.Session(
        aws_access_key_id='test', aws_secret_access_key='test', region_name='ca-central-1'
    )
    s3 = session.client('s3')
    table = session.resource('dynamodb').Table('user-files')

    file_id, key = path_document_identity('alice', 'workflow-docs/notes.md')
    row = {
        'username': {'S': 'alice'},
        'file_id': {'S': file_id},
        's3_path': {'S': f's3://documents/{key}'},
        'file-group': {'S': 'workflow-docs'},
        'created_at': {'S': '2026-10-30'},
        'status': {'S': 'READY'},
    }

    with Stubber(s3) as s3_stub, Stubber(table.meta.client) as table_stub:
        table_stub.add_response(
            'get_item',
            {},
            {
                'TableName': 'user-files',
                'Key': {'username': 'alice', 'file_id': file_id},
                'ConsistentRead': True,
            },
        )

        s3_stub.add_response(
            'put_object',
            {},
            {
                'Bucket': 'documents',
                'Key': key,
                'Body': b'# Notes',
                'ContentType': 'text/markdown; charset=utf-8',
                'ServerSideEncryption': 'AES256',
                'Metadata': {
                    'source-binding': sha256(b'\ndocs/workflow-docs/notes.md').hexdigest()
                },
                'IfNoneMatch': '*',
            },
        )

        table_stub.add_response(
            'update_item',
            {'Attributes': row},
            {
                'TableName': 'user-files',
                'Key': {'username': 'alice', 'file_id': file_id},
                'UpdateExpression': 'SET #f0 = :f0, #f1 = :f1, #f2 = if_not_exists(#f2, :f2), #f3 = :f3',
                'ExpressionAttributeNames': {
                    '#f0': 's3_path',
                    '#f1': 'file-group',
                    '#f2': 'created_at',
                    '#f3': 'status',
                },
                'ExpressionAttributeValues': {
                    ':f0': f's3://documents/{key}',
                    ':f1': 'workflow-docs',
                    ':f2': ANY,
                    ':f3': 'READY',
                },
                'ReturnValues': 'ALL_NEW',
            },
        )
        results = store_documents(
            {
                'username': 'alice',
                'files': [
                    {
                        'name': 'notes.md',
                        'category': 'workflow-docs',
                        'content': '# Notes',
                        'metadata': {'reviewed': False, 'revision': 1},
                    }
                ],
            },
            s3,
            table,
            'documents',
            'request',
        )
        assert results[0]['status'] == 'READY'

        s3_stub.assert_no_pending_responses()

        table_stub.assert_no_pending_responses()


def test_client_sends_bearer_to_cloudfront(monkeypatch) -> None:
    """Verify real client/server canonical compatibility using a synthetic signing boundary."""
    opener = Mock()
    opener.open.return_value = io.BytesIO(b'{"files":[]}')
    factory = Mock(return_value=opener)

    monkeypatch.setattr(upload, 'build_opener', factory)

    monkeypatch.setattr(
        upload,
        'device_headers',
        lambda: {'X-Device-Id': 'fixture-device', 'X-Device-Mac': '02:11:22:33:44:55'},
    )

    def synthetic_native(operation, **values):
        """Replace only hardware key access, preserving actual client signing canonicalization."""
        assert operation == 'sign'
        message = base64.b64decode(values['message'])

        return {
            'public_key': base64.b64encode(
                KEY.public_key().public_bytes(
                    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
                )
            ).decode(),
            'signature': base64.b64encode(KEY.sign(message, ec.ECDSA(hashes.SHA256()))).decode(),
        }

    monkeypatch.setattr(device_signing, 'native', synthetic_native)
    payload = {'username': 'alice', 'files': []}
    assert upload.send_upload(
        'https://example.cloudfront.net/documents', payload, 'fixture-token'
    ) == {'files': []}
    outgoing = opener.open.call_args.args[0]
    assert outgoing.get_header('Authorization') == 'Bearer fixture-token'
    assert json.loads(outgoing.data) == payload
    assert outgoing.get_header('X-device-id') == 'fixture-device'
    assert outgoing.get_header('X-device-mac') == '02:11:22:33:44:55'
    assert factory.call_args.args == (upload.NoRedirect,)
    proof = validate_native_proof(
        {
            'rawPath': '/documents',
            'headers': dict(outgoing.header_items()),
            'body': outgoing.data.decode(),
            'requestContext': {
                'http': {'method': 'POST'},
                'authorizer': {
                    'jwt': {'claims': {'iss': 'fixture-issuer', 'sub': 'fixture-subject'}}
                },
            },
        }
    )
    assert proof['public_key'] == outgoing.get_header('X-device-key')


def test_read_sdk_contracts() -> None:
    """Exercise real SDK scan/query/get serializers with private streamed content."""
    session = boto3.Session(
        aws_access_key_id='test', aws_secret_access_key='test', region_name='ca-central-1'
    )
    table = session.resource('dynamodb').Table('user-files')
    s3 = session.client('s3')
    file_id = 'b1cdfdcf-1a9c-4f25-9387-d185132abb17'
    payload = {'scope': 'all', 'username': 'alice', 'owner': 'alice', 'file_id': file_id}
    key = f'docs/user-docs/alice/notes-{file_id}.md'
    row = {
        'username': {'S': 'alice'},
        'file_id': {'S': file_id},
        'filename': {'S': 'notes.md'},
        'category': {'S': 'notes'},
        'created_at': {'S': '2026-10-27'},
        'status': {'S': 'READY'},
        'size_bytes': {'N': '7'},
        's3_bucket': {'S': 'documents'},
        's3_key': {'S': key},
    }
    stream = io.BytesIO(b'# Notes')

    with Stubber(table.meta.client) as table_stub, Stubber(s3) as s3_stub:
        table_stub.add_response(
            'scan',
            {'Items': [deepcopy(row)]},
            {
                'TableName': 'user-files',
                'Limit': 50,
                'FilterExpression': '#status = :ready',
                'ExpressionAttributeNames': {'#status': 'status', '#group': 'file-group'},
                'ExpressionAttributeValues': {':ready': 'READY'},
                'ProjectionExpression': ANY,
            },
        )

        table_stub.add_response(
            'query',
            {'Items': [deepcopy(row)]},
            {
                'TableName': 'user-files',
                'Limit': 50,
                'FilterExpression': '#status = :ready',
                'ExpressionAttributeNames': {'#status': 'status', '#group': 'file-group'},
                'ExpressionAttributeValues': {':ready': 'READY', ':username': 'alice'},
                'KeyConditionExpression': 'username = :username',
                'ProjectionExpression': ANY,
            },
        )

        table_stub.add_response(
            'get_item',
            {'Item': deepcopy(row)},
            {
                'TableName': 'user-files',
                'Key': {'username': 'alice', 'file_id': file_id},
                'ConsistentRead': True,
            },
        )

        s3_stub.add_response(
            'get_object', {'Body': stream, 'ContentLength': 7}, {'Bucket': 'documents', 'Key': key}
        )
        assert list_documents(payload, table)['documents'][0]['size_bytes'] == 7
        assert (
            list_documents({**payload, 'scope': 'mine'}, table)['documents'][0]['owner'] == 'alice'
        )
        assert get_document(payload, table, s3, 'documents')['document']['content'] == '# Notes'
        assert stream.closed

        table_stub.assert_no_pending_responses()

        s3_stub.assert_no_pending_responses()
