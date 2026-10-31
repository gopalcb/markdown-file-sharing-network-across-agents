"""Apply scoped document storage and project table grants through GitHub deployment."""

import json
import os
from pathlib import Path

import boto3

ACCOUNT = '123456789012'
REGION = 'ca-central-1'
BUCKET_ARN = f'arn:aws:s3:::agent-work-history--{ACCOUNT}-{REGION}'
POLICY_NAME = 'AgentWorkHistoryStorage'
PROJECTS_ARN = f'arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/softrobots-projects'


def configure_storage_permissions(session) -> None:
    """Verify GitHub identity, add only owned storage policies and read them back."""
    identity = session.client('sts').get_caller_identity()

    if (
        os.environ.get('GITHUB_ACTIONS') != 'true'
        or os.environ.get('GITHUB_REPOSITORY') != 'gopalcb/markdown-file-sharing-network-across-agents'
        or os.environ.get('GITHUB_REF') != 'refs/heads/main'
        or session.region_name != REGION
        or identity['Account'] != ACCOUNT
        or not identity['Arn'].startswith(
            f'arn:aws:sts::{ACCOUNT}:assumed-role/softrobots-gb-cicd-role/'
        )
    ):
        raise ValueError('Storage permission updates require the main GitHub deployment role')
    statements = json.loads(
        (Path(__file__).resolve().parents[1] / 'iam/cloudformation-policy.json').read_text()
    )['Statement']
    provisioner = next(item for item in statements if item.get('Sid') == 'ManageDocumentBucket')
    tables = next(item for item in statements if item.get('Sid') == 'ManageDocumentTables')
    policies = {
        'softrobots-document-api-cloudformation-role': [
            {**provisioner, 'Resource': BUCKET_ARN},
            {**tables, 'Resource': PROJECTS_ARN},
        ],
        'softrobots-document-api-lambda-role': [
            {
                'Effect': 'Allow',
                'Action': ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Scan'],
                'Resource': PROJECTS_ARN,
            },
            {
                'Effect': 'Allow',
                'Action': ['s3:PutObject', 's3:GetObject'],
                'Resource': BUCKET_ARN + '/agent-work/raw-markdown/*',
            },
            {
                'Effect': 'Deny',
                'Action': ['s3:DeleteObject', 's3:DeleteObjectVersion'],
                'Resource': BUCKET_ARN + '/*',
            },
        ],
    }
    iam = session.client('iam')

    for role, statements in policies.items():
        document = {'Version': '2012-10-17', 'Statement': statements}

        iam.put_role_policy(
            RoleName=role, PolicyName=POLICY_NAME, PolicyDocument=json.dumps(document)
        )
        actual = iam.get_role_policy(RoleName=role, PolicyName=POLICY_NAME)['PolicyDocument']

        if actual != document:
            raise ValueError('Storage policy read-back differs from the requested document')


def main() -> None:
    """Run the guarded additive IAM update only inside GitHub Actions."""

    configure_storage_permissions(boto3.Session(region_name=REGION))

    print('Agent work history storage policies applied and verified.')


if __name__ == '__main__':
    main()
