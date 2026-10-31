"""Run the real Lambda handler against temporary Moto storage without AWS access."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import boto3
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'aws-infrastructure/document-api/src'))
from app import lambda_handler


def main() -> None:
    """Upload through the trusted handler, read bytes back, and assert stable file identity."""
    os.environ.update(
        AWS_ACCESS_KEY_ID='testing', AWS_SECRET_ACCESS_KEY='testing', AWS_SESSION_TOKEN='',
        AWS_DEFAULT_REGION='us-east-1', AWS_EC2_METADATA_DISABLED='true',
        DOCUMENTS_BUCKET='local-markdown-documents', USER_FILES_TABLE='local-document-index',
    )
    context = SimpleNamespace(aws_request_id='local-smoke')
    with mock_aws():
        boto3.client('s3').create_bucket(Bucket=os.environ['DOCUMENTS_BUCKET'])
        boto3.resource('dynamodb').create_table(
            TableName=os.environ['USER_FILES_TABLE'],
            KeySchema=[{'AttributeName': 'username', 'KeyType': 'HASH'},
                       {'AttributeName': 'file_id', 'KeyType': 'RANGE'}],
            AttributeDefinitions=[{'AttributeName': 'username', 'AttributeType': 'S'},
                                  {'AttributeName': 'file_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST',
        )
        # Direct invocation is an IAM-protected adapter boundary in AWS, never a public HTTP route.
        command = {'operation': 'upload_documents', 'username': 'alice', 'upload': {
            'file-group': 'knowledge', 'files': [{'name': 'hello.md', 'content': '# Shared Markdown\n', 'metadata': {}}],
        }}
        response = lambda_handler(command, context)
        assert response['statusCode'] == 201, response
        receipt = json.loads(response['body'])['files'][0]
        assert receipt['status'] == 'READY' and receipt['file_id'], receipt
        read = lambda_handler({'operation': 'get_document', 'username': 'alice',
                               'owner': 'alice', 'scope': 'mine', 'file_id': receipt['file_id']}, context)
        assert read['statusCode'] == 200, read
        assert '# Shared Markdown' in read['body'], read
        repeated = lambda_handler(command, context)
        assert repeated['statusCode'] == 201, repeated
        assert json.loads(repeated['body'])['files'][0]['file_id'] == receipt['file_id']
        print(json.dumps({'mode': 'offline-moto', 'upload': 201, 'read': 200,
                          'status': receipt['status'], 'stable_file_id': True}))


if __name__ == '__main__':
    main()
