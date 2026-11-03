"""Adapt validated MCP input to the shared telemetry document service."""

import asyncio
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from document_api.s3_document_service import DocumentStorage, store_documents

from mcp_api.validation_service import UploadBatch, prepare_payload


def write_documents(payload: dict, config: dict) -> list:
    """Use task-role credentials and no automatic write retries; close transports."""
    aws_config = Config(connect_timeout=5, read_timeout=15, retries={'total_max_attempts': 1})
    s3 = boto3.client('s3', config=aws_config)
    dynamodb = None

    try:
        dynamodb = boto3.resource('dynamodb', config=aws_config)

        storage = DocumentStorage(
            dynamodb.Table(config['USER_FILES_TABLE']), s3, config['S3_BUCKET']
        )

        return store_documents(payload, storage, str(uuid4()))
    finally:
        s3.close()

        if dynamodb is not None:
            dynamodb.meta.client.close()


async def save_batch(payload: UploadBatch, config: dict) -> dict:
    """Report success only after every shared document receipt reaches READY."""
    prepared = prepare_payload(payload)

    try:
        results = await asyncio.to_thread(write_documents, prepared, config)
    except (BotoCoreError, ClientError):
        raise ValueError('Storage not confirmed; reconcile before retrying') from None

    return {
        'success': all(item['status'] == 'READY' for item in results),
        'files': results,
    }
