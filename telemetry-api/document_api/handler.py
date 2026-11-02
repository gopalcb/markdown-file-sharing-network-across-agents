"""Route native and portal document commands through the two storage services."""

import json
import logging
import os
from types import SimpleNamespace
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from services.authentication_service import check_device, session_record

from document_api.dynamodb_document_service import (
    DeletionConflict,
    DocumentConflict,
    ProjectExists,
    authorize_native_device,
    create_project,
    list_documents,
    list_projects,
    record_deletion,
)
from document_api.portal_contract import portal_response, validate_document_response
from document_api.portal_validation import PORTAL_OPERATIONS, validate_portal_event
from document_api.s3_document_service import (
    DocumentStorage,
    execute_portal,
    read_document,
    store_documents,
)
from document_api.validation import (
    validate_event,
    validate_native_proof,
    validate_read_event,
    validate_upload_identity,
)

AWS_CONFIG = Config(connect_timeout=2, read_timeout=3, retries={'total_max_attempts': 1})
LOGGER = logging.getLogger(__name__)


def proxy_response(body: dict, status: int) -> dict:
    """Build one private no-store Lambda proxy response."""

    return {
        'statusCode': status,
        'headers': {'content-type': 'application/json', 'cache-control': 'no-store'},
        'body': json.dumps(body),
    }


def execute_documents(payload: dict) -> tuple[dict, int]:
    """Execute one portal command locally and expose only its validated response."""

    try:
        proxy = lambda_handler(payload, SimpleNamespace(aws_request_id=str(uuid4())))

        if (
            not isinstance(proxy, dict)
            or type(proxy.get('statusCode')) is not int
            or not isinstance(proxy.get('body'), str)
            or proxy.get('isBase64Encoded')
            or len(json.dumps(proxy).encode()) > 2 * 1024 * 1024
        ):
            raise ValueError('Invalid document proxy response')
        status = proxy['statusCode']

        if status in (400, 403, 404, 409):
            errors = {
                400: 'invalid_document_request',
                403: 'forbidden',
                404: 'not_found',
                409: 'project_exists'
                if payload['operation'] == 'create_project'
                else 'revision_conflict',
            }

            return {'error': errors[status]}, status
        allowed = (
            (201, 207)
            if payload['operation'] == 'upload_documents'
            else ((200, 201) if payload['operation'] == 'create_project' else (200,))
        )

        if status not in allowed:
            raise ValueError('Document service failed')
        validator = (
            validate_document_response
            if payload['operation'] in ('list_documents', 'get_document')
            else portal_response
        )
        result = validator(json.loads(proxy['body']), payload)

        if (
            payload['operation'] == 'upload_documents'
            and status == 201
            and any(item['status'] != 'READY' for item in result['files'])
        ):
            raise ValueError('Inconsistent upload success receipt')

        return result, status
    except (BotoCoreError, ClientError, OSError, ValueError, TypeError, UnicodeError):
        return {'error': 'documents_unavailable'}, 502


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
                    body = (
                        {'registered': True}
                        if approved
                        else {
                            'approval_required': True,
                            'fingerprint': proof['fingerprint'],
                            'owner_id': proof['owner_id'],
                        }
                    )

                    return proxy_response(body, 200 if approved else 202)
                payload['device'] = {
                    key: record[key] for key in ('device_id', 'device_mac', 'device_type')
                }

            if not is_read and payload.get('action') == 'report_deleted':
                deletion_table = session.resource('dynamodb', config=AWS_CONFIG).Table(
                    os.environ['DELETIONS_TABLE']
                )
                receipt = record_deletion(payload, proof, deletion_table)

                return proxy_response({'deletion': receipt, 'request_id': request_id}, 200)

            if is_portal and payload['operation'] in ('list_projects', 'create_project'):
                projects = session.resource('dynamodb', config=AWS_CONFIG).Table(
                    os.environ['PROJECTS_TABLE']
                )
                body, status = (
                    create_project(payload, projects)
                    if payload['operation'] == 'create_project'
                    else (list_projects(payload, projects), 200)
                )

                return proxy_response(body, status)

            s3 = session.client('s3', config=AWS_CONFIG)
            table = session.resource('dynamodb', config=AWS_CONFIG).Table(
                os.environ['USER_FILES_TABLE']
            )
            storage = DocumentStorage(
                table,
                s3,
                os.environ['DOCUMENTS_BUCKET'],
                os.environ.get('LEGACY_DOCUMENTS_BUCKET'),
            )

            if is_portal:
                body, status = execute_portal(payload, storage, request_id)
            elif is_read:
                body = (
                    list_documents(payload, table)
                    if payload['operation'] == 'list_documents'
                    else read_document(payload, storage)
                )
                status = 200 if body is not None else 404
                body = {**(body or {'error': 'Document not found'}), 'request_id': request_id}
            else:
                results = store_documents(payload, storage, request_id)
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
            LOGGER.error(
                'Document request failed',
                extra={'request_id': request_id, 'error_type': type(error).__name__},
            )
            message = 'Unable to read documents'

            if not is_read or payload['operation'] == 'upload_documents':
                message = 'Upload outcome unknown; inspect request_id before retrying'
            elif payload['operation'] in ('replace_document', 'update_task'):
                message = 'Save outcome unknown; reload the current revision before retrying'

            if payload.get('action') == 'report_deleted':
                message = 'Deletion report not acknowledged; retry the same event ID'
            status, body = 500, {'error': message, 'request_id': request_id}

    return proxy_response(body, status)
