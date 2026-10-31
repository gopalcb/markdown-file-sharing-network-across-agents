"""Sign native requests with an explicitly selected macOS key and store sessions in Keychain."""

import base64
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


class DeviceSecurityUnavailable(RuntimeError):
    """Fail closed when a trusted native operation cannot complete."""


def private_directory() -> Path:
    """Require an owned private directory; never follow a substituted state directory."""
    directory = Path.home() / '.local' / 'state' / 'work-organizer'

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()

    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DeviceSecurityUnavailable('Unsafe private device directory')

    return directory


@contextmanager
def credential_lock(account: str):
    """Serialize credential read/refresh/logout across project runtimes on this Mac."""

    if len(account) != 64 or any(char not in '0123456789abcdef' for char in account):
        raise DeviceSecurityUnavailable('Invalid credential identity')
    descriptor = os.open(
        private_directory() / (account + '.lock'),
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )

    with os.fdopen(descriptor, 'w') as stream:
        info = os.fstat(stream.fileno())

        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise DeviceSecurityUnavailable('Unsafe credential lock')

        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def helper_path() -> Path:
    """Compile once per source version, outside document trees; never compile per upload."""

    if sys.platform != 'darwin':
        raise DeviceSecurityUnavailable('Native signed uploads require macOS')
    source = Path(__file__).with_name('mac_device_key.swift')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    output = private_directory() / ('device-helper-' + digest)

    with credential_lock(hashlib.sha256(b'work-organizer-helper-build').hexdigest()):
        if not output.exists():
            temporary = output.with_name(output.name + '.' + uuid.uuid4().hex)

            try:
                subprocess.run(
                    ['/usr/bin/xcrun', 'swiftc', str(source), '-O', '-o', str(temporary)],
                    check=True,
                    capture_output=True,
                    timeout=90,
                )

                temporary.chmod(0o700)

                os.replace(temporary, output)
            except (OSError, subprocess.SubprocessError):
                raise DeviceSecurityUnavailable(
                    'Install Apple command-line tools for device signing'
                ) from None
            finally:
                temporary.unlink(missing_ok=True)
        info = output.lstat()

        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise DeviceSecurityUnavailable('Unsafe device helper')

    return output


def native(operation: str, **values) -> dict:
    """Use private pipes; callers must never log captured output or supplied credentials."""

    mode = os.environ.get('WORK_ORGANIZER_DEVICE_KEY_MODE', 'secure-enclave')
    if mode not in ('secure-enclave', 'standard-keychain'):
        raise DeviceSecurityUnavailable('Invalid device key mode')
    try:
        result = subprocess.run(
            [str(helper_path())],
            input=json.dumps({**values, 'operation': operation, 'key_mode': mode}),
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )

        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise DeviceSecurityUnavailable(
            'Device key or Keychain unavailable; unlock this Mac'
        ) from None


def credential_id(config: dict, username: str) -> str:
    """Namespace saved credentials by region, client and explicit sign-in name."""

    return hashlib.sha256(
        json.dumps(
            [config.get('cognito_region', ''), config['cognito_client_id'], username],
            separators=(',', ':'),
        ).encode()
    ).hexdigest()


def signed_headers(endpoint: str, body: bytes, access_token: str, metadata: dict) -> dict:
    """Bind the exact request bytes and identity credential to fresh device proof."""
    target = urlsplit(endpoint)

    if (
        target.scheme != 'https'
        or target.path not in ('/documents', '/browser-documents')
        or target.query
        or target.fragment
        or target.username
        or target.password
    ):
        raise DeviceSecurityUnavailable('Signed uploads require the approved HTTPS document route')
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    fields = [
        'WO-DEVICE-V1',
        'POST',
        target.path,
        timestamp,
        nonce,
        hashlib.sha256(body).hexdigest(),
        hashlib.sha256(access_token.encode()).hexdigest(),
        metadata['X-Device-Id'],
        metadata['X-Device-Mac'],
    ]
    proof = native('sign', message=base64.b64encode('\n'.join(fields).encode()).decode())

    return {
        **metadata,
        'X-Device-Key': proof['public_key'],
        'X-Device-Timestamp': timestamp,
        'X-Device-Nonce': nonce,
        'X-Device-Signature': proof['signature'],
    }
