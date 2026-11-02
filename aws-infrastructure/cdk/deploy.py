"""Build, inspect, and deploy telemetry through local or GitHub OIDC credentials."""

import argparse
import hashlib
import json
import mimetypes
import os
import re
import ssl
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import ClientError
from botocore.httpsession import get_cert_path

ACCOUNT = '249766001220'
REGION = 'ca-central-1'
CDK_CLI = '2.1147.0'
PORTAL_ORIGIN = 'https://d2f8oftnpxmjan.cloudfront.net'
DISTRIBUTION_ID = 'E224O80UCWC7IJ'
MCP_ORIGIN = 'https://d3cjbof2ij7jy9.cloudfront.net'
MCP_DISTRIBUTION_ID = 'E3BS4AEVAMTQSF'
CDK_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = CDK_ROOT.parents[1]
DEPLOYMENT_ROOT = REPOSITORY_ROOT / '.deployment'
OUTPUTS_PATH = DEPLOYMENT_ROOT / 'telemetry-cdk-outputs.json'
PREPARE_PATH = DEPLOYMENT_ROOT / 'telemetry-prepare.json'
MCP_PREPARE_PATH = DEPLOYMENT_ROOT / 'mcp-telemetry-prepare.json'


def run_command(command: list[str], cwd: Path, allowed: set[int] | None = None) -> str:
    """Run one bounded local command and reject unexpected outcomes."""
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    allowed_codes = allowed or {0}

    if result.returncode not in allowed_codes:
        message = result.stderr.strip() or result.stdout.strip() or 'command failed'
        raise RuntimeError(f'{command[0]} failed: {message[-2000:]}')

    return result.stdout + result.stderr


def verified_session(profile: str | None) -> boto3.Session:
    """Require the approved AWS account and region for local or ambient credentials."""
    session = boto3.Session(profile_name=profile, region_name=REGION)
    identity = session.client('sts').get_caller_identity()

    if identity.get('Account') != ACCOUNT or session.region_name != REGION:
        raise RuntimeError('Deployment identity must target the approved account and region')

    return session


def source_digest() -> str:
    """Bind prepare receipts to runtime, UI, infrastructure and build configuration."""
    roots = [
        REPOSITORY_ROOT / 'aws-infrastructure',
        REPOSITORY_ROOT / 'telemetry-api',
        REPOSITORY_ROOT / 'telemetry-ui',
    ]
    paths = {REPOSITORY_ROOT / 'package.json', REPOSITORY_ROOT / 'package-lock.json'}
    ignored = {
        '__pycache__',
        '.pytest_cache',
        '.ruff_cache',
        '.angular',
        '.venv',
        'node_modules',
        'dist',
        'cdk.out',
        'tests',
    }
    digest = hashlib.sha256()

    for root in roots:
        paths.update(root.rglob('*'))

    for path in sorted(paths):
        relative = path.relative_to(REPOSITORY_ROOT)

        if (
            not path.is_file()
            or path.is_symlink()
            or ignored.intersection(relative.parts)
            or path.suffix == '.md'
            or path.name.endswith('.spec.ts')
        ):
            continue
        data = path.read_bytes()

        digest.update(relative.as_posix().encode() + b'\0')

        digest.update(len(data).to_bytes(8, 'big'))

        digest.update(data)

    return digest.hexdigest()


def cdk_command(arguments: list[str], phase: str) -> list[str]:
    """Build a pinned CDK CLI command using the active Python environment."""

    return [
        'npx',
        '--yes',
        f'aws-cdk@{CDK_CLI}',
        '--app',
        f'{sys.executable} app.py',
        *arguments,
        '--context',
        f'phase={phase}',
    ]


def synthesize_and_diff(phase: str, stacks: list[str]) -> None:
    """Synthesize locally and save the reviewed CloudFormation difference."""

    DEPLOYMENT_ROOT.mkdir(parents=True, exist_ok=True)

    run_command(cdk_command(['synth', *stacks, '--quiet'], phase), CDK_ROOT)
    difference = run_command(
        cdk_command(['diff', *stacks, '--fail'], phase),
        CDK_ROOT,
        allowed={0, 1},
    )

    (DEPLOYMENT_ROOT / f'telemetry-{phase}.diff.txt').write_text(difference)


