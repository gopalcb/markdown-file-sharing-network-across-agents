"""Define the GitHub-deployed Softrobots telemetry CDK application."""

import os

import aws_cdk as cdk
from data_stack import DocumentDataStack
from edge_stack import PortalEdgeStack
from identity_stack import PortalIdentityStack
from mcp_edge_stack import McpEdgeStack
from telemetry_stack import TelemetryStack

ACCOUNT = '249766001220'
REGION = 'ca-central-1'


def build_app() -> cdk.App:
    """Create the telemetry and optional edge stacks for the requested phase."""
    app = cdk.App()
    phase = app.node.try_get_context('phase') or 'prepare'

    if phase not in {'prepare', 'cutover', 'identity', 'retire', 'mcp-prepare', 'mcp-cutover'}:
        raise ValueError(
            'CDK context phase must be prepare, cutover, identity, retire, '
            'mcp-prepare or mcp-cutover'
        )

    environment = cdk.Environment(
        account=os.environ.get('CDK_DEFAULT_ACCOUNT', ACCOUNT),
        region=os.environ.get('CDK_DEFAULT_REGION', REGION),
    )

    if phase == 'identity':
        PortalIdentityStack(
            app,
            'SoftrobotsPortalIdentity',
            env=environment,
            stack_name='softrobots-portal-auth',
        )

        return app

    telemetry = TelemetryStack(
        app,
        'SoftrobotsTelemetry',
        env=environment,
        stack_name='softrobots-telemetry',
    )

    # Always synthesize the existing edge consumer with telemetry so CDK keeps
    # the API export that the deployed edge stack imports.  Prepare still
    # deploys only SoftrobotsTelemetry; this does not update CloudFront.
    if phase in {'prepare', 'cutover', 'retire', 'mcp-prepare', 'mcp-cutover'}:
        edge = PortalEdgeStack(
            app,
            'SoftrobotsPortalEdge',
            telemetry=telemetry,
            env=environment,
            stack_name='softrobots-portal-edge',
        )

        edge.add_stack_dependency(telemetry)

    if phase == 'mcp-cutover':
        mcp_edge = McpEdgeStack(
            app,
            'MarkdownMcpEdge',
            telemetry=telemetry,
            env=environment,
            stack_name='markdown-mcp-service-edge',
        )

        mcp_edge.add_stack_dependency(telemetry)

    if phase == 'retire':
        DocumentDataStack(
            app,
            'SoftrobotsDocumentData',
            env=environment,
            stack_name='softrobots-document-api',
        )

    return app


build_app().synth()
