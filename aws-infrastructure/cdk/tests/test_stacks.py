"""Assert the one-Lambda telemetry topology without AWS access."""

import importlib.util
import os
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

CDK_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(CDK_ROOT))

from edge_stack import PortalEdgeStack  # noqa: E402
from identity_stack import PortalIdentityStack  # noqa: E402
from mcp_edge_stack import McpEdgeStack  # noqa: E402
from telemetry_stack import TelemetryStack  # noqa: E402


def test_example_app_synthesizes_all_phases_and_rejects_real_accounts(tmp_path, monkeypatch):
    """Exercise app routing and the fictitious-account guard without AWS calls."""
    spec = importlib.util.spec_from_file_location('example_topology', CDK_ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app_factory = cdk.App
    monkeypatch.setenv('CDK_DEFAULT_ACCOUNT', '123456789012')
    monkeypatch.setenv('CDK_DEFAULT_REGION', 'ca-central-1')
    for phase in ['prepare', 'cutover', 'identity', 'retire', 'mcp-prepare', 'mcp-cutover']:
        monkeypatch.setattr(
            module.cdk,
            'App',
            lambda phase=phase: app_factory(context={'phase': phase}, outdir=str(tmp_path / phase)),
        )
        assert module.build_app().synth().stacks
    monkeypatch.setenv('CDK_DEFAULT_ACCOUNT', '999999999999')
    with pytest.raises(ValueError, match='fictitious'):
        module.build_app()


def stacks() -> tuple[Template, Template, Template]:
    """Synthesize replacement and edge templates in the approved environment."""
    os.environ['CDK_DEFAULT_ACCOUNT'] = '123456789012'
    os.environ['CDK_DEFAULT_REGION'] = 'ca-central-1'
    app = cdk.App()
    telemetry_stack = TelemetryStack(
        app,
        'Telemetry',
        env=cdk.Environment(account='123456789012', region='ca-central-1'),
    )
    edge_stack = PortalEdgeStack(
        app,
        'Edge',
        telemetry=telemetry_stack,
        env=cdk.Environment(account='123456789012', region='ca-central-1'),
    )
    mcp_edge_stack = McpEdgeStack(
        app,
        'McpEdge',
        telemetry=telemetry_stack,
        env=cdk.Environment(account='123456789012', region='ca-central-1'),
    )

    return (
        Template.from_stack(telemetry_stack),
        Template.from_stack(edge_stack),
        Template.from_stack(mcp_edge_stack),
    )


def test_one_application_lambda_with_bounded_concurrency() -> None:
    """Keep the final Work Organizer runtime to one explicitly capped Lambda."""
    telemetry, _, _ = stacks()

    telemetry.resource_count_is('AWS::Lambda::Function', 1)

    telemetry.has_resource_properties(
        'AWS::Lambda::Function',
        {
            'FunctionName': 'telemetry-lambda',
            'PackageType': 'Image',
            'ReservedConcurrentExecutions': 10,
            'MemorySize': 512,
            'Timeout': 28,
        },
    )


def test_native_uploads_keep_distinct_authorizers() -> None:
    """Preserve both native Cognito trust boundaries over the shared runtime."""
    telemetry, _, _ = stacks()

    telemetry.resource_count_is('AWS::ApiGatewayV2::Authorizer', 2)

    telemetry.has_resource_properties(
        'AWS::ApiGatewayV2::Route',
        {'RouteKey': 'POST /documents', 'AuthorizationType': 'JWT'},
    )

    telemetry.has_resource_properties(
        'AWS::ApiGatewayV2::Route',
        {'RouteKey': 'POST /browser-documents', 'AuthorizationType': 'JWT'},
    )


def test_ui_is_private_and_edge_updates_existing_distribution() -> None:
    """Require private S3/OAC and retain the canonical distribution logical ID."""
    telemetry, edge, _ = stacks()

    telemetry.has_resource_properties(
        'AWS::S3::Bucket',
        {
            'PublicAccessBlockConfiguration': {
                'BlockPublicAcls': True,
                'BlockPublicPolicy': True,
                'IgnorePublicAcls': True,
                'RestrictPublicBuckets': True,
            },
            'VersioningConfiguration': {'Status': 'Enabled'},
        },
    )

    edge.has_resource(
        'AWS::CloudFront::Distribution',
        Match.object_like({'DeletionPolicy': 'Retain', 'UpdateReplacePolicy': 'Retain'}),
    )

    edge.resource_count_is('AWS::Lambda::Function', 0)


def test_mcp_routes_state_and_edge_share_the_telemetry_lambda() -> None:
    """Keep MCP self-authenticated and migrate its retained edge without new compute."""
    telemetry, _, mcp_edge = stacks()

    telemetry.has_resource_properties(
        'AWS::Lambda::Function',
        {
            'Environment': {
                'Variables': Match.object_like(
                    {
                        'MCP_ENABLED': 'true',
                        'MCP_RESOURCE': 'https://mcp.example.com/mcp/',
                        'OAUTH_STATE_TABLE': 'markdown-mcp-service-oauth',
                    }
                )
            }
        },
    )

    for route_key in (
        'ANY /mcp',
        'ANY /mcp/{proxy+}',
        'ANY /.well-known/{proxy+}',
        'GET /.well-known/oauth-protected-resource',
        'ANY /authorize',
        'ANY /token',
        'ANY /register',
    ):
        telemetry.has_resource_properties(
            'AWS::ApiGatewayV2::Route',
            {'RouteKey': route_key, 'AuthorizationType': Match.absent()},
        )

    mcp_edge.resource_count_is('AWS::CloudFront::Distribution', 1)

    mcp_edge.resource_count_is('AWS::Lambda::Function', 0)


def test_identity_stack_keeps_cognito_and_data_without_deployment_iam() -> None:
    """Adopt active clients/tables while excluding old CI, ECS and Lambda roles."""
    app = cdk.App()
    stack = PortalIdentityStack(
        app,
        'Identity',
        env=cdk.Environment(account='123456789012', region='ca-central-1'),
    )
    identity = Template.from_stack(stack)

    identity.resource_count_is('AWS::Cognito::UserPoolClient', 2)

    identity.resource_count_is('AWS::DynamoDB::Table', 2)

    identity.resource_count_is('AWS::IAM::Role', 0)

    identity.resource_count_is('AWS::IAM::Policy', 0)

    identity.has_resource_properties(
        'AWS::Cognito::UserPoolClient',
        {
            'ClientName': 'work-organizer-portal',
            'CallbackURLs': [{'Fn::Sub': '${PortalOrigin}/work-organizer/auth/callback'}],
        },
    )