def deploy_stacks(phase: str, stacks: list[str]) -> dict:
    """Deploy the exact reviewed stacks once and return CDK outputs."""
    command = cdk_command(
        [
            'deploy',
            *stacks,
            '--require-approval',
            'never',
            '--outputs-file',
            str(OUTPUTS_PATH),
        ],
        phase,
    )

    run_command(command, CDK_ROOT)

    return json.loads(OUTPUTS_PATH.read_text())


def stack_outputs(outputs: dict, stack_name: str) -> dict:
    """Return one stack output map regardless of construct or deployed stack key."""

    if stack_name in outputs:
        return outputs[stack_name]

    if len(outputs) == 1:
        return next(iter(outputs.values()))

    raise RuntimeError(f'Deployment outputs do not contain {stack_name}')


def auth_environment(api_host: str) -> dict:
    """Return the nonsecret browser Cognito configuration for static publication."""

    return {
        'COGNITO_ISSUER': ('https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_yXCtjKCHu'),
        'COGNITO_CLIENT_ID': '3i3d9n9dsaq3cjttk7r5mnbrt',
        'COGNITO_DOMAIN': ('https://ca-central-1yxctjkchu.auth.ca-central-1.amazoncognito.com'),
        'AUTH_REDIRECT_URI': f'{PORTAL_ORIGIN}/work-organizer/auth/callback',
        'AUTH_LOGOUT_URI': f'{PORTAL_ORIGIN}/work-organizer/signed-out',
        'API_ORIGIN': f'https://{api_host}',
    }


def auth_config_bytes(api_host: str) -> bytes:
    """Serialize the fixed nonsecret browser configuration after validating the API host."""

    if not re.fullmatch(r'[a-z0-9]+\.execute-api\.ca-central-1\.amazonaws\.com', api_host):
        raise ValueError('API host must be the deployed ca-central-1 HTTP API')
    environment = auth_environment(api_host)
    config = {
        'issuer': environment['COGNITO_ISSUER'],
        'clientId': environment['COGNITO_CLIENT_ID'],
        'domain': environment['COGNITO_DOMAIN'],
        'redirectUri': environment['AUTH_REDIRECT_URI'],
        'logoutUri': environment['AUTH_LOGOUT_URI'],
        'passkeysEnabled': False,
    }

    return (json.dumps(config) + '\n').encode()


def publish_assets(session: boto3.Session, bucket: str, api_host: str) -> dict:
    """Publish immutable UI assets first and runtime configuration/index last."""
    assets = REPOSITORY_ROOT / 'telemetry-ui' / 'dist' / 'telemetry-ui' / 'browser'

    if not (assets / 'index.html').is_file():
        raise RuntimeError('Compiled Angular index.html is required')
    s3 = session.client('s3')
    manifest = {}
    paths = sorted(assets.rglob('*'), key=lambda path: path.name == 'index.html')

    for path in paths:
        if path.is_symlink():
            raise RuntimeError('UI releases cannot contain symlinks')

        if not path.is_file() or path.name in {'auth-config.json', 'index.html'}:
            continue
        key = path.relative_to(assets).as_posix()
        data = path.read_bytes()
        immutable = bool(re.search(r'-[A-Za-z0-9]{8,}\.(?:js|css|woff2?)$', key))
        response = s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=mimetypes.guess_type(key)[0] or 'application/octet-stream',
            CacheControl='public, max-age=31536000, immutable' if immutable else 'no-store',
        )
        manifest[key] = {
            'sha256': hashlib.sha256(data).hexdigest(),
            'version_id': response.get('VersionId', ''),
        }

    for name, data, content_type in [
        ('auth-config.json', auth_config_bytes(api_host), 'application/json'),
        ('index.html', (assets / 'index.html').read_bytes(), 'text/html'),
    ]:
        response = s3.put_object(
            Bucket=bucket,
            Key=name,
            Body=data,
            ContentType=content_type,
            CacheControl='no-store',
        )
        manifest[name] = {
            'sha256': hashlib.sha256(data).hexdigest(),
            'version_id': response.get('VersionId', ''),
        }

    return manifest


