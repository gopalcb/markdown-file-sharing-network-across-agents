"""Synthesize example family-pool clients and session tables."""

from pathlib import Path

from aws_cdk import Stack
from aws_cdk.cloudformation_include import CfnInclude
from constructs import Construct


class PortalIdentityStack(Stack):
    """Keep Cognito client/session ownership in the local CDK application."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        """Include the simplified existing-stack template without replacing identities."""

        super().__init__(scope, construct_id, **kwargs)
        template = Path(__file__).resolve().parent.parent / 'auth.yaml'

        CfnInclude(
            self,
            'PortalIdentityTemplate',
            template_file=str(template),
            preserve_logical_ids=True,
        )
