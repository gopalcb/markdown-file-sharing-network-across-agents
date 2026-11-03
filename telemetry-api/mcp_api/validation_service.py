"""Validate MCP deployment settings and complete Markdown batches."""

import os
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Metadata(BaseModel):
    """Bounded editorial metadata; relative paths never alter storage prefixes."""

    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    project: str = Field(default='chatgpt-web-chat', max_length=100)
    relative_path: str = Field(default='', max_length=200)

    @field_validator('relative_path')
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Accept only safe slash-separated segments."""

        if value and any(not re.fullmatch(r'[A-Za-z0-9_-]+', part) for part in value.split('/')):
            raise ValueError('Invalid relative path')

        return value


class MarkdownFile(BaseModel):
    """One safe basename and UTF-8 Markdown document."""

    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    name: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.md$')
    content: str = Field(max_length=262144)
    metadata: Metadata = Field(default_factory=Metadata)


class UploadBatch(BaseModel):
    """Personal upload input; authorization independently binds the subject."""

    model_config = ConfigDict(extra='forbid', populate_by_name=True, hide_input_in_errors=True)
    username: str = Field(default='gopalcbala', pattern=r'^gopalcbala$')
    file_group: str = Field(
        default='learning-chatgpt',
        alias='file-group',
        pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$',
    )
    files: list[MarkdownFile] = Field(min_length=1, max_length=10)


def prepare_payload(batch: UploadBatch) -> dict:
    """Validate every byte and destination before shared storage receives input."""
    names = set()
    size = 0

    for document in batch.files:
        if document.name in names or '..' in document.name:
            raise ValueError('Duplicate or unsafe destination')

        names.add(document.name)
        body = document.content.encode('utf-8')

        if len(body) > 256 * 1024:
            raise ValueError('File exceeds 256 KiB')
        size += len(body)

    if size > 2_000_000:
        raise ValueError('Batch exceeds 2 MB')

    return batch.model_dump(by_alias=True)


def load_config() -> dict:
    """Fail startup for incomplete enabled configuration; permit deny-all bootstrap."""
    enabled = os.environ.get('MCP_ENABLED', 'true').lower() == 'true'
    config = {'enabled': enabled}

    if not enabled:
        return config

    environment = {
        'PUBLIC_BASE_URL': 'PUBLIC_BASE_URL',
        'COGNITO_ISSUER': 'MCP_COGNITO_ISSUER',
        'COGNITO_CLIENT_ID': 'MCP_COGNITO_CLIENT_ID',
        'MCP_RESOURCE': 'MCP_RESOURCE',
        'MCP_WRITE_SCOPE': 'MCP_WRITE_SCOPE',
        'ALLOWED_SUB': 'MCP_ALLOWED_SUB',
        'S3_BUCKET': 'S3_BUCKET',
        'USER_FILES_TABLE': 'USER_FILES_TABLE',
        'COGNITO_DOMAIN': 'MCP_COGNITO_DOMAIN',
        'OAUTH_STATE_TABLE': 'OAUTH_STATE_TABLE',
        'OAUTH_SECRET_ARN': 'OAUTH_SECRET_ARN',
    }

    for name, environment_name in environment.items():
        value = os.environ.get(environment_name, '')

        if not value or value.strip() != value or any(ord(char) < 32 for char in value):
            raise ValueError(f'Missing or invalid {name}')
        config[name] = value

    base = config['PUBLIC_BASE_URL']
    parsed = urlsplit(base)

    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError('PUBLIC_BASE_URL must be an HTTPS origin without a trailing slash')

    if config['MCP_RESOURCE'] != base + '/mcp/':
        raise ValueError('MCP_RESOURCE must exactly match PUBLIC_BASE_URL/mcp/')

    if not re.fullmatch(
        r'https://cognito-idp\.[a-z0-9-]+\.amazonaws\.com/[A-Za-z0-9_-]+',
        config['COGNITO_ISSUER'],
    ):
        raise ValueError('Invalid Cognito issuer')

    # Configuration is trusted operator input. Accept a Cognito managed-login
    # hostname in the issuer's region, rather than binding this app to a source pool.
    issuer_region = urlsplit(config['COGNITO_ISSUER']).hostname.split('.')[1]
    if not re.fullmatch(
        r'https://[a-z0-9-]+\.auth\.' + re.escape(issuer_region) + r'\.amazoncognito\.com',
        config['COGNITO_DOMAIN'],
    ):
        raise ValueError('Invalid Cognito managed login domain')

    origin_host = os.environ.get('MCP_ORIGIN_HOST', '')

    if origin_host:
        if not re.fullmatch(r'[a-z0-9]+\.execute-api\.ca-central-1\.amazonaws\.com', origin_host):
            raise ValueError('MCP_ORIGIN_HOST must be the exact telemetry HTTP API hostname')
        config['MCP_ORIGIN_HOST'] = origin_host

    return config


def valid_oauth_redirect(value: str) -> bool:
    """Allow only literal ChatGPT callback paths or local native callbacks."""

    try:
        url = urlsplit(value)

        if url.username or url.password or url.query or url.fragment:
            return False

        if url.scheme == 'https' and url.hostname == 'chatgpt.com' and url.port in (None, 443):
            return url.path == '/connector_platform_oauth_redirect' or bool(
                re.fullmatch(r'/connector/oauth/[A-Za-z0-9_-]+', url.path)
            )

        return bool(
            url.scheme == 'http'
            and url.hostname in {'localhost', '127.0.0.1'}
            and url.port
            and url.path == '/callback'
        )
    except ValueError:
        return False
