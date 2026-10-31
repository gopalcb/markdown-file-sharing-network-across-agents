"""Prepare the dedicated private artifact bucket using the CI/CD role only."""

import boto3
from botocore.exceptions import ClientError

ACCOUNT = '123456789012'
REGION = 'ca-central-1'
ROLE_PREFIX = f'arn:aws:sts::{ACCOUNT}:assumed-role/softrobots-gb-cicd-role/'
BUCKET = f'softrobots-document-api-artifacts-{ACCOUNT}-{REGION}'
PUBLIC_ACCESS_BLOCK = {
    'BlockPublicAcls': True,
    'IgnorePublicAcls': True,
    'BlockPublicPolicy': True,
    'RestrictPublicBuckets': True,
}


def prepare_bucket(session) -> str:
    """Verify the deployment identity, then create/secure only the named artifact bucket."""
    identity = session.client('sts').get_caller_identity()

    if (
        session.region_name != REGION
        or identity['Account'] != ACCOUNT
        or not identity['Arn'].startswith(ROLE_PREFIX)
    ):
        raise ValueError(
            'Expected the dedicated CI/CD role in account 123456789012, region ca-central-1'
        )
    s3 = session.client('s3')

    try:
        location = s3.get_bucket_location(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT)
    except ClientError as error:
        if error.response['Error']['Code'] != 'NoSuchBucket':
            raise

        try:
            s3.create_bucket(
                Bucket=BUCKET, CreateBucketConfiguration={'LocationConstraint': REGION}
            )
        except ClientError as create_error:
            # A concurrent authorized bootstrap may win the create race; verify ownership below.
            if create_error.response['Error']['Code'] != 'BucketAlreadyOwnedByYou':
                raise
        location = s3.get_bucket_location(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT)

    if location.get('LocationConstraint') != REGION:
        raise ValueError('Artifact bucket is not in the expected region')

    s3.put_public_access_block(
        Bucket=BUCKET,
        ExpectedBucketOwner=ACCOUNT,
        PublicAccessBlockConfiguration=PUBLIC_ACCESS_BLOCK,
    )

    s3.put_bucket_encryption(
        Bucket=BUCKET,
        ExpectedBucketOwner=ACCOUNT,
        ServerSideEncryptionConfiguration={
            'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]
        },
    )
    actual_block = s3.get_public_access_block(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT)[
        'PublicAccessBlockConfiguration'
    ]
    encryption = s3.get_bucket_encryption(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT)[
        'ServerSideEncryptionConfiguration'
    ]

    if actual_block != PUBLIC_ACCESS_BLOCK:
        raise RuntimeError(
            'Artifact bucket public access block did not match the required settings'
        )

    if not encryption['Rules'] or any(
        rule['ApplyServerSideEncryptionByDefault']['SSEAlgorithm'] != 'AES256'
        for rule in encryption['Rules']
    ):
        raise RuntimeError('Artifact bucket default encryption was not confirmed')

    return BUCKET


def main() -> None:
    """Use the existing short-lived OIDC session; print only the verified bucket name."""

    print(prepare_bucket(boto3.Session()))


if __name__ == '__main__':
    main()
