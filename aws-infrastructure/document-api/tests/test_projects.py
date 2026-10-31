"""Verify shared project catalog durability, duplicate guards and scoped provisioning."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import app
import pytest
import yaml
from botocore.exceptions import ClientError
from services.project_service import ProjectExists, create_project, list_projects
from services.project_validation_service import validate_project_event


def create_command(name: str = 'Website', description: str = 'A project') -> dict:
    """Build the trusted request produced by the authenticated portal bridge."""

    return {
        'operation': 'create_project',
        'username': 'alice',
        'project': {'name': name, 'description': description},
    }


def conditional_conflict() -> ClientError:
    """Model DynamoDB's real failed conditional write response."""

    return ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem')


def test_atomic_create_and_same_owner_retry() -> None:
    """Unique names use conditional writes; an identical owner retry returns its original row."""
    table = Mock()
    payload = validate_project_event(create_command(' Website '))
    body, status = create_project(payload, table)
    row = table.put_item.call_args.kwargs['Item']
    assert status == 201 and body['project']['name'] == 'Website'
    assert body['project']['owner'] == 'alice'
    assert len(body['project']['id']) == 64
    assert (
        table.put_item.call_args.kwargs['ConditionExpression'] == 'attribute_not_exists(project_id)'
    )
    table.put_item.side_effect = conditional_conflict()
    table.get_item.return_value = {'Item': row}
    repeated, status = create_project(validate_project_event(create_command('WEBSITE')), table)
    assert status == 200 and repeated == body

    table.get_item.assert_called_once_with(
        Key={'project_id': row['project_id']}, ConsistentRead=True
    )

    with pytest.raises(ProjectExists):
        create_project(
            validate_project_event(create_command('website', 'Changed description')), table
        )

    with pytest.raises(ProjectExists):
        create_project(
            validate_project_event({**create_command('website'), 'username': 'bob'}), table
        )


def test_unicode_normalization_and_casefold_share_project_id() -> None:
    """Equivalent Unicode/case names cannot create visually duplicate catalog entries."""
    first = validate_project_event(create_command('Straße'))['project']['id']
    assert first == validate_project_event(create_command('STRASSE'))['project']['id']
    assert (
        validate_project_event(create_command('ＡＢＣ'))['project']['id']
        == validate_project_event(create_command('abc'))['project']['id']
    )


@pytest.mark.parametrize(
    'changes',
    [
        {'requestContext': {}},
        {'username': '../owner'},
        {'project': {'name': '', 'description': ''}},
        {'project': {'name': 'x' * 81, 'description': ''}},
        {'project': {'name': 'x\ny', 'description': ''}},
        {'project': {'name': 'x', 'description': 'y' * 2001}},
        {'project': {'name': 'x', 'description': '', 'owner': 'bob'}},
        {'project': {'name': 4, 'description': ''}},
        {'table': 'another-table'},
    ],
)
def test_invalid_commands_do_not_reach_aws(changes, monkeypatch) -> None:
    """Reject public dispatch, ownership overrides and malformed data before table access."""
    session = Mock()

    monkeypatch.setattr(app.boto3, 'Session', session)
    result = app.lambda_handler(
        {**create_command(), **changes}, SimpleNamespace(aws_request_id='r')
    )
    assert result['statusCode'] == 400

    session.assert_not_called()


def test_list_is_shared_bounded_and_cursor_identity_bound() -> None:
    """All authorized members see catalog pages; a cursor only selects a validated table key."""
    table = Mock()
    row = {
        'project_id': 'a' * 64,
        'name': 'Website',
        'description': '',
        'owner': 'alice',
        'created_at': '2026-10-30T00:00:00Z',
        'private': 'hidden',
    }
    table.scan.return_value = {
        'Items': [row],
        'LastEvaluatedKey': {'project_id': row['project_id']},
    }
    command = {'operation': 'list_projects', 'username': 'bob'}
    result = list_projects(validate_project_event(command), table)
    assert result['projects'][0]['owner'] == 'alice'
    assert 'private' not in result['projects'][0]
    assert table.scan.call_args.kwargs['Limit'] == 50
    next_page = validate_project_event({**command, 'cursor': result['cursor']})
    assert next_page['start_key'] == {'project_id': 'a' * 64}

    list_projects(next_page, table)
    assert table.scan.call_args.kwargs['ExclusiveStartKey'] == {'project_id': 'a' * 64}

    with pytest.raises(ValueError):
        validate_project_event({**command, 'username': 'alice', 'cursor': result['cursor']})
    forged = base64.urlsafe_b64encode(
        json.dumps({'username': 'bob', 'id': '../other'}).encode()
    ).decode()

    with pytest.raises(ValueError):
        validate_project_event({**command, 'cursor': forged})


def test_lambda_uses_dedicated_configured_table_only(monkeypatch) -> None:
    """Projects never depend on document/S3 storage and return the published contract."""
    session = Mock()

    monkeypatch.setattr(app.boto3, 'Session', Mock(return_value=session))

    monkeypatch.setenv('PROJECTS_TABLE', 'softrobots-projects')
    result = app.lambda_handler(create_command(), SimpleNamespace(aws_request_id='r'))
    assert result['statusCode'] == 201
    assert json.loads(result['body'])['project']['owner'] == 'alice'

    session.resource.return_value.Table.assert_called_once_with('softrobots-projects')

    session.client.assert_not_called()


def test_table_retention_encryption_billing_and_ci_bootstrap() -> None:
    """The existing workflow applies table grants before deploying its retained resource."""
    root = Path(__file__).resolve().parents[1]
    template = yaml.load((root / 'template.yaml').read_text(), Loader=yaml.BaseLoader)
    resource = template['Resources']['Projects']
    assert resource['DeletionPolicy'] == resource['UpdateReplacePolicy'] == 'Retain'
    properties = resource['Properties']
    assert properties['TableName'] == 'softrobots-projects'
    assert properties['BillingMode'] == 'PAY_PER_REQUEST'
    assert properties['SSESpecification']['SSEEnabled'] == 'true'
    assert properties['PointInTimeRecoverySpecification']['PointInTimeRecoveryEnabled'] == 'true'
    assert properties['KeySchema'] == [{'AttributeName': 'project_id', 'KeyType': 'HASH'}]
    assert (
        template['Resources']['UploadFunction']['Properties']['Environment']['Variables'][
            'PROJECTS_TABLE'
        ]
        == 'Projects'
    )
    assert properties['BillingMode'] == 'PAY_PER_REQUEST'
