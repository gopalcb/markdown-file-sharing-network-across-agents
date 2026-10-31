"""Coordinate independent latest-file uploads and truthful per-file batch outcomes."""

from botocore.exceptions import BotoCoreError, ClientError

from services.logging_service import log_event
from services.path_document_service import document_identity, store_path_document


def store_documents(payload: dict, s3, table, bucket: str, request_id: str) -> list:
    """Continue after individual failures; READY requires object and index acknowledgement."""
    results = []

    for source in payload['files']:
        document = source

        if payload.get('storage_layout') != 'project-path-v1':
            group = source.get(
                'file-group', payload.get('file-group', source.get('category', 'workflow-docs'))
            )
            document = {**source, 'relative_path': f'docs/{group}/{source["name"]}'}
        file_id, _ = document_identity(payload['username'], document['relative_path'])

        try:
            results.append(store_path_document(payload, document, s3, table, bucket))
        except (BotoCoreError, ClientError, ValueError) as error:
            log_event('upload_incomplete', request_id, file_id, type(error).__name__)

            results.append(
                {
                    'name': document['name'],
                    'file_id': file_id,
                    'status': 'INCOMPLETE',
                    'error': 'Storage not confirmed; inspect this file_id before retrying',
                }
            )

    return results
