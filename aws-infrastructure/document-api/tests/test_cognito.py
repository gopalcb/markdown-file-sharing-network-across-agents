"""Verify access-token ownership, private login and credential transport boundaries offline."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import cognito_auth as auth
import pytest
import yaml
from test_boundaries import upload
from test_upload import app, event_for


@pytest.mark.parametrize(
    'claims',
    [
        {},
        {'token_use': 'id', 'username': 'alice', 'sub': 'one'},
        {'token_use': 'access', 'username': 'bob', 'sub': 'two'},
        {'token_use': 'access', 'username': 'alice'},
        {'token_use': 'access', 'sub': 'one'},
    ],
)
def test_denied_identity_never_writes(claims, monkeypatch):
    """Absent, identity-token and mismatched principals cannot create storage clients."""
    event = event_for(
        {
            'username': 'alice',
            'files': [
                {
                    'name': 'notes.md',
                    'content': '# Notes',
                    'category': 'workflow-docs',
                    'metadata': {},
                }
            ],
        }
    )
    event['requestContext']['authorizer']['jwt']['claims'] = claims
    session = Mock()

    monkeypatch.setattr(app.boto3, 'Session', session)
    response = app.lambda_handler(event, SimpleNamespace(aws_request_id='request'))
    assert response['statusCode'] == 403

    session.assert_not_called()
    assert 'alice' not in response['body']


def test_missing_authorizer_never_writes(monkeypatch):
    """A direct event with bearer text but no trusted authorizer context is denied."""
    event = event_for(
        {
            'username': 'alice',
            'files': [
                {
                    'name': 'notes.md',
                    'content': '# Notes',
                    'category': 'workflow-docs',
                    'metadata': {},
                }
            ],
        }
    )
    del event['requestContext']
    event['headers']['Authorization'] = 'Bearer unverified'
    session = Mock()

    monkeypatch.setattr(app.boto3, 'Session', session)
    assert app.lambda_handler(event, SimpleNamespace(aws_request_id='r'))['statusCode'] == 403

    session.assert_not_called()


@pytest.mark.parametrize(
    'challenge,field',
    [('SOFTWARE_TOKEN_MFA', 'SOFTWARE_TOKEN_MFA_CODE'), ('SMS_MFA', 'SMS_MFA_CODE')],
)
def test_login_mfa_and_alias(monkeypatch, challenge, field):
    """Only Cognito receives password/MFA; the caller gets an access token and canonical user."""

    monkeypatch.setattr(upload.sys.stdin, 'isatty', lambda: True)
    prompt = Mock(side_effect=['fixture-password', '123456'])

    monkeypatch.setattr(auth.getpass, 'getpass', prompt)
    client = Mock()
    client.initiate_auth.return_value = {
        'ChallengeName': challenge,
        'Session': 'fixture-session',
        'ChallengeParameters': {'USERNAME': 'alice'},
    }
    client.respond_to_auth_challenge.return_value = {
        'AuthenticationResult': {
            'AccessToken': 'fixture-token',
            'RefreshToken': 'fixture-refresh',
            'ExpiresIn': 300,
        }
    }
    client.get_user.return_value = {'Username': 'alice'}
    assert upload.login(
        {'cognito_client_id': 'client'}, 'alias@example.test', client
    ).credentials() == ('fixture-token', 'alice')
    assert client.initiate_auth.call_args.kwargs['AuthParameters']['PASSWORD'] == 'fixture-password'
    assert client.respond_to_auth_challenge.call_args.kwargs['ChallengeResponses'] == {
        'USERNAME': 'alice',
        field: '123456',
    }

    client.get_user.assert_called_once_with(AccessToken='fixture-token')


def test_noninteractive_login_never_prompts(monkeypatch):
    """Agent/non-terminal execution cannot collect a password from stdin."""

    monkeypatch.setattr(upload.sys.stdin, 'isatty', lambda: False)
    prompt = Mock()

    monkeypatch.setattr(auth.getpass, 'getpass', prompt)
    client = Mock()

    with pytest.raises(auth.LoginRequired):
        upload.login({'cognito_client_id': 'client'}, 'alice', client)

    prompt.assert_not_called()

    client.initiate_auth.assert_not_called()


def test_password_echo_fallback_is_error(monkeypatch):
    """getpass warning becomes a hard failure before login can send a credential."""

    monkeypatch.setattr(upload.sys.stdin, 'isatty', lambda: True)

    def warn(prompt):
        """Simulate getpass discovering an unusable terminal."""

        auth.warnings.warn('fixture', auth.getpass.GetPassWarning)

        return 'must-not-return'

    monkeypatch.setattr(auth.getpass, 'getpass', warn)
    client = Mock()

    with pytest.raises(auth.LoginRequired):
        upload.login({'cognito_client_id': 'client'}, 'alice', client)

    client.initiate_auth.assert_not_called()


@pytest.mark.parametrize(
    'url',
    [
        'http://example.test/documents',
        'https://u:p@example.test/documents',
        'https://example.test/other',
        'https://example.test/documents?x=1',
    ],
)
def test_unsafe_transport_rejected(url):
    """Reject non-HTTPS and ambiguous upload targets before transmission."""

    with pytest.raises(ValueError):
        upload.send_upload(url, {}, 'fixture-token')


def test_redirect_is_closed():
    """A redirect cannot forward the password-independent bearer credential."""
    response = Mock()

    with pytest.raises(ValueError):
        upload.NoRedirect().redirect_request(None, response, 302, '', {}, 'https://other.test')

    response.close.assert_called_once()


def test_upload_route_requires_cognito():
    """The deployable route never permits IAM or anonymous uploads and has no default issuer."""
    root = Path(__file__).resolve().parents[1]
    template = yaml.load((root / 'template.yaml').read_text(), Loader=yaml.BaseLoader)
    route = template['Resources']['UploadRoute']['Properties']
    assert route['AuthorizationType'] == 'JWT'
    assert route['AuthorizerId'] == 'UploadAuthorizer'
    assert route['AuthorizationScopes'] == ['aws.cognito.signin.user.admin']
    authorizer = template['Resources']['UploadAuthorizer']['Properties']
    assert authorizer['IdentitySource'] == ['$request.header.Authorization']
    assert authorizer['JwtConfiguration']['Audience'] == ['CognitoClientId']
    assert 'CognitoUserPoolId' in authorizer['JwtConfiguration']['Issuer']

    for name in ('CognitoUserPoolId', 'CognitoClientId'):
        assert 'Default' not in template['Parameters'][name]


def test_browser_route_has_a_separate_exact_issuer_client_and_invoke_permission():
    """Adding browser uploader trust leaves the existing route and issuer isolated."""
    root = Path(__file__).resolve().parents[1]
    template = yaml.load((root / 'template.yaml').read_text(), Loader=yaml.BaseLoader)
    resources = template['Resources']
    old = resources['UploadRoute']['Properties']
    route = resources['BrowserUploadRoute']['Properties']
    assert old['RouteKey'] == 'POST /documents'
    assert old['AuthorizerId'] == 'UploadAuthorizer'
    assert route['RouteKey'] == 'POST /browser-documents'
    assert route['AuthorizationType'] == 'JWT'
    assert route['AuthorizerId'] == 'BrowserUploadAuthorizer'
    assert route['AuthorizationScopes'] == ['aws.cognito.signin.user.admin']
    config = resources['BrowserUploadAuthorizer']['Properties']['JwtConfiguration']
    assert config['Audience'] == ['BrowserCognitoClientId']
    assert '${BrowserCognitoUserPoolId}' in config['Issuer']
    assert resources['UploadAuthorizer']['Properties']['JwtConfiguration']['Audience'] == [
        'CognitoClientId'
    ]
    for name in ('BrowserCognitoUserPoolId', 'BrowserCognitoClientId'):
        assert 'Default' not in template['Parameters'][name]
        assert 'AllowedPattern' in template['Parameters'][name]
    assert resources['InvokePermission']['Properties']['SourceArn'].endswith('/POST/documents')
    assert resources['BrowserInvokePermission']['Properties']['SourceArn'].endswith(
        '/POST/browser-documents'
    )


def test_template_does_not_own_cloudfront_or_portal_ui():
    """The extracted stack contains no portal distribution or UI resources."""
    template = yaml.load((Path(__file__).resolve().parents[1] / "template.yaml").read_text(), Loader=yaml.BaseLoader)
    assert not any(resource["Type"].startswith("AWS::CloudFront") for resource in template["Resources"].values())


def test_resume_only_uploader_never_prompts_or_posts_when_login_is_missing(monkeypatch, tmp_path):
    """A missing OTP session stops one-file upload before credential prompts or network writes."""
    document = tmp_path / 'otp-upload-verification.md'

    document.write_text('# Upload verification\n')

    monkeypatch.setattr(
        'sys.argv',
        [
            'upload.py',
            '--region',
            'ca-central-1',
            '--client-id',
            'fixtureclient',
            '--username',
            'gopalcbala',
            '--resume-only',
            str(document),
        ],
    )

    monkeypatch.setattr(upload.boto3, 'client', Mock())

    monkeypatch.setattr(upload, 'resume_session', Mock(side_effect=upload.LoginRequired('fixture')))
    login = Mock()
    send = Mock()

    monkeypatch.setattr(upload, 'login', login)

    monkeypatch.setattr(upload, 'send_upload', send)

    monkeypatch.delenv('SSL_CERT_FILE', raising=False)

    monkeypatch.setattr(upload, 'get_cert_path', lambda verify: '/fixture/sdk-ca.pem')

    with pytest.raises(SystemExit) as error:
        upload.main()
    assert error.value.code == 1

    login.assert_not_called()

    send.assert_not_called()
    assert upload.os.environ['SSL_CERT_FILE'] == '/fixture/sdk-ca.pem'
