"""Inspect or explicitly approve/revoke one pending native key in main-only CI.

Preflight only simulates IAM. Explicit install-permissions installs the reviewed
managed policy after its scoped bootstrap has been deployed through auth CI.
"""

import argparse
import base64
import json
import os
import re
from hashlib import sha256
from pathlib import Path
from time import time

import boto3
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ACCOUNT = '123456789012'
REGION = 'ca-central-1'
ROLE = f'arn:aws:iam::{ACCOUNT}:role/softrobots-document-api-lambda-role'
TABLE_PREFIX = f'arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/'
PENDING = 'softrobots-native-device-pending'
APPROVALS = 'softrobots-native-device-approvals'
NONCES = 'softrobots-native-device-nonces'
DELETIONS = 'softrobots-document-api-local-deletions'


def validate_ci_session(session) -> None:
    """Require the dedicated account, region and main-branch GitHub OIDC role."""

    if (
        os.environ.get('GITHUB_REPOSITORY') != 'gopalcb/markdown-file-sharing-network-across-agents'
        or os.environ.get('GITHUB_REF') != 'refs/heads/main'
    ):
        raise ValueError('Only the canonical main-branch GitHub workflow may operate devices')
    identity = session.client('sts').get_caller_identity()

    if (
        session.region_name != REGION
        or identity['Account'] != ACCOUNT
        or not identity['Arn'].startswith(
            f'arn:aws:sts::{ACCOUNT}:assumed-role/softrobots-gb-cicd-role/'
        )
    ):
        raise ValueError('Unexpected CI account, role or region')


def preflight_permissions(session) -> None:
    """Fail closed unless scoped runtime and provisioner grants are already effective."""

    validate_ci_session(session)
    iam = session.client('iam')
    checks = [
        (ROLE, PENDING, ['dynamodb:GetItem', 'dynamodb:PutItem'], True, 'PENDING#fixture'),
        (ROLE, PENDING, ['dynamodb:PutItem'], False, 'APPROVED#fixture'),
        (ROLE, APPROVALS, ['dynamodb:GetItem'], True, ''),
        (
            ROLE,
            APPROVALS,
            [
                'dynamodb:PutItem',
                'dynamodb:UpdateItem',
                'dynamodb:DeleteItem',
                'dynamodb:BatchWriteItem',
                'dynamodb:PartiQLInsert',
                'dynamodb:PartiQLUpdate',
                'dynamodb:PartiQLDelete',
            ],
            False,
            '',
        ),
        (ROLE, DELETIONS, ['dynamodb:GetItem', 'dynamodb:PutItem'], True, ''),
        (ROLE, NONCES, ['dynamodb:PutItem'], True, ''),
        (ROLE, NONCES, ['dynamodb:DeleteItem', 'dynamodb:UpdateItem'], False, ''),
        (ROLE, 'softrobots-auth-sessions', ['dynamodb:GetItem', 'dynamodb:PutItem'], True, ''),
    ]
    provisioner = f'arn:aws:iam::{ACCOUNT}:role/softrobots-document-api-cloudformation-role'

    for table in (PENDING, APPROVALS, NONCES):
        checks.append(
            (
                provisioner,
                table,
                [
                    'dynamodb:CreateTable',
                    'dynamodb:DescribeTable',
                    'dynamodb:UpdateTable',
                    'dynamodb:UpdateContinuousBackups',
                    'dynamodb:UpdateTimeToLive',
                    'dynamodb:DescribeTimeToLive',
                ],
                True,
                '',
            )
        )

    checks.append(
        (
            provisioner,
            DELETIONS,
            [
                'dynamodb:CreateTable',
                'dynamodb:DescribeTable',
                'dynamodb:UpdateTable',
                'dynamodb:UpdateContinuousBackups',
            ],
            True,
            '',
        )
    )

    for role, table, actions, allowed, leading_key in checks:
        arguments = {
            'PolicySourceArn': role,
            'ActionNames': actions,
            'ResourceArns': [TABLE_PREFIX + table],
        }

        if leading_key:
            arguments['ContextEntries'] = [
                {
                    'ContextKeyName': 'dynamodb:LeadingKeys',
                    'ContextKeyValues': [leading_key],
                    'ContextKeyType': 'stringList',
                }
            ]
        results = iam.simulate_principal_policy(**arguments)['EvaluationResults']

        if len(results) != len(actions) or any(
            (result['EvalDecision'] == 'allowed') != allowed or result.get('MissingContextValues')
            for result in results
        ):
            raise ValueError(
                f'Scoped device IAM prerequisite missing or excessive: {table} {actions}'
            )

    object_prefix = 'arn:aws:s3:::agent-work-history--123456789012-ca-central-1/'
    storage_checks = [
        (
            f'agent-work/raw-markdown/fixture/{category}/fixture.md',
            ['s3:PutObject', 's3:GetObject'],
        )
        for category in ('workflow-docs', 'worksummary', 'knowledge', 'custom-group')
    ]

    storage_checks.append(
        (
            'arn:aws:s3:::softrobots-document-api-123456789012-ca-central-1/docs/user-docs/fixture/fixture.md',
            ['s3:GetObject'],
        )
    )

    for key, actions in storage_checks:
        results = iam.simulate_principal_policy(
            PolicySourceArn=ROLE,
            ActionNames=actions,
            ResourceArns=[key if key.startswith('arn:') else object_prefix + key],
        )['EvaluationResults']

        if len(results) != len(actions) or any(
            result['EvalDecision'] != 'allowed' or result.get('MissingContextValues')
            for result in results
        ):
            raise ValueError('Document storage IAM prerequisite is missing')

    denied_actions = ['s3:DeleteObject', 's3:DeleteObjectVersion']
    deletion_checks = iam.simulate_principal_policy(
        PolicySourceArn=ROLE,
        ActionNames=denied_actions,
        ResourceArns=[
            'arn:aws:s3:::agent-work-history--123456789012-ca-central-1/agent-work/raw-markdown/fixture/workflow-docs/fixture.md'
        ],
    )['EvaluationResults']

    if len(deletion_checks) != len(denied_actions) or any(
        result['EvalDecision'] != 'explicitDeny' for result in deletion_checks
    ):
        raise ValueError('Document deletion deny prerequisite is missing')


