"""Synthesize the retained-data example without a second document runtime."""

from pathlib import Path

from aws_cdk import Stack
from aws_cdk.cloudformation_include import CfnInclude
from constructs import Construct


class DocumentDataStack(Stack):
    """Adopt the document stack with only its retained data resources."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        """Include original logical IDs for retained buckets and tables."""

        super().__init__(scope, construct_id, **kwargs)
        template = Path(__file__).resolve().parent.parent / 'document-data.yaml'

        CfnInclude(
            self,
            'DocumentDataTemplate',
            template_file=str(template),
            preserve_logical_ids=True,
        )
