"""Test the GitHub-only additive storage permission bootstrap without AWS calls."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    'storage_permissions',
    Path(__file__).resolve().parents[1] / 'scripts/configure_storage_permissions.py',
)
module = importlib.util.module_from_spec(SPEC)

SPEC.loader.exec_module(module)


@pytest.fixture
def session(monkeypatch):
    """Provide the exact authorized CI context using fake AWS clients."""

    monkeypatch.setenv('GITHUB_ACTIONS', 'true')

    monkeypatch.setenv('GITHUB_REPOSITORY', 'gopalcb/markdown-file-sharing-network-across-agents')

    monkeypatch.setenv('GITHUB_REF', 'refs/heads/main')
    session = Mock(region_name=module.REGION)
    sts, iam = Mock(), Mock()
    sts.get_caller_identity.return_value = {
        'Account': module.ACCOUNT,
        'Arn': f'arn:aws:sts::{module.ACCOUNT}:assumed-role/softrobots-gb-cicd-role/test',
    }
    session.client.side_effect = lambda service: {'sts': sts, 'iam': iam}[service]
    iam.get_role_policy.side_effect = lambda **args: {
        'PolicyDocument': json.loads(iam.put_role_policy.call_args.kwargs['PolicyDocument'])
    }

    return session


def test_additive_scoped_policy_update(session) -> None:
    """Only the two owned policies are written and read back; no trust changes occur."""

    module.configure_storage_permissions(session)
    iam = session.client('iam')
    assert iam.put_role_policy.call_count == 2
    assert iam.get_role_policy.call_count == 2

    for call in iam.put_role_policy.call_args_list:
        assert call.kwargs['PolicyName'] == 'AgentWorkHistoryStorage'
        document = json.loads(call.kwargs['PolicyDocument'])
        assert all(
            s['Resource'].startswith(module.BUCKET_ARN) or s['Resource'] == module.PROJECTS_ARN
            for s in document['Statement']
        )
        table_grant = next(s for s in document['Statement'] if s['Resource'] == module.PROJECTS_ARN)

        if call.kwargs['RoleName'] == 'softrobots-document-api-lambda-role':
            assert set(table_grant['Action']) == {
                'dynamodb:GetItem',
                'dynamodb:PutItem',
                'dynamodb:Scan',
            }
        else:
            assert 'dynamodb:CreateTable' in table_grant['Action']
            assert 'dynamodb:UpdateContinuousBackups' in table_grant['Action']

    iam.update_assume_role_policy.assert_not_called()

    iam.delete_role_policy.assert_not_called()


@pytest.mark.parametrize(
    'field,value',
    [
        ('GITHUB_ACTIONS', 'false'),
        ('GITHUB_REPOSITORY', 'other/repo'),
        ('GITHUB_REF', 'refs/heads/feature'),
    ],
)
def test_non_main_ci_context_rejected(session, monkeypatch, field, value) -> None:
    """A local or non-main run cannot mutate IAM."""

    monkeypatch.setenv(field, value)

    with pytest.raises(ValueError):
        module.configure_storage_permissions(session)

    session.client('iam').put_role_policy.assert_not_called()


def test_readback_mismatch_stops(session) -> None:
    """Do not claim success when the policy read-back differs."""
    iam = session.client('iam')
    iam.get_role_policy.side_effect = lambda **args: {'PolicyDocument': {}}

    with pytest.raises(ValueError):
        module.configure_storage_permissions(session)
    assert iam.put_role_policy.call_count == 1