def inspect_pending(database, owner_id: str, fingerprint: str) -> dict:
    """Read and cryptographically validate the exact enrollment selected by an operator."""

    if not re.fullmatch('[0-9a-f]{64}', owner_id) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
        raise ValueError('Exact lowercase SHA256 owner identifier and key fingerprint required')
    item = (
        database.Table(PENDING)
        .get_item(Key={'pending_id': f'PENDING#{owner_id}#{fingerprint}'}, ConsistentRead=True)
        .get('Item')
    )

    if not item:
        raise ValueError('Pending key not found; inspect the exact enrollment first')
    public_der = base64.b64decode(item['public_key'], validate=True)
    key = serialization.load_der_public_key(public_der)
    derived_owner = sha256(
        json.dumps([item['issuer'], item['subject']], separators=(',', ':')).encode()
    ).hexdigest()

    if (
        derived_owner != owner_id
        or item['owner_id'] != owner_id
        or item['fingerprint'] != fingerprint
        or sha256(public_der).hexdigest() != fingerprint
        or not isinstance(key, ec.EllipticCurvePublicKey)
        or not isinstance(key.curve, ec.SECP256R1)
        or key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        != public_der
    ):
        raise ValueError('Pending key does not match the exact owner and fingerprint')

    return item


def change_approval(
    database, owner_id: str, fingerprint: str, action: str, confirmation: str
) -> None:
    """Inspect enrollment and write only the operator's exact approved or revoked key."""

    if action not in ('approve', 'revoke') or confirmation != f'{action}:{owner_id}:{fingerprint}':
        raise ValueError('Explicit action:owner_id:fingerprint confirmation required')
    pending = inspect_pending(database, owner_id, fingerprint)

    database.Table(APPROVALS).put_item(
        Item={
            'approval_id': f'{owner_id}#{fingerprint}',
            'owner_id': owner_id,
            'fingerprint': fingerprint,
            'issuer': pending['issuer'],
            'subject': pending['subject'],
            'status': 'APPROVED' if action == 'approve' else 'REVOKED',
            'updated_at': int(time()),
            'actor': os.environ['GITHUB_ACTOR'],
            'workflow_run': os.environ['GITHUB_RUN_ID'],
        }
    )


def install_approval_policy(session) -> None:
    """Install only the reviewed named managed policy; reject unexpected existing contents."""

    validate_ci_session(session)
    iam = session.client('iam')
    arn = f'arn:aws:iam::{ACCOUNT}:policy/SoftrobotsNativeDeviceApproval'
    document = json.loads(
        (Path(__file__).resolve().parents[1] / 'iam/device-approval-policy.json').read_text()
    )

    try:
        policy = iam.get_policy(PolicyArn=arn)['Policy']
    except ClientError as error:
        if error.response['Error']['Code'] != 'NoSuchEntity':
            raise
        policy = iam.create_policy(
            PolicyName='SoftrobotsNativeDeviceApproval',
            PolicyDocument=json.dumps(document),
            Description='Explicit native device inspection and approval through main-only CI',
        )['Policy']

    if policy['Arn'] != arn:
        raise ValueError('Unexpected managed policy identity')
    existing = iam.get_policy_version(PolicyArn=arn, VersionId=policy['DefaultVersionId'])[
        'PolicyVersion'
    ]['Document']

    if existing != document:
        raise ValueError('Existing approval policy differs; review before changing permissions')

    iam.attach_role_policy(RoleName='softrobots-gb-cicd-role', PolicyArn=arn)


def main() -> None:
    """Run IAM preflight or one explicitly selected administrative device operation."""
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        'action', choices=['preflight', 'install-permissions', 'inspect', 'approve', 'revoke']
    )

    parser.add_argument('--owner-id', default='')

    parser.add_argument('--fingerprint', default='')

    parser.add_argument('--confirmation', default='')
    args = parser.parse_args()
    session = boto3.Session(region_name=REGION)

    validate_ci_session(session)

    if args.action == 'install-permissions':
        install_approval_policy(session)

        print('Scoped native device permission policy installed; no device approved.')

        return

    if args.action == 'preflight':
        preflight_permissions(session)

        print('Native-device IAM preflight passed; deployment remains separate.')

        return
    database = session.resource('dynamodb')
    pending = inspect_pending(database, args.owner_id, args.fingerprint)

    print(
        json.dumps(
            {
                key: pending[key]
                for key in (
                    'owner_id',
                    'issuer',
                    'subject',
                    'fingerprint',
                    'device_id',
                    'device_mac',
                )
            },
            sort_keys=True,
        )
    )

    if args.action != 'inspect':
        change_approval(database, args.owner_id, args.fingerprint, args.action, args.confirmation)

        print(f'{args.action} completed for the inspected exact owner/key.')


if __name__ == '__main__':
    main()
