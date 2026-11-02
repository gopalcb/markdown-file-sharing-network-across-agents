"""Adopt and migrate the existing MCP distribution to telemetry-lambda."""

from pathlib import Path

from aws_cdk import Stack
from aws_cdk.cloudformation_include import CfnInclude
from constructs import Construct
from telemetry_stack import TelemetryStack


class McpEdgeStack(Stack):
    """Update only the retained MCP distribution origin in place."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        telemetry: TelemetryStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        template = Path(__file__).resolve().parent.parent / 'mcp-edge.yaml'

        CfnInclude(
            self,
            'McpEdgeTemplate',
            template_file=str(template),
            preserve_logical_ids=True,
            parameters={'OriginHostname': telemetry.api_host},
        )
