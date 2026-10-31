"""Exercise deployment configuration discovery without credentials or AWS mutations."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'resolve_auth_config', ROOT / 'scripts/resolve_auth_config.py'
)
config = importlib.util.module_from_spec(SPEC)

SPEC.loader.exec_module(config)


@pytest.fixture
def session():
    """Provide a completed, tagged stack in the dedicated CI identity."""
    session = Mock(region_name=config.REGION)
    sts, cloudformation = Mock(), Mock()
    session.client.side_effect = lambda name: {'sts': sts, 'cloudformation': cloudformation}[name]
    sts.get_caller_identity.return_value = {
        'Account': config.ACCOUNT,
        'Arn': config.ROLE_PREFIX + 'fixture',
    }
    cloudformation.describe_stacks.return_value = {
        'Stacks': [
            {
                'StackId': f'arn:aws:cloudformation:{config.REGION}:{config.ACCOUNT}:stack/{config.STACK}/fixture',
                'StackStatus': 'UPDATE_COMPLETE',
                'Tags': [{'Key': 'Project', 'Value': 'softrobots-systems'}],
                'Outputs': [
                    {'OutputKey': 'UserPoolId', 'OutputValue': config.POOL},
                    {'OutputKey': 'UploadClientId', 'OutputValue': 'testclient123'},
                    {
                        'OutputKey': 'SessionDevicesTableName',
                        'OutputValue': 'softrobots-auth-sessions',
                    },
                ],
            }
        ]
    }

    original = cloudformation.describe_stacks.return_value
    browser = {
        'Stacks': [
            {
                'StackId': f'arn:aws:cloudformation:{config.REGION}:{config.ACCOUNT}:stack/{config.BROWSER_STACK}/fixture',
                'StackStatus': 'UPDATE_COMPLETE',
                'Tags': [{'Key': 'Project', 'Value': 'softrobots-systems'}],
                'Outputs': [
                    {'OutputKey': 'UserPoolId', 'OutputValue': config.BROWSER_POOL},
                    {'OutputKey': 'UploadClientId', 'OutputValue': config.BROWSER_CLIENT},
                ],
            }
        ],
    }
    cloudformation.describe_stacks.side_effect = lambda StackName: (
        original if StackName == config.STACK else browser
    )
    session.browser_state = browser['Stacks'][0]

    return session


def test_resolves_without_repository_variables(session):
    """The activated stack alone provides the two deployment values."""
    expected = {
        'UPLOAD_POOL': config.POOL,
        'UPLOAD_CLIENT': 'testclient123',
        'BROWSER_UPLOAD_POOL': config.BROWSER_POOL,
        'BROWSER_UPLOAD_CLIENT': config.BROWSER_CLIENT,
    }
    assert config.resolve_config(session) == expected
    assert config.resolve_config(session, config.POOL, 'testclient123') == expected


@pytest.mark.parametrize(
    'pool,client',
    [(config.POOL, ''), ('', 'testclient123'), ('wrong', 'testclient123'), (config.POOL, 'wrong')],
)
def test_bad_overrides_fail_closed(session, pool, client):
    """Partial or drifting explicit configuration cannot override the trusted stack."""

    with pytest.raises(ValueError):
        config.resolve_config(session, pool, client)


@pytest.mark.parametrize(
    'key,value',
    [
        ('StackStatus', 'UPDATE_IN_PROGRESS'),
        ('StackStatus', 'UPDATE_ROLLBACK_COMPLETE'),
        ('StackId', 'wrong'),
        ('Tags', []),
        ('Outputs', []),
    ],
)
def test_unready_or_wrong_stack_is_rejected(session, key, value):
    """Incomplete, unowned or incorrectly identified stacks cannot authorize deployment."""
    session.client('cloudformation').describe_stacks.return_value['Stacks'][0][key] = value

    with pytest.raises(ValueError):
        config.resolve_config(session)


@pytest.mark.parametrize(
    'key,value',
    [
        ('UserPoolId', 'ca-central-1_Other'),
        ('UploadClientId', 'client\nINJECT=value'),
        ('SessionDevicesTableName', 'other'),
    ],
)
def test_outputs_are_bound_to_family_pool_and_safe_environment_values(session, key, value):
    """Wrong resources and environment-file injection fail before any export."""
    outputs = session.client('cloudformation').describe_stacks.return_value['Stacks'][0]['Outputs']

    for item in outputs:
        if item['OutputKey'] == key:
            item['OutputValue'] = value

    with pytest.raises(ValueError):
        config.resolve_config(session)


def test_wrong_identity_stops_before_stack_read(session):
    """Ambient administrator credentials cannot satisfy CI identity validation."""
    session.client('sts').get_caller_identity.return_value['Arn'] = (
        f'arn:aws:iam::{config.ACCOUNT}:user/admin'
    )

    with pytest.raises(ValueError):
        config.resolve_config(session)

    session.client('cloudformation').describe_stacks.assert_not_called()


@pytest.mark.parametrize(
    'code,message,expected',
    [
        ('ValidationError', 'Stack does not exist', ValueError),
        ('AccessDenied', 'Denied', ClientError),
    ],
)
def test_missing_stack_has_recovery_message_and_denials_propagate(session, code, message, expected):
    """Only actual missing-stack failures become activation guidance."""
    session.client('cloudformation').describe_stacks.side_effect = ClientError(
        {'Error': {'Code': code, 'Message': message}}, 'DescribeStacks'
    )

    with pytest.raises(expected) as failure:
        config.resolve_config(session)

    if expected is ValueError:
        assert 'portal-auth.yml' in str(failure.value)


def test_workflow_resolves_before_mutations_and_guards_diagnostics() -> None:
    """Standalone validation cannot obtain AWS credentials or deploy infrastructure."""
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.load((root / '.github/workflows/validate.yml').read_text(), Loader=yaml.BaseLoader)
    assert workflow['permissions'] == {'contents': 'read'}
    assert set(workflow['jobs']) == {'validate'}
    text = (root / '.github/workflows/validate.yml').read_text()
    for forbidden in ('configure-aws-credentials', 'id-token', 'cloudformation deploy', 'aws s3api'):
        assert forbidden not in text


def test_cli_exports_only_validated_identifiers(session, monkeypatch, tmp_path):
    """GitHub environment output is written only after successful configuration validation."""
    destination = tmp_path / 'github.env'

    monkeypatch.setenv('GITHUB_ENV', str(destination))

    monkeypatch.delenv('UPLOAD_POOL_OVERRIDE', raising=False)

    monkeypatch.delenv('UPLOAD_CLIENT_OVERRIDE', raising=False)

    monkeypatch.setattr(config.boto3, 'Session', lambda **kwargs: session)

    config.main()
    assert destination.read_text() == (
        f'UPLOAD_POOL={config.POOL}\nUPLOAD_CLIENT=testclient123\n'
        f'BROWSER_UPLOAD_POOL={config.BROWSER_POOL}\nBROWSER_UPLOAD_CLIENT={config.BROWSER_CLIENT}\n'
    )

    monkeypatch.setenv('UPLOAD_CLIENT_OVERRIDE', 'partial')

    with pytest.raises(ValueError):
        config.main()
    assert destination.read_text() == (
        f'UPLOAD_POOL={config.POOL}\nUPLOAD_CLIENT=testclient123\n'
        f'BROWSER_UPLOAD_POOL={config.BROWSER_POOL}\nBROWSER_UPLOAD_CLIENT={config.BROWSER_CLIENT}\n'
    )


@pytest.mark.parametrize(
    'key,value',
    [
        ('StackId', 'wrong'),
        ('StackStatus', 'UPDATE_IN_PROGRESS'),
        ('Tags', []),
        ('Outputs', []),
    ],
)
def test_browser_stack_must_be_completed_owned_and_exact(session, key, value):
    """A second issuer cannot be added from an unready, unowned or drifting stack."""
    session.browser_state[key] = value

    with pytest.raises(ValueError):
        config.resolve_config(session)


def test_browser_stack_cannot_supply_another_upload_client(session):
    """The separate route accepts only the approved existing username upload client."""
    session.browser_state['Outputs'][1]['OutputValue'] = 'otherclient'

    with pytest.raises(ValueError):
        config.resolve_config(session)
