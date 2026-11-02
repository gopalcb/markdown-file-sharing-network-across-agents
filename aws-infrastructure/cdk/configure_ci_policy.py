"""Install the exact GitHub OIDC permissions required by the telemetry release."""

import argparse
import json

import boto3

ACCOUNT = '249766001220'
REGION = 'ca-central-1'
ROLE = 'softrobots-gb-cicd-role'
POLICY_NAME = 'SoftrobotsPortalServerlessDeployment'
POLICY_ARN = f'arn:aws:iam::{ACCOUNT}:policy/{POLICY_NAME}'
QUOTA_POLICY_ARN = f'arn:aws:iam::{ACCOUNT}:policy/SoftrobotsLambdaQuotaRequest'
LEGACY_MCP_ROLE_ARN = f'arn:aws:iam::{ACCOUNT}:role/markdown-mcp-service-github-role'


def deployment_policy() -> dict:
    """Return the bounded policy used by the main-only release workflow."""
    bootstrap_roles = [
        f'arn:aws:iam::{ACCOUNT}:role/cdk-hnb659fds-{name}-role-{ACCOUNT}-{REGION}'
        for name in ('deploy', 'file-publishing', 'image-publishing', 'lookup')
    ]
    stacks = [
        f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{name}/*'
        for name in (
            'softrobots-telemetry',
            'softrobots-portal-edge',
            'markdown-mcp-service-edge',
            'softrobots-document-api',
        )
    ]
    tables = [
        f'arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{name}'
        for name in (
            'softrobots-document-api-user-files',
            'softrobots-projects',
            'softrobots-document-api-local-deletions',
            'softrobots-native-device-pending',
            'softrobots-native-device-approvals',
            'softrobots-native-device-nonces',
        )
    ]

    return {
        'Version': '2012-10-17',
        'Statement': [
            {
                'Sid': 'AssumeExactCdkBootstrapRoles',
                'Effect': 'Allow',
                'Action': 'sts:AssumeRole',
                'Resource': bootstrap_roles,
            },
            {
                'Sid': 'ReadExactCdkBootstrapVersion',
                'Effect': 'Allow',
                'Action': 'ssm:GetParameter',
                'Resource': (
                    f'arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/cdk-bootstrap/hnb659fds/version'
                ),
            },
            {
                'Sid': 'InspectOwnedReleaseStacks',
                'Effect': 'Allow',
                'Action': [
                    'cloudformation:DescribeStacks',
                    'cloudformation:DescribeStackEvents',
                ],
                'Resource': stacks,
            },
            {
                'Sid': 'PublishAndReadTelemetryUi',
                'Effect': 'Allow',
                'Action': ['s3:PutObject', 's3:GetObject', 's3:GetObjectVersion'],
                'Resource': f'arn:aws:s3:::softrobots-telemetry-ui-{ACCOUNT}-{REGION}/*',
            },
            {
                'Sid': 'ListReleaseBuckets',
                'Effect': 'Allow',
                'Action': ['s3:ListBucket', 's3:GetBucketVersioning'],
                'Resource': [
                    f'arn:aws:s3:::softrobots-telemetry-ui-{ACCOUNT}-{REGION}',
                    f'arn:aws:s3:::agent-work-history--{ACCOUNT}-{REGION}',
                    f'arn:aws:s3:::softrobots-document-api-{ACCOUNT}-{REGION}',
                ],
            },
            {
                'Sid': 'InvalidateCanonicalEdges',
                'Effect': 'Allow',
                'Action': 'cloudfront:CreateInvalidation',
                'Resource': [
                    f'arn:aws:cloudfront::{ACCOUNT}:distribution/E224O80UCWC7IJ',
                    f'arn:aws:cloudfront::{ACCOUNT}:distribution/E3BS4AEVAMTQSF',
                ],
            },
            {
                'Sid': 'InspectReleaseFunctions',
                'Effect': 'Allow',
                'Action': [
                    'lambda:GetFunction',
                    'lambda:GetFunctionConfiguration',
                    'lambda:GetFunctionConcurrency',
                ],
                'Resource': [
                    f'arn:aws:lambda:{REGION}:{ACCOUNT}:function:telemetry-lambda',
                    f'arn:aws:lambda:{REGION}:{ACCOUNT}:function:softrobots-document-api-upload',
                ],
            },
            {
                'Sid': 'InspectRetainedTables',
                'Effect': 'Allow',
                'Action': 'dynamodb:DescribeTable',
                'Resource': tables,
            },
            {
                'Sid': 'InspectThisReviewedPolicy',
                'Effect': 'Allow',
                'Action': [
                    'iam:GetPolicy',
                    'iam:GetPolicyVersion',
                    'iam:ListPolicyVersions',
                ],
                'Resource': POLICY_ARN,
            },
            {
                'Sid': 'DenyDirectPolicyEscalation',
                'Effect': 'Deny',
                'Action': [
                    'iam:CreatePolicy',
                    'iam:CreatePolicyVersion',
                    'iam:SetDefaultPolicyVersion',
                    'iam:DeletePolicyVersion',
                    'iam:DeletePolicy',
                ],
                'Resource': [POLICY_ARN, QUOTA_POLICY_ARN],
            },
            {
                'Sid': 'DenyLegacyMcpRoleMutation',
                'Effect': 'Deny',
                'Action': [
                    'iam:CreateRole',
                    'iam:UpdateAssumeRolePolicy',
                    'iam:DeleteRole',
                    'iam:TagRole',
                    'iam:UntagRole',
                    'iam:PutRolePolicy',
                    'iam:DeleteRolePolicy',
                ],
                'Resource': LEGACY_MCP_ROLE_ARN,
            },
        ],
    }


def normalized(value: dict) -> str:
    """Serialize a policy deterministically for equality checks."""

    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def apply_policy() -> dict:
    """Converge once to the policy that revokes its own future write access."""
    session = boto3.Session(region_name=REGION)
    identity = session.client('sts').get_caller_identity()

    if identity.get('Account') != ACCOUNT:
        raise RuntimeError('CI policy update must target the approved AWS account')
    iam = session.client('iam')
    policy = iam.get_policy(PolicyArn=POLICY_ARN)['Policy']
    current = iam.get_policy_version(
        PolicyArn=POLICY_ARN,
        VersionId=policy['DefaultVersionId'],
    )['PolicyVersion']['Document']
    desired = deployment_policy()

    if normalized(current) == normalized(desired):
        return {'changed': False, 'policy': POLICY_NAME}
    versions = iam.list_policy_versions(PolicyArn=POLICY_ARN)['Versions']

    if len(versions) >= 5:
        raise RuntimeError('Deployment policy has no free version slot for hardening')
    created = iam.create_policy_version(
        PolicyArn=POLICY_ARN,
        PolicyDocument=json.dumps(desired),
        SetAsDefault=True,
    )['PolicyVersion']['VersionId']

    return {
        'changed': True,
        'policy': POLICY_NAME,
        'version': created,
        'direct_iam_mutation_denied': True,
    }


def main() -> None:
    """Validate or install the bounded release policy."""
    parser = argparse.ArgumentParser()

    parser.add_argument('operation', choices=('check', 'apply'))
    arguments = parser.parse_args()
    result = (
        {'valid': True, 'statements': len(deployment_policy()['Statement'])}
        if arguments.operation == 'check'
        else apply_policy()
    )

    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
