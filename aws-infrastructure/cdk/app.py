"""Synthesize fictitious telemetry examples for local validation only."""

import os

import aws_cdk as cdk
from data_stack import DocumentDataStack
from edge_stack import PortalEdgeStack
from identity_stack import PortalIdentityStack
from mcp_edge_stack import McpEdgeStack
from telemetry_stack import TelemetryStack

ACCOUNT = '123456789012'
REGION = 'ca-central-1'


def build_app() -> cdk.App:
    """Create the telemetry and optional edge stacks for the requested phase."""
    app = cdk.App()
    phase = app.node.try_get_context('phase') or 'prepare'

    if phase not in {
        'prepare',
        'cutover',
        'identity',
        'retire',
        'mcp-prepare',
        'mcp-cutover',
    }:
        raise ValueError(
            'CDK context phase must be prepare, cutover, identity, retire, '
            'mcp-prepare or mcp-cutover'
        )

    environment = cdk.Environment(
        account=os.environ.get('CDK_DEFAULT_ACCOUNT', ACCOUNT),
        region=os.environ.get('CDK_DEFAULT_REGION', REGION),
    )
    if environment.account != ACCOUNT:
        raise ValueError('Only the fictitious validation account may be synthesized here')

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

    # Include the edge consumer so local synthesis checks its cross-stack API
    # export/import relationship. No phase here runs a deployment command.
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


if __name__ == '__main__':
    build_app().synth()
