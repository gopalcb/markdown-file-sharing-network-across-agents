"""Validate and read bounded portfolio event pages without exposing identifiers."""

import base64
import binascii
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config

TABLE_NAME = os.environ.get('ANALYTICS_TABLE', 'markdown-sharing-example-analytics')
PAGE_SIZE = 250
TEXT_FIELDS = (
    'event_id',
    'client_timestamp',
    'event_type',
    'target',
    'page_title',
    'page_path',
    'status',
)
METADATA_FIELDS = (
    'element_id',
    'video_title',
    'current_time_seconds',
    'language',
    'referrer_host',
    'browser_timezone',
)


def validate_analytics_query(arguments) -> dict:
    """Accept only a bounded continuation key for the fixed analytics table."""

    if any(name != 'cursor' for name in arguments) or len(arguments.getlist('cursor')) > 1:
        raise ValueError('Unsupported analytics query')
    cursor = arguments.get('cursor', '')

    if not cursor:
        return {}

    if len(cursor) > 2048:
        raise ValueError('Invalid analytics cursor')

    try:
        value = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))

        if not isinstance(value, dict) or set(value) != {'key', 'as_of'}:
            raise ValueError('Invalid analytics cursor')
        key = value['key']

        if not isinstance(key, dict) or set(key) != {'PK', 'SK'}:
            raise ValueError('Invalid analytics key')

        for name, prefix in (('PK', 'SESSION#'), ('SK', 'EVENT#')):
            if not isinstance(key[name], str) or not re.fullmatch(
                prefix + r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',
                key[name],
            ):
                raise ValueError('Invalid analytics key')

        timestamp = datetime.fromisoformat(value['as_of'])

        if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
            raise ValueError('Analytics snapshot must be UTC')

        if timestamp > datetime.now(timezone.utc):
            raise ValueError('Invalid future analytics snapshot')

        return {'key': key, 'as_of': timestamp.isoformat()}
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError):
        raise ValueError('Invalid analytics cursor') from None


def normalize_event(item: dict) -> dict:
    """Return allowlisted display fields and a stable non-identifying visitor grouping key."""
    event = {field: str(item.get(field, ''))[:512] for field in TEXT_FIELDS}
    session = str(item.get('session', ''))
    event['visitor_key'] = hashlib.sha256(session.encode()).hexdigest()
    event['visited_at'] = str(item.get('received_at', ''))[:64]
    event['project'] = str(item.get('get_project', ''))[:200]
    event['location'] = {
        field: str(item.get(field, ''))[:200] for field in ('city', 'region', 'country')
    }
    event['location']['timezone'] = str(item.get('geo_timezone', ''))[:200]
    metadata = item.get('metadata', {})
    event['metadata'] = {}

    for field in METADATA_FIELDS:
        value = metadata.get(field)

        if field == 'current_time_seconds':
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                number = float(value)

                if 0 <= number <= 86400:
                    event['metadata'][field] = number
        elif isinstance(value, str):
            event['metadata'][field] = value[:200]

    return event


def read_portfolio_events(query: dict, table=None) -> dict:
    """Scan one fixed-table page; preserve empty-page cursors and an upper time boundary."""

    if table is None:
        table = boto3.resource(
            'dynamodb',
            region_name='ca-central-1',
            config=Config(connect_timeout=2, read_timeout=10, retries={'total_max_attempts': 1}),
        ).Table(TABLE_NAME)

    now = datetime.now(timezone.utc)
    as_of = query.get('as_of') or now.isoformat()
    fields = (
        'session',
        'event_id',
        'received_at',
        'client_timestamp',
        'event_type',
        'target',
        'get_project',
        'page_title',
        'page_path',
        'status',
        'city',
        'region',
        'country',
        'geo_timezone',
        'metadata',
    )
    parameters = {
        'Limit': PAGE_SIZE,
        'ConsistentRead': True,
        'FilterExpression': Attr('expires_at').gt(int(now.timestamp()))
        & Attr('received_at').lte(as_of),
        'ProjectionExpression': ', '.join('#' + field for field in fields),
        'ExpressionAttributeNames': {'#' + field: field for field in fields},
    }

    if query.get('key'):
        parameters['ExclusiveStartKey'] = query['key']

    result = table.scan(**parameters)
    next_cursor = None

    if result.get('LastEvaluatedKey'):
        cursor = {'key': result['LastEvaluatedKey'], 'as_of': as_of}
        next_cursor = base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode()

    return {
        'events': [normalize_event(item) for item in result.get('Items', [])],
        'next_cursor': next_cursor,
        'as_of': as_of,
    }
