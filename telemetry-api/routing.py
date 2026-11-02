"""Declare portal HTTP routes and enforce authentication before dispatch."""

from time import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from document_api.handler import execute_documents
from document_api.portal_contract import portal_request, validate_document_request
from flask import Response, current_app, g, jsonify, request
from services.analytics_service import (
    AnalyticsResolutionIncomplete,
    delete_portfolio_events,
    read_portfolio_events,
    validate_analytics_delete,
    validate_analytics_query,
)
from services.authentication_service import (
    COOKIE_NAME,
    TABLE_NAME,
    AuthenticationError,
    AuthorizationError,
    BrowserSessionService,
    authenticate_browser_session,
    browser_session_response,
    check_device,
    create_browser_session,
    read_browser_session,
    revoke_browser_session,
    session_record,
    validate_cookie_mutation,
    verify_access_token,
)
from werkzeug.exceptions import NotFound

AWS_CONFIG = Config(connect_timeout=2, read_timeout=3, retries={'total_max_attempts': 1})


def protect_api() -> tuple[Response, int] | None:
    """Allow only exact health paths publicly; reject unverifiable API requests safely."""

    if request.path in ('/health', '/api/health'):
        return None

    if request.path != '/api' and not request.path.startswith('/api/'):
        return None

    try:
        bearer = request.headers.get('Authorization', '')
        device_id = request.headers.get('X-Device-Id', '')
        bootstrap = request.path == '/api/session' and request.method == 'POST'

        if request.path == '/api/session/logout' and request.method in ('GET', 'POST'):
            service = browser_dependencies()
            g.browser_session = read_browser_session(
                service, request.cookies.get(COOKIE_NAME, ''), device_id
            )

            if request.method == 'POST':
                validate_cookie_mutation(
                    request.headers.get('Origin', ''),
                    current_app.config['BROWSER_ORIGIN'],
                    request.headers.get('X-CSRF-Token', ''),
                    g.browser_session,
                )

            return None

        if bootstrap and (
            request.headers.get('Origin') != current_app.config['BROWSER_ORIGIN']
            or not request.is_json
            or not bearer
        ):
            raise PermissionError('Invalid session bootstrap origin')

        if not bearer and request.cookies.get(COOKIE_NAME):
            service = browser_dependencies()
            g.identity, g.browser_session = authenticate_browser_session(
                service,
                request.cookies[COOKIE_NAME],
                device_id,
            )

            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                validate_cookie_mutation(
                    request.headers.get('Origin', ''),
                    current_app.config['BROWSER_ORIGIN'],
                    request.headers.get('X-CSRF-Token', ''),
                    g.browser_session,
                )
        else:
            g.identity = verify_access_token(
                bearer, current_app.config['AUTH_CONFIG'], current_app.extensions['jwks_cache']
            )

        g.claims = g.identity.pop('_claims')
        record = session_record(g.claims, device_id)
        table = current_app.extensions['device_table']

        if table is None:
            table = boto3.resource(
                'dynamodb',
                config=AWS_CONFIG,
            ).Table('softrobots-auth-sessions')
            current_app.extensions['device_table'] = table

        check_device(
            table,
            record,
            register=bootstrap or (request.path == '/api/me' and request.method == 'GET'),
        )
    except PermissionError:
        return jsonify(error='device_not_authorized'), 403
    except ClientError as error:
        if error.response['Error']['Code'] in {'NotAuthorizedException', 'UserNotFoundException'}:
            return jsonify(error='unauthorized'), 401

        return jsonify(error='authentication_unavailable'), 503
    except BotoCoreError:
        return jsonify(error='authentication_unavailable'), 503
    except AuthenticationError:
        response = jsonify(error='unauthorized')
        response.headers['WWW-Authenticate'] = 'Bearer'

        return response, 401
    except AuthorizationError:
        return jsonify(error='forbidden'), 403

    return None


def private_api_response(response: Response) -> Response:
    """Prevent browsers and shared caches from retaining protected responses or errors."""

    if request.path == '/api' or request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'private, no-store'

        if response.status_code == 401 and request.cookies.get(COOKIE_NAME):
            response.delete_cookie(COOKIE_NAME, secure=True, httponly=True, samesite='Lax')

    return response


def health() -> Response:
    """Report API readiness without credentials or infrastructure details."""

    return jsonify(status='ok', service='telemetry-api')


def current_user() -> Response:
    """Return only the verified durable identity and signed application groups."""

    return jsonify(g.identity)


def portfolio_analytics() -> Response | tuple[Response, int]:
    """Read or delete portfolio activity after standard identity and device checks."""

    if request.method == 'DELETE':
        try:
            events = validate_analytics_delete(request.get_json(silent=True), request.args)
        except ValueError:
            return jsonify(error='invalid_analytics_delete'), 400

        try:
            return jsonify(delete_portfolio_events(events))
        except AnalyticsResolutionIncomplete:
            current_app.logger.warning('Portfolio analytics deletion resolution incomplete')

            return jsonify(error='analytics_delete_resolution_incomplete'), 409
        except (BotoCoreError, ClientError):
            current_app.logger.warning('Portfolio analytics deletion outcome unconfirmed')

            return jsonify(error='analytics_delete_unconfirmed'), 503

    try:
        query = validate_analytics_query(request.args)
    except ValueError:
        return jsonify(error='invalid_analytics_query'), 400

    try:
        return jsonify(read_portfolio_events(query))
    except (BotoCoreError, ClientError):
        current_app.logger.warning('Portfolio analytics storage unavailable')

        return jsonify(error='analytics_unavailable'), 503


