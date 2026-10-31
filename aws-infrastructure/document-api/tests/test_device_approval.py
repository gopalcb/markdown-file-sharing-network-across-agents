"""Test explicit administrator decisions and fail-closed deployment IAM prerequisites."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
import yaml
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from device_fixtures import fixture_database
from services.native_device_service import authorize_native_device
from services.validation_service import validate_native_proof
from test_upload import event_for

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('device_approval', ROOT / 'scripts/approve_device.py')
admin = importlib.util.module_from_spec(SPEC)

SPEC.loader.exec_module(admin)


@pytest.fixture
def ci_session(monkeypatch):
    """Supply the exact dedicated CI identity without accessing AWS."""

    monkeypatch.setenv('GITHUB_REPOSITORY', 'gopalcb/markdown-file-sharing-network-across-agents')

    monkeypatch.setenv('GITHUB_REF', 'refs/heads/main')

    monkeypatch.setenv('GITHUB_ACTOR', 'fixture-reviewer')

    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    session = Mock(region_name='ca-central-1')
    session.client.return_value.get_caller_identity.return_value = {
        'Account': '123456789012',
        'Arn': 'arn:aws:sts::123456789012:assumed-role/softrobots-gb-cicd-role/fixture',
    }

    return session


def test_exact_pending_key_required_for_approval_and_revocation(ci_session):
    """An explicit matching confirmation and validated pending key are required for changes."""
    database, rows = fixture_database()
    proof = validate_native_proof(event_for({'username': 'alice', 'action': 'register_device'}))
    assert authorize_native_device(database, proof, register=True) is False
    args = (database, proof['owner_id'], proof['fingerprint'])

    with pytest.raises(ValueError, match='confirmation'):
        admin.change_approval(*args, 'approve', '')
    assert not rows['softrobots-native-device-approvals']

    admin.inspect_pending(*args)
    confirmation = f'approve:{proof["owner_id"]}:{proof["fingerprint"]}'

    admin.change_approval(*args, 'approve', confirmation)
    state = rows['softrobots-native-device-approvals'][proof['approval_id']]
    assert state['status'] == 'APPROVED'
    assert state['actor'] == 'fixture-reviewer'
    assert state['workflow_run'] == '123'

    admin.change_approval(*args, 'revoke', confirmation.replace('approve:', 'revoke:'))
    assert rows['softrobots-native-device-approvals'][proof['approval_id']]['status'] == 'REVOKED'


@pytest.mark.parametrize('change', ['owner_id', 'fingerprint', 'issuer', 'subject', 'public_key'])
def test_corrupt_pending_never_approved(change, ci_session):
    """The pending table cannot substitute an unrelated key or owner under a selected key."""
    database, rows = fixture_database()
    proof = validate_native_proof(event_for({'username': 'alice', 'action': 'register_device'}))

    authorize_native_device(database, proof, register=True)
    pending = rows['softrobots-native-device-pending']['PENDING#' + proof['approval_id']]
    pending[change] = 'corrupted'

    with pytest.raises(ValueError):
        admin.change_approval(
            database,
            proof['owner_id'],
            proof['fingerprint'],
            'approve',
            f'approve:{proof["owner_id"]}:{proof["fingerprint"]}',
        )
    assert not rows['softrobots-native-device-approvals']


@pytest.mark.parametrize(
    'setting,value', [('GITHUB_REF', 'refs/heads/feature'), ('GITHUB_REPOSITORY', 'someone/fork')]
)
def test_admin_wrong_ci_context_rejected(ci_session, monkeypatch, setting, value):
    """Fork or non-main execution stops before credential use."""

    monkeypatch.setenv(setting, value)

    with pytest.raises(ValueError):
        admin.validate_ci_session(ci_session)

    ci_session.client.assert_not_called()


def test_preflight_blocks_missing_or_excessive_grants(ci_session):
    """IAM simulation must explicitly allow required actions and deny forbidden writes."""
    iam = ci_session.client.return_value
    iam.simulate_principal_policy.return_value = {'EvaluationResults': []}

    with pytest.raises(ValueError, match='prerequisite'):
        admin.preflight_permissions(ci_session)

    def decisions(**arguments):
        """Return the intended scoped policy decisions for each requested action."""

        if arguments['ActionNames'][0].startswith('s3:Delete'):
            return {
                'EvaluationResults': [
                    {'EvalActionName': action, 'EvalDecision': 'explicitDeny'}
                    for action in arguments['ActionNames']
                ]
            }
        table = arguments['ResourceArns'][0].split('/')[-1]
        leading = arguments.get('ContextEntries', [{}])[0].get('ContextKeyValues', [''])[0]
        allowed = not (
            leading.startswith('APPROVED#')
            or (
                table == admin.APPROVALS
                and arguments['ActionNames'][0] != 'dynamodb:GetItem'
                and arguments['PolicySourceArn'] == admin.ROLE
            )
            or (table == admin.NONCES and arguments['ActionNames'][0] == 'dynamodb:DeleteItem')
        )

        return {
            'EvaluationResults': [
                {'EvalActionName': action, 'EvalDecision': 'allowed' if allowed else 'implicitDeny'}
                for action in arguments['ActionNames']
            ]
        }

    iam.simulate_principal_policy.side_effect = lambda **arguments: {
        'EvaluationResults': [
            {'EvalActionName': action, 'EvalDecision': 'allowed'}
            for action in arguments['ActionNames']
        ]
    }

    with pytest.raises(ValueError, match='prerequisite'):
        admin.preflight_permissions(ci_session)

    iam.simulate_principal_policy.side_effect = decisions

    admin.preflight_permissions(ci_session)
    iam.simulate_principal_policy.side_effect = ClientError(
        {'Error': {'Code': 'AccessDenied'}}, 'SimulatePrincipalPolicy'
    )

    with pytest.raises(ClientError):
        admin.preflight_permissions(ci_session)


def test_workflow_and_policy_boundaries():
    """Deployment gates mutations; administrative approval remains explicit and main-only."""
    policies = json.loads((ROOT / 'iam/runtime-policy.json').read_text())['Statement']
    approval_grants = [
        statement
        for statement in policies
        if statement.get('Effect') == 'Allow'
        and str(statement.get('Resource', '')).endswith(admin.APPROVALS)
    ]
    assert len(approval_grants) == 1
    assert approval_grants[0]['Action'] == 'dynamodb:GetItem'
    template = yaml.load((ROOT / 'template.yaml').read_text(), Loader=yaml.BaseLoader)
    assert (
        template['Resources']['NativeDeviceNonces']['Properties']['TimeToLiveSpecification'][
            'Enabled'
        ]
        == 'true'
    )


def test_registry_sdk_parameter_contracts():
    """Serialize native enrollment, consistent approval reads and conditional nonce writes."""
    session = boto3.Session(
        aws_access_key_id='test', aws_secret_access_key='test', region_name='ca-central-1'
    )
    database = session.resource('dynamodb')
    proof = validate_native_proof(event_for({'username': 'alice', 'action': 'register_device'}))

    with Stubber(database.meta.client) as stub:
        stub.add_response(
            'get_item',
            {},
            {
                'TableName': admin.APPROVALS,
                'Key': {'approval_id': proof['approval_id']},
                'ConsistentRead': True,
            },
        )

        stub.add_response(
            'put_item',
            {},
            {
                'TableName': admin.NONCES,
                'Item': ANY,
                'ConditionExpression': 'attribute_not_exists(nonce_id)',
            },
        )

        stub.add_response(
            'put_item',
            {},
            {
                'TableName': admin.PENDING,
                'Item': ANY,
                'ConditionExpression': 'attribute_not_exists(pending_id)',
            },
        )
        assert authorize_native_device(database, proof, register=True) is False

        stub.assert_no_pending_responses()


@pytest.mark.parametrize('exists', [False, True])
def test_install_permissions_creates_or_reuses_exact_policy(ci_session, exists):
    """An existing matching managed policy is reusable; only the CI role is attached."""
    iam = ci_session.client.return_value
    arn = 'arn:aws:iam::123456789012:policy/SoftrobotsNativeDeviceApproval'
    policy = {'Arn': arn, 'DefaultVersionId': 'v1'}
    expected = json.loads((ROOT / 'iam/device-approval-policy.json').read_text())
    iam.get_policy.return_value = {'Policy': policy}

    if not exists:
        iam.get_policy.side_effect = ClientError({'Error': {'Code': 'NoSuchEntity'}}, 'GetPolicy')
    iam.create_policy.return_value = {'Policy': policy}
    iam.get_policy_version.return_value = {'PolicyVersion': {'Document': expected}}

    admin.install_approval_policy(ci_session)

    iam.attach_role_policy.assert_called_once_with(
        RoleName='softrobots-gb-cicd-role', PolicyArn=arn
    )

    if exists:
        iam.create_policy.assert_not_called()
    else:
        assert json.loads(iam.create_policy.call_args.kwargs['PolicyDocument']) == expected

    ci_session.resource.assert_not_called()


def test_install_permissions_rejects_existing_policy_drift(ci_session):
    """Never attach an unexpected policy or overwrite its contents during bootstrap."""
    iam = ci_session.client.return_value
    iam.get_policy.return_value = {
        'Policy': {
            'Arn': 'arn:aws:iam::123456789012:policy/SoftrobotsNativeDeviceApproval',
            'DefaultVersionId': 'v1',
        }
    }
    iam.get_policy_version.return_value = {'PolicyVersion': {'Document': {'Statement': []}}}

    with pytest.raises(ValueError, match='differs'):
        admin.install_approval_policy(ci_session)

    iam.attach_role_policy.assert_not_called()

    iam.create_policy.assert_not_called()