def inspect_published_assets(session: boto3.Session, bucket: str) -> dict:
    """Read back every currently published UI object with its exact version and hash."""
    s3 = session.client('s3')
    listed = s3.list_objects_v2(Bucket=bucket)

    if listed.get('IsTruncated'):
        raise RuntimeError('UI asset read-back must fit one bounded S3 page')
    manifest = {}

    for item in listed.get('Contents', []):
        response = s3.get_object(Bucket=bucket, Key=item['Key'])
        stream = response['Body']

        try:
            data = stream.read()
        finally:
            stream.close()
        manifest[item['Key']] = {
            'sha256': hashlib.sha256(data).hexdigest(),
            'version_id': response.get('VersionId', ''),
        }

    if 'index.html' not in manifest or 'auth-config.json' not in manifest:
        raise RuntimeError('Published UI is missing index.html or auth-config.json')

    return manifest


def verify_published_assets(expected: dict, published: dict) -> None:
    """Require every uploaded UI byte and returned version to survive S3 read-back."""

    for key, receipt in expected.items():
        observed = published.get(key)

        if observed is None or observed['sha256'] != receipt['sha256']:
            raise RuntimeError(f'Published UI read-back differs for {key}')

        if receipt['version_id'] and observed['version_id'] != receipt['version_id']:
            raise RuntimeError(f'Published UI version changed during release for {key}')


def read_health(url: str) -> dict:
    """Call the public health endpoint once with botocore's maintained CA bundle."""
    context = ssl.create_default_context(cafile=get_cert_path(True))

    with urlopen(url, timeout=35, context=context) as response:
        health = json.loads(response.read())

        if response.status != 200 or health.get('status') != 'ok':
            raise RuntimeError('telemetry-lambda returned an invalid health response')

    return health


