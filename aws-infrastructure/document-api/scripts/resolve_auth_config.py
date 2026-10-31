"""Resolve upload deployment settings from the existing, activated auth stack.

Only read AWS configuration here. CI exports validated nonsecret identifiers before
artifact uploads or infrastructure writes; optional overrides must match the stack.
"""

import os
import re
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ACCOUNT = '123456789012'
REGION = 'ca-central-1'
POOL = 'ca-central-1_ExampleNative'
STACK = 'softrobots-portal-auth'
BROWSER_POOL = 'ca-central-1_ExampleBrowser'
BROWSER_CLIENT = 'examplebrowserclient'
BROWSER_STACK = 'softrobots-username-auth'
ROLE_PREFIX = f'arn:aws:sts::{ACCOUNT}:assumed-role/softrobots-gb-cicd-role/'


def resolve_config(session, pool_override: str = '', client_override: str = '') -> dict:
    """Require the approved CI target and completed auth stack before returning settings."""
    identity = session.client('sts').get_caller_identity()

    if (
        session.region_name != REGION
        or identity.get('Account') != ACCOUNT
        or not identity.get('Arn', '').startswith(ROLE_PREFIX)
    ):
        raise ValueError(
            'Upload configuration requires the dedicated CI role in the approved account/region'
        )

    if bool(pool_override) != bool(client_override):
        raise ValueError('Set both upload overrides or leave both unset to use auth stack outputs')

    try:
        state = session.client('cloudformation').describe_stacks(StackName=STACK)['Stacks'][0]
    except ClientError as error:
        if error.response['Error'][
            'Code'
        ] == 'ValidationError' and 'does not exist' in error.response['Error'].get('Message', ''):
            raise ValueError(
                'Activate softrobots-portal-auth through portal-auth.yml after installing the current auth IAM prerequisites'
            ) from None
        raise

    if (
        not state.get('StackId', '').startswith(
            f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK}/'
        )
        or state.get('StackStatus') not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}
        or {'Key': 'Project', 'Value': 'softrobots-systems'} not in state.get('Tags', [])
    ):
        raise ValueError(
            'Auth stack identity, ownership or completion is invalid; finish auth activation first'
        )
    outputs = {item['OutputKey']: item['OutputValue'] for item in state.get('Outputs', [])}
    pool = outputs.get('UserPoolId', '')
    client = outputs.get('UploadClientId', '')

    if (
        pool != POOL
        or not re.fullmatch(r'[a-z0-9]{1,128}', client)
        or outputs.get('SessionDevicesTableName') != 'softrobots-auth-sessions'
    ):
        raise ValueError('Activate the approved family-pool clients and session/device table first')

    if pool_override and (pool_override != pool or client_override != client):
        raise ValueError('Upload overrides do not match the activated auth stack')

    return {'UPLOAD_POOL': pool, 'UPLOAD_CLIENT': client, **resolve_browser_config(session)}


def resolve_browser_config(session) -> dict:
    """Read the completed username stack without changing the existing upload issuer."""
    state = session.client('cloudformation').describe_stacks(StackName=BROWSER_STACK)['Stacks'][0]
    outputs = {item['OutputKey']: item['OutputValue'] for item in state.get('Outputs', [])}

    if (
        not state.get('StackId', '').startswith(
            f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{BROWSER_STACK}/'
        )
        or state.get('StackStatus') not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}
        or {'Key': 'Project', 'Value': 'softrobots-systems'} not in state.get('Tags', [])
        or outputs.get('UserPoolId') != BROWSER_POOL
        or outputs.get('UploadClientId') != BROWSER_CLIENT
    ):
        raise ValueError('Activate the approved completed username-pool upload client first')

    return {'BROWSER_UPLOAD_POOL': BROWSER_POOL, 'BROWSER_UPLOAD_CLIENT': BROWSER_CLIENT}


def main() -> None:
    """Write validated public identifiers to the GitHub environment after successful reads."""
    values = resolve_config(
        boto3.Session(region_name=REGION),
        os.environ.get('UPLOAD_POOL_OVERRIDE', ''),
        os.environ.get('UPLOAD_CLIENT_OVERRIDE', ''),
    )

    with Path(os.environ['GITHUB_ENV']).open('a') as output:
        output.write(''.join(f'{key}={value}\n' for key, value in values.items()))


if __name__ == '__main__':
    main()
