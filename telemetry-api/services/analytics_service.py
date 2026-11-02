"""Validate, read, and delete portfolio events without exposing storage identifiers."""

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config

TABLE_NAME = 'gb-portfolio-visitors-table'
PAGE_SIZE = 250
MAX_DELETE_SCAN_PAGES = 40
MAX_ANALYTICS_DELETE_EVENTS = 100
EVENT_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[45][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z')
VISITOR_KEY = re.compile(r'[0-9a-f]{64}\Z')
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


class AnalyticsResolutionIncomplete(RuntimeError):
    """The service could not resolve every requested identity before its scan bound."""


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
                prefix + r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', key[name]
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


def validate_analytics_delete(payload: dict, arguments) -> list:
    """Accept a bounded list of public event identities and no storage keys."""

    if arguments or not isinstance(payload, dict) or set(payload) != {'events'}:
        raise ValueError('Invalid analytics delete request')
    events = payload['events']

    if not isinstance(events, list) or not 1 <= len(events) <= MAX_ANALYTICS_DELETE_EVENTS:
        raise ValueError('Invalid analytics delete events')

    normalized = []
    seen = set()

    for event in events:
        if not isinstance(event, dict) or set(event) != {'visitor_key', 'event_id'}:
            raise ValueError('Invalid analytics event identity')
        visitor_key = event['visitor_key']
        event_id = event['event_id']

        if (
            not isinstance(visitor_key, str)
            or not VISITOR_KEY.fullmatch(visitor_key)
            or not isinstance(event_id, str)
            or not EVENT_ID.fullmatch(event_id)
        ):
            raise ValueError('Invalid analytics event identity')
        identity = (visitor_key, event_id)

        if identity in seen:
            raise ValueError('Duplicate analytics event identity')

        seen.add(identity)

        normalized.append({'visitor_key': visitor_key, 'event_id': event_id})

    return normalized


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


def portfolio_event_identity(item: dict) -> tuple:
    """Return the public hashed-session and event identity for a stored event."""

    session = str(item.get('session', ''))
    event_id = str(item.get('event_id', ''))

    return hashlib.sha256(session.encode()).hexdigest(), event_id


def resolve_portfolio_event_keys(events: list, table) -> tuple:
    """Resolve selected public identities to exact keys without returning raw keys."""

    requested = {(event['visitor_key'], event['event_id']) for event in events}
    resolved = {}
    exclusive_start_key = None

    for _ in range(MAX_DELETE_SCAN_PAGES):
        parameters = {
            'Limit': PAGE_SIZE,
            'ConsistentRead': True,
            'ProjectionExpression': '#pk, #sk, #session, #event_id',
            'ExpressionAttributeNames': {
                '#pk': 'PK',
                '#sk': 'SK',
                '#session': 'session',
                '#event_id': 'event_id',
            },
        }

        if exclusive_start_key:
            parameters['ExclusiveStartKey'] = exclusive_start_key
        result = table.scan(**parameters)

        for item in result.get('Items', []):
            identity = portfolio_event_identity(item)

            if identity not in requested:
                continue
            expected_key = {
                'PK': 'SESSION#' + str(item.get('session', '')),
                'SK': 'EVENT#' + str(item.get('event_id', '')),
            }
            actual_key = {'PK': item.get('PK'), 'SK': item.get('SK')}

            if actual_key != expected_key:
                raise AnalyticsResolutionIncomplete('Analytics storage key mismatch')
            previous = resolved.get(identity)

            if previous is not None and previous != actual_key:
                raise AnalyticsResolutionIncomplete('Analytics event identity is not unique')
            resolved[identity] = actual_key

        if len(resolved) == len(requested):
            break
        exclusive_start_key = result.get('LastEvaluatedKey')

        if not exclusive_start_key:
            break
    else:
        if exclusive_start_key:
            raise AnalyticsResolutionIncomplete('Analytics identity resolution exceeded its bound')

    return resolved, requested - set(resolved)


def delete_portfolio_events(events: list, table=None) -> dict:
    """Atomically remove resolved selected events and confirm already absent identities."""

    if table is None:
        table = boto3.resource(
            'dynamodb',
            region_name='ca-central-1',
            config=Config(connect_timeout=2, read_timeout=10, retries={'total_max_attempts': 1}),
        ).Table(TABLE_NAME)
    resolved, missing = resolve_portfolio_event_keys(events, table)

    if resolved:
        table.meta.client.transact_write_items(
            TransactItems=[
                {
                    'Delete': {
                        'TableName': table.name,
                        'Key': {'PK': {'S': key['PK']}, 'SK': {'S': key['SK']}},
                    }
                }
                for key in resolved.values()
            ]
        )

    return {
        'removed': events,
        'deleted_count': len(resolved),
        'already_absent_count': len(missing),
    }
