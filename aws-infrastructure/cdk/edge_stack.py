"""Synthesize an example portal edge from the retained template structure."""

from pathlib import Path

from aws_cdk import Stack
from aws_cdk.cloudformation_include import CfnInclude
from constructs import Construct
from telemetry_stack import COGNITO_DOMAIN, PORTAL_ISSUER, TelemetryStack


class PortalEdgeStack(Stack):
    """Describe the example distribution and its telemetry origin relationships."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        telemetry: TelemetryStack,
        **kwargs,
    ) -> None:
        """Include the reviewed edge template with replacement-origin parameters."""

        super().__init__(scope, construct_id, **kwargs)
        template = Path(__file__).resolve().parent.parent / 'portal-edge.yaml'

        CfnInclude(
            self,
            'PortalEdgeTemplate',
            template_file=str(template),
            preserve_logical_ids=True,
            parameters={
                'DocumentApiOriginHost': telemetry.api_host,
                'UiBucketDomain': telemetry.ui_bucket.bucket_regional_domain_name,
                'ApiOriginHost': telemetry.api_host,
                'AuthConnectOrigins': f'{PORTAL_ISSUER.rsplit("/", 1)[0]} {COGNITO_DOMAIN}',
            },
        )