def not_found(error: NotFound) -> tuple[Response, int]:
    """Keep unknown API routes machine-readable."""

    return jsonify(error='not_found'), 404


def read_documents(owner: str | None = None, file_id: str | None = None) -> tuple[Response, int]:
    """Read a library page or Markdown document under the authenticated scope."""

    try:
        payload = validate_document_request(request.args, g.identity['sub'], owner, file_id)
    except ValueError:
        return jsonify(error='invalid_document_request'), 400

    if payload['scope'] == 'mine' and owner is not None and owner != g.identity['sub']:
        return jsonify(error='forbidden'), 403
    body, status = execute_documents(payload)

    return jsonify(body), status


def browser_dependencies() -> BrowserSessionService:
    """Create scoped AWS transports lazily, allowing offline tests to inject fixtures."""

    if current_app.extensions['browser_table'] is None:
        current_app.extensions['browser_table'] = boto3.resource(
            'dynamodb', config=AWS_CONFIG
        ).Table(TABLE_NAME)

    if current_app.extensions['browser_cognito'] is None:
        region = current_app.config['AUTH_CONFIG']['issuer'].split('.')[1]
        current_app.extensions['browser_cognito'] = boto3.client(
            'cognito-idp', region_name=region, config=AWS_CONFIG
        )

    return BrowserSessionService(
        current_app.extensions['browser_table'],
        current_app.extensions['browser_cognito'],
        current_app.config['AUTH_CONFIG'],
        current_app.extensions['jwks_cache'],
    )


def browser_session() -> Response | tuple:
    """Establish a persistent cookie or restore its nonsecret identity on reload."""
    identity = {**g.identity, '_claims': g.claims}

    if request.method == 'GET':
        if not hasattr(g, 'browser_session'):
            return jsonify(error='browser_session_required'), 401

        return jsonify(browser_session_response(identity, g.browser_session))

    try:
        service = browser_dependencies()
        cookie, record, fresh = create_browser_session(
            service,
            identity,
            request.get_json(),
            request.headers['X-Device-Id'],
        )

        if request.cookies.get(COOKIE_NAME):
            try:
                revoke_browser_session(
                    service,
                    request.cookies[COOKIE_NAME],
                    request.headers['X-Device-Id'],
                )
            except AuthenticationError:
                pass
    except (AuthenticationError, AuthorizationError):
        return jsonify(error='unauthorized'), 401
    except (ValueError, KeyError, TypeError):
        return jsonify(error='invalid_session_request'), 400
    except (BotoCoreError, ClientError):
        return jsonify(error='authentication_unavailable'), 503

    response = jsonify(browser_session_response(fresh, record))

    response.set_cookie(
        COOKIE_NAME,
        cookie,
        max_age=max(0, int(record['expires_at'] - time())),
        secure=True,
        httponly=True,
        samesite='Lax',
        path='/',
    )

    return response


def browser_logout() -> Response | tuple:
    """Invalidate the current cookie session before directing the user out of Cognito."""

    if not hasattr(g, 'browser_session'):
        return jsonify(error='browser_session_required'), 401

    if request.method == 'GET':
        # Same-origin JSON reveals only logout CSRF, even after group access is removed.
        return jsonify(csrfToken=g.browser_session['csrf'])

    try:
        service = browser_dependencies()

        revoke_browser_session(
            service,
            request.cookies[COOKIE_NAME],
            request.headers['X-Device-Id'],
        )
    except (BotoCoreError, ClientError):
        return jsonify(error='logout_unavailable'), 503

    response = Response(status=204)

    response.delete_cookie(COOKIE_NAME, secure=True, httponly=True, samesite='Lax', path='/')

    return response


def portal_documents(owner: str | None = None, file_id: str | None = None) -> tuple[Response, int]:
    """Bridge protected browser writes and task/folder queries to the scoped Lambda."""

    if request.path == '/api/projects':
        operation = 'create_project' if request.method == 'POST' else 'list_projects'
    elif request.path == '/api/document-folders':
        operation = 'list_folders'
    elif request.path.startswith('/api/tasks'):
        operation = (
            'update_task' if request.method == 'PATCH' else ('get_task' if owner else 'list_tasks')
        )
    else:
        operation = 'replace_document' if owner else 'upload_documents'

    try:
        body = request.get_json(silent=True) if request.method in ('POST', 'PUT', 'PATCH') else None
        payload = portal_request(operation, g.identity['sub'], request.args, body, owner, file_id)
    except PermissionError:
        return jsonify(error='forbidden'), 403
    except (ValueError, TypeError, UnicodeError):
        return jsonify(error='invalid_document_request'), 400
    body, status = execute_documents(payload)

    return jsonify(body), status
