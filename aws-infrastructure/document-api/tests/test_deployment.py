"""Test deployment identity and artifact-bucket failure boundaries without AWS writes."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'prepare_artifact_bucket', ROOT / 'scripts/prepare_artifact_bucket.py'
)
prepare = importlib.util.module_from_spec(SPEC)

SPEC.loader.exec_module(prepare)


@pytest.fixture
def session():
    """Supply a simulated CI/CD identity and a correctly secured existing bucket."""
    session = Mock(region_name=prepare.REGION)
    sts, s3 = Mock(), Mock()
    session.client.side_effect = lambda service: {'sts': sts, 's3': s3}[service]
    sts.get_caller_identity.return_value = {
        'Account': prepare.ACCOUNT,
        'Arn': prepare.ROLE_PREFIX + 'test',
    }
    s3.get_bucket_location.return_value = {'LocationConstraint': prepare.REGION}
    s3.get_public_access_block.return_value = {
        'PublicAccessBlockConfiguration': prepare.PUBLIC_ACCESS_BLOCK
    }
    s3.get_bucket_encryption.return_value = {
        'ServerSideEncryptionConfiguration': {
            'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]
        }
    }

    return session


@pytest.mark.parametrize(
    'account,arn,region',
    [
        ('000000000000', prepare.ROLE_PREFIX + 'test', prepare.REGION),
        (prepare.ACCOUNT, f'arn:aws:iam::{prepare.ACCOUNT}:user/admin', prepare.REGION),
        (prepare.ACCOUNT, prepare.ROLE_PREFIX + 'test', 'us-east-1'),
    ],
)
def test_wrong_target_never_touches_s3(session, account: str, arn: str, region: str) -> None:
    """Reject unexpected identities and regions before creating an S3 client."""
    session.region_name = region
    session.client('sts').get_caller_identity.return_value = {'Account': account, 'Arn': arn}

    session.client.reset_mock()

    with pytest.raises(ValueError):
        prepare.prepare_bucket(session)
    assert [call.args[0] for call in session.client.call_args_list] == ['sts']


def test_existing_bucket_is_verified_and_secured(session) -> None:
    """Repeated preparation does not recreate the bucket and checks ownership on reads/writes."""
    s3 = session.client('s3')
    assert prepare.prepare_bucket(session) == prepare.BUCKET

    s3.create_bucket.assert_not_called()

    for operation in [
        'get_bucket_location',
        'put_public_access_block',
        'put_bucket_encryption',
        'get_public_access_block',
        'get_bucket_encryption',
    ]:
        assert getattr(s3, operation).call_args.kwargs['ExpectedBucketOwner'] == prepare.ACCOUNT


def test_missing_bucket_is_created_then_verified(session) -> None:
    """Only explicit NoSuchBucket triggers creation in the configured region."""
    s3 = session.client('s3')
    s3.get_bucket_location.side_effect = [
        ClientError({'Error': {'Code': 'NoSuchBucket'}}, 'GetBucketLocation'),
        {'LocationConstraint': prepare.REGION},
    ]
    assert prepare.prepare_bucket(session) == prepare.BUCKET

    s3.create_bucket.assert_called_once_with(
        Bucket=prepare.BUCKET, CreateBucketConfiguration={'LocationConstraint': prepare.REGION}
    )
    assert s3.get_bucket_location.call_count == 2


@pytest.mark.parametrize('code', ['AccessDenied', 'NoSuchAccessPoint', 'PermanentRedirect'])
def test_lookup_failure_is_not_treated_as_missing(session, code: str) -> None:
    """Fail closed on denied, redirected or otherwise unexpected bucket access."""
    s3 = session.client('s3')
    s3.get_bucket_location.side_effect = ClientError({'Error': {'Code': code}}, 'GetBucketLocation')

    with pytest.raises(ClientError):
        prepare.prepare_bucket(session)

    s3.create_bucket.assert_not_called()

    s3.put_public_access_block.assert_not_called()


def test_existing_bucket_wrong_region_is_untouched(session) -> None:
    """Do not reconfigure a same-name bucket in a different region."""
    s3 = session.client('s3')
    s3.get_bucket_location.return_value = {'LocationConstraint': None}

    with pytest.raises(ValueError):
        prepare.prepare_bucket(session)

    s3.put_public_access_block.assert_not_called()


@pytest.mark.parametrize('failure', ['public_access', 'encryption'])
def test_security_readback_must_match(session, failure: str) -> None:
    """Stop deployment if the required bucket protection is not confirmed."""
    s3 = session.client('s3')

    if failure == 'public_access':
        s3.get_public_access_block.return_value = {'PublicAccessBlockConfiguration': {}}
    else:
        s3.get_bucket_encryption.return_value = {'ServerSideEncryptionConfiguration': {'Rules': []}}

    with pytest.raises(RuntimeError):
        prepare.prepare_bucket(session)


def test_workflow_aws_credentials_only_on_main() -> None:
    """Standalone validation cannot obtain AWS credentials or deploy infrastructure."""
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.load((root / '.github/workflows/validate.yml').read_text(), Loader=yaml.BaseLoader)
    assert workflow['permissions'] == {'contents': 'read'}
    assert set(workflow['jobs']) == {'validate'}
    text = (root / '.github/workflows/validate.yml').read_text()
    for forbidden in ('configure-aws-credentials', 'id-token', 'cloudformation deploy', 'aws s3api'):
        assert forbidden not in text
