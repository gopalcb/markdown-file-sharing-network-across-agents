"""Authenticate native upload adapters and preserve the signed request bytes.

API Gateway validates each route's Cognito client. This boundary additionally checks
the JWT signature locally so a forged adapter-context header cannot grant access.
Both native routes execute the same embedded document storage coordinator.
"""

import base64
import json
import os
import re
from types import SimpleNamespace

import jwt
from document_api.handler import lambda_handler
from flask import Response, current_app, jsonify, request

from services.authentication_service import AuthenticationError, JwksCache


def native_event() -> tuple[dict, str]:
    """Reconstruct one gateway event after independently verifying its signed identity."""
    prefix = 'NATIVE_UPLOAD' if request.path == '/documents' else 'BROWSER_UPLOAD'
    issuer = os.environ.get(prefix + '_ISSUER', '')
    client_id = os.environ.get(prefix + '_CLIENT_ID', '')

    if not re.fullmatch(
        r'https://cognito-idp\.ca-central-1\.amazonaws\.com/ca-central-1_[A-Za-z0-9]+',
        issuer,
    ) or not re.fullmatch(r'[a-z0-9]{1,128}', client_id):
        raise RuntimeError('Native upload authentication is not configured')

    raw_context = request.headers.get('x-amzn-request-context', '')

    if not raw_context or len(raw_context) > 65536:
        raise AuthenticationError('Gateway context required')
    context = json.loads(raw_context)
    gateway_claims = context['authorizer']['jwt']['claims']

    if (
        not isinstance(context.get('requestId'), str)
        or not re.fullmatch(r'[A-Za-z0-9_=-]{1,128}', context['requestId'])
        or context['http']['method'] != request.method
        or context['http']['path'] != request.path
        or request.query_string
        or not isinstance(gateway_claims, dict)
    ):
        raise AuthenticationError('Invalid gateway context')

    authorization = request.headers.get('Authorization', '')

    if not re.fullmatch(r'Bearer [A-Za-z0-9._~-]{1,16384}', authorization):
        raise AuthenticationError('Bearer required')
    token = authorization[7:]
    header = jwt.get_unverified_header(token)

    if (
        header.get('alg') != 'RS256'
        or not isinstance(header.get('kid'), str)
        or not 1 <= len(header['kid']) <= 256
        or header.get('crit')
    ):
        raise AuthenticationError('Invalid signing header')
    caches = current_app.extensions.setdefault('native_jwks_caches', {})
    cache = caches.setdefault(issuer, JwksCache(issuer))
    claims = jwt.decode(
        token,
        cache.get_key(header['kid']).key,
        algorithms=['RS256'],
        issuer=issuer,
        leeway=10,
        options={
            'verify_aud': False,
            'require': [
                'iss',
                'sub',
                'exp',
                'iat',
                'client_id',
                'token_use',
                'username',
            ],
        },
    )

    if (
        claims['client_id'] != client_id
        or claims['token_use'] != 'access'
        or not isinstance(claims.get('scope'), str)
        or 'aws.cognito.signin.user.admin' not in claims['scope'].split()
    ):
        raise AuthenticationError('Wrong upload client')

    for field in ('iss', 'sub', 'client_id', 'token_use', 'username'):
        if gateway_claims.get(field) != claims[field]:
            raise AuthenticationError('Gateway identity mismatch')

    return {
        'version': '2.0',
        'rawPath': request.path,
        'rawQueryString': '',
        'headers': {key.lower(): value for key, value in request.headers.items()},
        'body': base64.b64encode(request.get_data(cache=True)).decode('ascii'),
        'isBase64Encoded': True,
        'requestContext': {**context, 'authorizer': {'jwt': {'claims': claims}}},
    }, context['requestId']


def native_upload() -> Response | tuple[Response, int]:
    """Run native ingestion once, preserving its device proof and exact response receipt."""

    try:
        event, request_id = native_event()
    except RuntimeError:
        return jsonify(error='upload_authentication_unavailable'), 503
    except (
        AuthenticationError,
        jwt.PyJWTError,
        KeyError,
        ValueError,
        TypeError,
        RecursionError,
    ):
        return jsonify(error='unauthorized'), 401
    result = lambda_handler(event, SimpleNamespace(aws_request_id=request_id))

    return Response(
        result['body'],
        status=result['statusCode'],
        headers=result.get('headers', {}),
    )
