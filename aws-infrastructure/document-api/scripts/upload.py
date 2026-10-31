"""Upload Markdown with Cognito credentials entered privately by the human operator."""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# The minimal native client is bundled locally; no watcher or saved state is shipped.
sys.path.insert(0, str(Path(__file__).resolve().parent / 'client'))
import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.httpsession import get_cert_path
from cognito_auth import LoginRequired, device_headers, login
from device_signing import signed_headers
from native_session import logout_saved, resume_session


class NoRedirect(HTTPRedirectHandler):
    """Keep bearer credentials and document bodies on the configured endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Reject every redirect, including redirects to the same host."""

        fp.close()
        raise ValueError('Upload redirects are disabled')


def send_upload(upload_url: str, payload: dict, access_token: str) -> dict:
    """Send an access token over HTTPS without redirects or automatic write retries."""
    parsed = urlsplit(upload_url)

    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.path not in ('/documents', '/browser-documents')
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError('Use the approved HTTPS document upload route')

    if not access_token or any(character.isspace() for character in access_token):
        raise ValueError('A Cognito access token is required')
    body = json.dumps(payload).encode('utf-8')
    outgoing = Request(
        upload_url,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {access_token}',
            **signed_headers(upload_url, body, access_token, device_headers()),
        },
        method='POST',
    )

    with build_opener(NoRedirect).open(outgoing, timeout=40) as response:
        return json.load(response)


def main() -> None:
    """Collect credentials only in the operator process; report sanitized failures."""
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument('--upload-url', default='https://example.invalid/documents')

    parser.add_argument('--region', required=True)

    parser.add_argument('--client-id', required=True)

    parser.add_argument(
        '--username', required=True, help='Cognito sign-in name; aliases resolve at login'
    )

    parser.add_argument(
        '--resume-only',
        action='store_true',
        help='Require saved credentials; never fall back to a password prompt',
    )

    parser.add_argument('--file-group', '--category', dest='file_group', default='workflow-docs')

    parser.add_argument(
        '--logout', action='store_true', help='Delete the saved login and revoke its refresh token'
    )

    parser.add_argument('--metadata', default='{}', help='JSON object applied to each file')

    parser.add_argument('files', nargs='*', type=Path)
    args = parser.parse_args()

    if not args.logout and not args.files:
        parser.error('Supply Markdown files or --logout')

    try:
        if not os.environ.get('SSL_CERT_FILE'):
            os.environ['SSL_CERT_FILE'] = get_cert_path(True)
        client = boto3.client(
            'cognito-idp',
            region_name=args.region,
            config=Config(
                signature_version=UNSIGNED,
                retries={'total_max_attempts': 1},
                connect_timeout=5,
                read_timeout=30,
                ignore_configured_endpoint_urls=True,
            ),
        )
        config = {'cognito_client_id': args.client_id, 'cognito_region': args.region}

        if args.logout:
            logout_saved(config, args.username, client)

            print('Signed out on this Mac.')

            return

        try:
            session = resume_session(config, args.username, client)
        except LoginRequired:
            if args.resume_only:
                raise
            session = login(config, args.username, client, remember=True)
        token, username = session.credentials()
        registration = send_upload(
            args.upload_url, {'username': username, 'action': 'register_device'}, token
        )

        if registration.get('approval_required') is True:
            receipt = {key: registration.get(key) for key in ('owner_id', 'fingerprint')}

            if any(
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in '0123456789abcdef' for char in value)
                for value in receipt.values()
            ):
                raise ValueError('Invalid approval receipt')

            print(json.dumps({'status': 'device-approval-required', **receipt}))
            raise SystemExit(2)

        if registration != {'registered': True}:
            raise ValueError('Device association failed')
        payload = {
            'username': username,
            'file-group': args.file_group,
            'files': [
                {
                    'name': path.name,
                    'content': path.read_text(encoding='utf-8'),
                    'category': args.file_group,
                    'metadata': json.loads(args.metadata),
                }
                for path in args.files
            ],
        }
        result = send_upload(args.upload_url, payload, token)
    except (
        RuntimeError,
        BotoCoreError,
        ClientError,
        HTTPError,
        URLError,
        ValueError,
        OSError,
        KeyError,
        getpass.GetPassWarning,
        EOFError,
    ):
        print(
            'Login/upload failed; verify account setup and request outcome before retrying.',
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    print(json.dumps(result, indent=2))

    if not result.get('files') or any(item['status'] != 'READY' for item in result['files']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
