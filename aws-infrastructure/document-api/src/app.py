"""Lambda boundary for native ingestion and trusted portal document/task operations."""

import json
import os

import boto3
from botocore.config import Config
from services.deletion_service import DeletionConflict, record_deletion
from services.document_read_service import get_document, list_documents
from services.document_service import store_documents
from services.logging_service import log_event
from services.native_device_service import authorize_native_device
from services.portal_document_service import execute_portal
from services.portal_validation_service import PORTAL_OPERATIONS, validate_portal_event
from services.project_service import ProjectExists, create_project, list_projects
from services.task_service import DocumentConflict
from services.validation_service import (
    validate_event,
    validate_native_proof,
    validate_read_event,
    validate_upload_identity,
)
from session_device_service import check_device, session_record

AWS_CONFIG = Config(connect_timeout=2, read_timeout=3, retries={'total_max_attempts': 1})


def lambda_handler(event: dict, context) -> dict:
    """Validate ingestion or trusted reads, then return a sanitized proxy response."""
    request_id = context.aws_request_id
    status = 201
    is_read = isinstance(event, dict) and 'operation' in event
    is_portal = (
        is_read
        and isinstance(event.get('operation'), str)
        and event['operation'] in PORTAL_OPERATIONS
    )

    try:
        payload = (
            validate_portal_event(event)
            if is_portal
            else (validate_read_event(event) if is_read else validate_event(event))
        )

        if not is_read:
            validate_upload_identity(event, payload)
            headers = {key.lower(): value for key, value in event.get('headers', {}).items()}
            record = session_record(
                event['requestContext']['authorizer']['jwt']['claims'],
                headers.get('x-device-id', ''),
                headers.get('x-device-mac', ''),
                native=True,
            )
            proof = validate_native_proof(event)
    except PermissionError:
        status, body = 403, {'error': 'Upload identity not authorized', 'request_id': request_id}
    except ValueError as error:
        status, body = 400, {'error': str(error), 'request_id': request_id}
    else:
        try:
            session = boto3.Session()

            if not is_read:
                device_table = session.resource('dynamodb', config=AWS_CONFIG).Table(
                    'softrobots-auth-sessions'
                )
                register = payload.get('action') == 'register_device'

                approved = authorize_native_device(
                    session.resource('dynamodb', config=AWS_CONFIG), proof, register
                )

                if approved:
                    check_device(device_table, record, register=register)

                if register:
                    return {
                        'statusCode': 200 if approved else 202,
                        'headers': {
                            'content-type': 'application/json',
                            'cache-control': 'no-store',
                        },
                        'body': json.dumps(
                            {'registered': True}
                            if approved
                            else {
                                'approval_required': True,
                                'fingerprint': proof['fingerprint'],
                                'owner_id': proof['owner_id'],
                            }
                        ),
                    }
                payload['device'] = {
                    key: record[key] for key in ('device_id', 'device_mac', 'device_type')
                }

            if not is_read and payload.get('action') == 'report_deleted':
                deletion_table = session.resource('dynamodb', config=AWS_CONFIG).Table(
                    os.environ['DELETIONS_TABLE']
                )
                receipt = record_deletion(payload, proof, deletion_table)

                return {
                    'statusCode': 200,
                    'headers': {'content-type': 'application/json', 'cache-control': 'no-store'},
                    'body': json.dumps({'deletion': receipt, 'request_id': request_id}),
                }

            if is_portal and payload['operation'] in ('list_projects', 'create_project'):
                projects = session.resource('dynamodb', config=AWS_CONFIG).Table(
                    os.environ['PROJECTS_TABLE']
                )
                body, status = (
                    create_project(payload, projects)
                    if payload['operation'] == 'create_project'
                    else (list_projects(payload, projects), 200)
                )

                return {
                    'statusCode': status,
                    'headers': {'content-type': 'application/json', 'cache-control': 'no-store'},
                    'body': json.dumps(body),
                }

            s3 = session.client('s3', config=AWS_CONFIG)
            table = session.resource('dynamodb', config=AWS_CONFIG).Table(
                os.environ['USER_FILES_TABLE']
            )

            if is_portal:
                body, status = execute_portal(
                    payload,
                    table,
                    s3,
                    os.environ['DOCUMENTS_BUCKET'],
                    os.environ.get('LEGACY_DOCUMENTS_BUCKET'),
                    request_id,
                )
            elif is_read:
                body = (
                    list_documents(payload, table)
                    if payload['operation'] == 'list_documents'
                    else get_document(
                        payload,
                        table,
                        s3,
                        os.environ['DOCUMENTS_BUCKET'],
                        os.environ.get('LEGACY_DOCUMENTS_BUCKET'),
                    )
                )
                status = 200 if body is not None else 404
                body = {**(body or {'error': 'Document not found'}), 'request_id': request_id}
            else:
                results = store_documents(
                    payload, s3, table, os.environ['DOCUMENTS_BUCKET'], request_id
                )
                status = 201 if all(item['status'] == 'READY' for item in results) else 207
                body = {'request_id': request_id, 'files': results}
        except ProjectExists:
            status, body = 409, {'error': 'project_exists', 'request_id': request_id}
        except DocumentConflict:
            status, body = 409, {'error': 'revision_conflict', 'request_id': request_id}
        except DeletionConflict:
            status, body = 409, {'error': 'Deletion event conflict', 'request_id': request_id}
        except PermissionError:
            status, body = 403, {'error': 'Device not authorized', 'request_id': request_id}
        except Exception as error:
            # HTTP boundary: report failure without exposing AWS details or document data.
            log_event('request_failed', request_id, error_type=type(error).__name__)
            message = 'Unable to read documents'

            if not is_read or payload['operation'] == 'upload_documents':
                message = 'Upload outcome unknown; inspect request_id before retrying'
            elif payload['operation'] in ('replace_document', 'update_task'):
                message = 'Save outcome unknown; reload the current revision before retrying'

            if payload.get('action') == 'report_deleted':
                message = 'Deletion report not acknowledged; retry the same event ID'
            status, body = 500, {'error': message, 'request_id': request_id}

    return {
        'statusCode': status,
        'headers': {'content-type': 'application/json', 'cache-control': 'no-store'},
        'body': json.dumps(body),
    }