def inspect_mcp_endpoint(origin: str) -> dict:
    """Check exact discovery and anonymous denial without credentials."""
    context = ssl.create_default_context(cafile=get_cert_path(True))
    metadata_url = origin + '/.well-known/oauth-protected-resource/mcp'

    with urlopen(metadata_url, timeout=10, context=context) as response:
        metadata = json.loads(response.read())

    if metadata.get('resource') != MCP_ORIGIN + '/mcp/':
        raise RuntimeError('MCP discovery changed the stable public resource')

    authorization_url = origin + '/.well-known/oauth-authorization-server'

    with urlopen(authorization_url, timeout=10, context=context) as response:
        authorization = json.loads(response.read())

    if (
        authorization.get('issuer') != MCP_ORIGIN + '/'
        or authorization.get('authorization_endpoint') != MCP_ORIGIN + '/authorize'
        or authorization.get('token_endpoint') != MCP_ORIGIN + '/token'
        or authorization.get('registration_endpoint') != MCP_ORIGIN + '/register'
    ):
        raise RuntimeError('MCP authorization-server discovery is invalid')

    request = Request(
        origin + '/mcp/',
        data=b'{}',
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    try:
        urlopen(request, timeout=10, context=context)
    except HTTPError as error:
        challenge = error.headers.get('WWW-Authenticate', '')

        if error.code != 401 or 'resource_metadata=' not in challenge:
            raise RuntimeError('MCP anonymous denial or challenge is invalid') from None
    else:
        raise RuntimeError('MCP unexpectedly accepted an anonymous request')

    return {
        'metadata_status': 200,
        'authorization_metadata_status': 200,
        'anonymous_status': 401,
    }


def inspect_runtime(session: boto3.Session, function_name: str) -> str:
    """Return the immutable deployed image URI after runtime and concurrency checks."""
    lambda_client = session.client('lambda')
    function = lambda_client.get_function(FunctionName=function_name)
    configuration = function['Configuration']
    concurrency = lambda_client.get_function_concurrency(FunctionName=function_name)
    image_uri = function['Code'].get('ResolvedImageUri', '')

    if (
        configuration.get('State') != 'Active'
        or concurrency.get('ReservedConcurrentExecutions') != 10
    ):
        raise RuntimeError('telemetry-lambda is not active with reserved concurrency 10')

    if not re.fullmatch(r'.+@sha256:[0-9a-f]{64}', image_uri):
        raise RuntimeError('telemetry-lambda did not expose an immutable resolved image URI')

    return image_uri


def mcp_prepare_record(profile: str | None, outputs: dict, probe: dict, runtime_image: str) -> dict:
    """Persist one MCP prepare receipt bound to the exact consolidated source."""
    record = {
        'schema_version': 1,
        'phase': 'mcp-prepared',
        'profile': profile or 'ambient-oidc',
        'account': ACCOUNT,
        'region': REGION,
        'source_sha256': source_digest(),
        'prepared_at': datetime.now(UTC).isoformat(),
        'outputs': outputs,
        'probe': probe,
        'runtime_image': runtime_image,
    }

    MCP_PREPARE_PATH.write_text(json.dumps(record, indent=2) + '\n')

    return record


def prepare_mcp(session: boto3.Session, profile: str | None) -> dict:
    """Deploy the combined image once and validate MCP directly before edge cutover."""

    synthesize_and_diff('mcp-prepare', ['SoftrobotsTelemetry'])
    deployed = deploy_stacks('mcp-prepare', ['SoftrobotsTelemetry'])
    outputs = stack_outputs(deployed, 'softrobots-telemetry')

    read_health(outputs['ApiHealthUrl'])
    probe = inspect_mcp_endpoint(f'https://{outputs["ApiHost"]}')
    runtime_image = inspect_runtime(session, outputs['FunctionName'])

    return mcp_prepare_record(profile, outputs, probe, runtime_image)


def reconcile_mcp(session: boto3.Session, profile: str | None) -> dict:
    """Validate MCP on the telemetry deployment without deploying the image twice."""

    if not PREPARE_PATH.is_file():
        raise RuntimeError('MCP reconciliation requires the successful telemetry prepare record')
    prepared = json.loads(PREPARE_PATH.read_text())

    if prepared.get('source_sha256') != source_digest():
        raise RuntimeError('Telemetry prepare record does not match the current source')
    stacks = session.client('cloudformation').describe_stacks(StackName='softrobots-telemetry')
    stack = stacks['Stacks'][0]

    if stack['StackStatus'] not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
        raise RuntimeError('Telemetry stack is not in the expected completed state')
    outputs = {item['OutputKey']: item['OutputValue'] for item in stack['Outputs']}

    for key in ('ApiHost', 'ApiHealthUrl', 'FunctionName'):
        if outputs.get(key) != prepared.get('outputs', {}).get(key):
            raise RuntimeError(f'Live telemetry output changed after prepare: {key}')
    runtime_image = inspect_runtime(session, outputs['FunctionName'])

    if runtime_image != prepared.get('runtime_image'):
        raise RuntimeError('Live telemetry image changed after prepare')

    read_health(outputs['ApiHealthUrl'])
    probe = inspect_mcp_endpoint(f'https://{outputs["ApiHost"]}')

    return mcp_prepare_record(profile, outputs, probe, runtime_image)


def cutover_mcp(session: boto3.Session) -> dict:
    """Switch the retained MCP distribution only for the directly checked image."""

    if not MCP_PREPARE_PATH.is_file():
        raise RuntimeError('MCP cutover requires a successful direct prepare record')
    prepared = json.loads(MCP_PREPARE_PATH.read_text())

    if prepared.get('source_sha256') != source_digest():
        raise RuntimeError('Source changed after MCP prepare; prepare the exact source again')

    if inspect_runtime(session, prepared['outputs']['FunctionName']) != prepared.get(
        'runtime_image'
    ):
        raise RuntimeError('Live telemetry image changed after MCP reconciliation')

    synthesize_and_diff('mcp-cutover', ['MarkdownMcpEdge'])
    outputs = deploy_stacks('mcp-cutover', ['MarkdownMcpEdge'])
    invalidation = session.client('cloudfront').create_invalidation(
        DistributionId=MCP_DISTRIBUTION_ID,
        InvalidationBatch={
            'CallerReference': f'mcp-telemetry-{prepared["source_sha256"]}',
            'Paths': {'Quantity': 1, 'Items': ['/*']},
        },
    )
    probe = inspect_mcp_endpoint(MCP_ORIGIN)

    return {
        'phase': 'mcp-cutover',
        'source_sha256': prepared['source_sha256'],
        'outputs': outputs,
        'probe': probe,
        'invalidation_id': invalidation['Invalidation']['Id'],
        'cutover_at': datetime.now(UTC).isoformat(),
    }


def save_prepare_record(
    profile: str | None, outputs: dict, manifest: dict, runtime_image: str
) -> dict:
    """Persist exact local source, cloud outputs and asset versions for cutover."""
    record = {
        'schema_version': 1,
        'phase': 'prepared',
        'profile': profile or 'ambient-oidc',
        'account': ACCOUNT,
        'region': REGION,
        'source_sha256': source_digest(),
        'prepared_at': datetime.now(UTC).isoformat(),
        'outputs': outputs,
        'assets': manifest,
        'runtime_image': runtime_image,
        'health_status_code': 200,
    }

    PREPARE_PATH.write_text(json.dumps(record, indent=2) + '\n')

    return record


def prepare(session: boto3.Session, profile: str | None) -> dict:
    """Build, deploy and directly validate the replacement without edge cutover."""

    run_command(['npm', 'run', 'build'], REPOSITORY_ROOT)

    synthesize_and_diff('prepare', ['SoftrobotsTelemetry'])
    deployed = deploy_stacks('prepare', ['SoftrobotsTelemetry'])
    outputs = stack_outputs(deployed, 'softrobots-telemetry')
    manifest = publish_assets(session, outputs['UiBucketName'], outputs['ApiHost'])
    published = inspect_published_assets(session, outputs['UiBucketName'])

    verify_published_assets(manifest, published)

    read_health(outputs['ApiHealthUrl'])
    runtime_image = inspect_runtime(session, outputs['FunctionName'])

    return save_prepare_record(profile, outputs, manifest, runtime_image)


def reconcile_prepare(session: boto3.Session, profile: str | None) -> dict:
    """Record an already-complete prepare deployment without redeploying it."""
    stacks = session.client('cloudformation').describe_stacks(StackName='softrobots-telemetry')
    stack = stacks['Stacks'][0]

    if stack['StackStatus'] not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
        raise RuntimeError('Telemetry stack is not in the expected completed state')
    outputs = {item['OutputKey']: item['OutputValue'] for item in stack['Outputs']}
    runtime_image = inspect_runtime(session, outputs['FunctionName'])
    manifest = inspect_published_assets(session, outputs['UiBucketName'])

    read_health(outputs['ApiHealthUrl'])

    return save_prepare_record(profile, outputs, manifest, runtime_image)


def cutover(session: boto3.Session) -> dict:
    """Switch the existing distribution only for the exact prepared source."""

    if not PREPARE_PATH.is_file():
        raise RuntimeError('Cutover requires a successful local prepare record')
    prepared = json.loads(PREPARE_PATH.read_text())

    if prepared.get('source_sha256') != source_digest():
        raise RuntimeError('Source changed after prepare; prepare the exact source again')

    synthesize_and_diff('cutover', ['SoftrobotsPortalEdge'])
    outputs = deploy_stacks('cutover', ['SoftrobotsPortalEdge'])
    invalidation = session.client('cloudfront').create_invalidation(
        DistributionId=DISTRIBUTION_ID,
        InvalidationBatch={
            'CallerReference': f'telemetry-{prepared["source_sha256"]}',
            'Paths': {
                'Quantity': 3,
                'Items': ['/index.html', '/auth-config.json', '/work-organizer/*'],
            },
        },
    )

    return {
        'phase': 'cutover',
        'source_sha256': prepared['source_sha256'],
        'outputs': outputs,
        'invalidation_id': invalidation['Invalidation']['Id'],
        'cutover_at': datetime.now(UTC).isoformat(),
    }


def deploy_identity() -> dict:
    """Adopt the active Cognito clients/tables and remove obsolete deployment IAM."""

    synthesize_and_diff('identity', ['SoftrobotsPortalIdentity'])

    return {
        'phase': 'identity',
        'outputs': deploy_stacks('identity', ['SoftrobotsPortalIdentity']),
    }


def retire_document_runtime(session: boto3.Session) -> dict:
    """Remove only the old document Lambda/API while retaining every data resource."""

    synthesize_and_diff('retire', ['SoftrobotsDocumentData'])
    outputs = deploy_stacks('retire', ['SoftrobotsDocumentData'])
    lambda_client = session.client('lambda')

    try:
        lambda_client.get_function(FunctionName='softrobots-document-api-upload')
    except ClientError as error:
        if error.response['Error']['Code'] != 'ResourceNotFoundException':
            raise
    else:
        raise RuntimeError('Superseded document Lambda still exists after retirement')

    dynamodb = session.client('dynamodb')
    s3 = session.client('s3')
    retained_tables = [
        'softrobots-document-api-user-files',
        'softrobots-projects',
        'softrobots-document-api-local-deletions',
        'softrobots-native-device-pending',
        'softrobots-native-device-approvals',
        'softrobots-native-device-nonces',
    ]
    retained_buckets = [
        f'agent-work-history--{ACCOUNT}-{REGION}',
        f'softrobots-document-api-{ACCOUNT}-{REGION}',
    ]

    for table in retained_tables:
        if dynamodb.describe_table(TableName=table)['Table']['TableStatus'] != 'ACTIVE':
            raise RuntimeError(f'Retained table is not active: {table}')

    for bucket in retained_buckets:
        s3.head_bucket(Bucket=bucket)

    return {
        'phase': 'retire',
        'outputs': outputs,
        'removed_function': 'softrobots-document-api-upload',
        'retained_tables': retained_tables,
        'retained_buckets': retained_buckets,
    }


def parse_args() -> argparse.Namespace:
    """Parse an explicit deployment phase and approved local profile."""
    parser = argparse.ArgumentParser()

    parser.add_argument(
        'phase',
        choices=[
            'prepare',
            'reconcile',
            'cutover',
            'identity',
            'retire',
            'mcp-prepare',
            'mcp-reconcile',
            'mcp-cutover',
        ],
    )

    parser.add_argument('--profile')

    return parser.parse_args()


def main() -> None:
    """Run one explicit CDK deployment phase through approved credentials."""
    args = parse_args()

    if args.profile:
        os.environ['AWS_PROFILE'] = args.profile
    else:
        os.environ.pop('AWS_PROFILE', None)
    os.environ['AWS_REGION'] = REGION
    os.environ['AWS_DEFAULT_REGION'] = REGION
    session = verified_session(args.profile)

    if args.phase == 'prepare':
        result = prepare(session, args.profile)
    elif args.phase == 'reconcile':
        result = reconcile_prepare(session, args.profile)
    elif args.phase == 'cutover':
        result = cutover(session)
    elif args.phase == 'identity':
        result = deploy_identity()
    elif args.phase == 'mcp-prepare':
        result = prepare_mcp(session, args.profile)
    elif args.phase == 'mcp-reconcile':
        result = reconcile_mcp(session, args.profile)
    elif args.phase == 'mcp-cutover':
        result = cutover_mcp(session)
    else:
        result = retire_document_runtime(session)

    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
