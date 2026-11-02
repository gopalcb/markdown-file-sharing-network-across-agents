"""Build the portal API with validated settings and explicitly registered routes."""

import os
from urllib.parse import urlsplit

from flask import Flask
from routing import (
    browser_logout,
    browser_session,
    current_user,
    health,
    not_found,
    portal_documents,
    portfolio_analytics,
    private_api_response,
    protect_api,
    read_documents,
)
from services.authentication_service import JwksCache, validate_auth_config
from services.native_upload_service import native_upload


def create_app(
    auth_values: dict | None = None,
    keys: JwksCache | None = None,
    device_table=None,
    browser_table=None,
    browser_cognito=None,
) -> Flask:
    """Fail startup without valid auth settings and protect current and future API routes."""
    config = validate_auth_config(dict(os.environ) if auth_values is None else auth_values)
    app = Flask(__name__)
    app.config['AUTH_CONFIG'] = config
    origin = (dict(os.environ) if auth_values is None else auth_values).get(
        'PORTAL_ORIGIN', 'https://d2f8oftnpxmjan.cloudfront.net'
    )
    target = urlsplit(origin)

    if (
        target.netloc == ''
        or target.path
        or target.query
        or target.fragment
        or target.username
        or target.password
        or not (
            target.scheme == 'https' or (target.scheme == 'http' and target.hostname == 'localhost')
        )
    ):
        raise ValueError('PORTAL_ORIGIN must be an exact HTTPS origin or explicit localhost')
    app.config['BROWSER_ORIGIN'] = origin
    app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
    app.extensions['browser_table'] = browser_table
    app.extensions['browser_cognito'] = browser_cognito
    app.extensions['jwks_cache'] = keys if keys is not None else JwksCache(config['issuer'])
    app.extensions['device_table'] = device_table

    app.before_request(protect_api)

    app.after_request(private_api_response)
    routes = (
        ('/documents', native_upload, ['POST'], None),
        ('/browser-documents', native_upload, ['POST'], None),
        ('/health', health, None, None),
        ('/api/health', health, None, None),
        ('/api/me', current_user, None, None),
        ('/api/session', browser_session, ['GET', 'POST'], None),
        ('/api/session/logout', browser_logout, ['GET', 'POST'], None),
        ('/api/documents', read_documents, None, None),
        ('/api/analytics/gh-portfolio', portfolio_analytics, ['GET', 'DELETE'], None),
        ('/api/documents/<owner>/<file_id>', read_documents, None, None),
        ('/api/documents', portal_documents, ['POST'], 'upload_documents'),
        (
            '/api/documents/<owner>/<file_id>',
            portal_documents,
            ['PUT'],
            'replace_document',
        ),
        ('/api/document-folders', portal_documents, None, None),
        ('/api/projects', portal_documents, ['GET', 'POST'], 'projects'),
        ('/api/tasks', portal_documents, None, 'list_tasks'),
        ('/api/tasks/<owner>/<file_id>', portal_documents, ['GET', 'PATCH'], 'task_detail'),
    )

    for rule, view, methods, endpoint in routes:
        app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)

    app.register_error_handler(404, not_found)

    return app
