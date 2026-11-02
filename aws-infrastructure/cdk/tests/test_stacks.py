"""Assert the one-Lambda telemetry topology without AWS access."""

import os
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

CDK_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(CDK_ROOT))

from edge_stack import PortalEdgeStack  # noqa: E402
from identity_stack import PortalIdentityStack  # noqa: E402
from mcp_edge_stack import McpEdgeStack  # noqa: E402
from telemetry_stack import TelemetryStack  # noqa: E402


def stacks() -> tuple[Template, Template, Template]:
    """Synthesize replacement and edge templates in the approved environment."""
    os.environ['CDK_DEFAULT_ACCOUNT'] = '249766001220'
    os.environ['CDK_DEFAULT_REGION'] = 'ca-central-1'
    app = cdk.App()
    telemetry_stack = TelemetryStack(
        app,
        'Telemetry',
        env=cdk.Environment(account='249766001220', region='ca-central-1'),
    )
    edge_stack = PortalEdgeStack(
        app,
        'Edge',
        telemetry=telemetry_stack,
        env=cdk.Environment(account='249766001220', region='ca-central-1'),
    )
    mcp_edge_stack = McpEdgeStack(
        app,
        'McpEdge',
        telemetry=telemetry_stack,
        env=cdk.Environment(account='249766001220', region='ca-central-1'),
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


def test_analytics_delete_permission_is_limited_to_the_portfolio_table() -> None:
    """Grant transactional deletion only on the fixed analytics table."""
    telemetry, _, _ = stacks()

    telemetry.has_resource_properties(
        'AWS::IAM::Policy',
        {
            'PolicyDocument': {
                'Statement': Match.array_with(
                    [
                        {
                            'Action': 'dynamodb:TransactWriteItems',
                            'Effect': 'Allow',
                            'Resource': {
                                'Fn::Join': [
                                    '',
                                    [
                                        'arn:',
                                        {'Ref': 'AWS::Partition'},
                                        ':dynamodb:ca-central-1:249766001220:'
                                        'table/gb-portfolio-visitors-table',
                                    ],
                                ]
                            },
                        }
                    ]
                )
            }
        },
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
                        'MCP_RESOURCE': 'https://d3cjbof2ij7jy9.cloudfront.net/mcp/',
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
        env=cdk.Environment(account='249766001220', region='ca-central-1'),
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
