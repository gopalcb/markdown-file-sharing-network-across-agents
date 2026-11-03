"""Provision the one Work Organizer Lambda, HTTP API and private UI bucket."""

from pathlib import Path

from aws_cdk import (
    Arn,
    ArnComponents,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_apigatewayv2 as apigateway,
)
from aws_cdk import (
    aws_ecr_assets as ecr_assets,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_s3 as s3,
)
from constructs import Construct

ACCOUNT = '123456789012'
REGION = 'ca-central-1'
PORTAL_ORIGIN = 'https://portal.example.com'
PORTAL_DISTRIBUTION = 'E1111111111111'
PORTAL_ISSUER = 'https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_Fixture123'
PORTAL_CLIENT = 'fixtureportalclient'
UPLOAD_CLIENT = 'fixturenativeclient'
USERNAME_ISSUER = 'https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_Fixture456'
USERNAME_UPLOAD_CLIENT = 'fixtureuploadclient'
COGNITO_DOMAIN = 'https://fixtureportal.auth.ca-central-1.amazoncognito.com'
MCP_PUBLIC_BASE = 'https://mcp.example.com'
MCP_ISSUER = 'https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_Fixture456'
MCP_CLIENT = 'fixturemcpclient'
MCP_COGNITO_DOMAIN = 'https://softrobots-usernames-123456789012.auth.ca-central-1.amazoncognito.com'
MCP_OWNER_SUB = '11111111-1111-4111-8111-111111111111'
MCP_OAUTH_TABLE = 'markdown-mcp-service-oauth'
MCP_OAUTH_SECRET = 'markdown-mcp-service-oauth-key'

READ_WRITE_TABLES = [
    'softrobots-document-api-user-files',
    'softrobots-projects',
    'softrobots-document-api-local-deletions',
    'softrobots-native-device-pending',
    'softrobots-native-device-approvals',
    'softrobots-native-device-nonces',
    'softrobots-auth-sessions',
    'softrobots-browser-sessions',
]
QUERY_TABLES = ['softrobots-document-api-user-files']
SCAN_TABLES = [
    'softrobots-document-api-user-files',
    'softrobots-projects',
    'markdown-sharing-example-analytics',
]


class TelemetryStack(Stack):
    """Own one application Lambda plus its API and static UI storage."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        """Create the replacement runtime without modifying retained data resources."""

        super().__init__(scope, construct_id, **kwargs)

        ui_bucket = s3.Bucket(
            self,
            'UiBucket',
            bucket_name=f'softrobots-telemetry-ui-{ACCOUNT}-{REGION}',
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        ui_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid='AllowCanonicalCloudFrontRead',
                actions=['s3:GetObject'],
                resources=[ui_bucket.arn_for_objects('*')],
                principals=[iam.ServicePrincipal('cloudfront.amazonaws.com')],
                conditions={
                    'StringEquals': {
                        'AWS:SourceArn': f'arn:aws:cloudfront::{ACCOUNT}:distribution/{PORTAL_DISTRIBUTION}'
                    }
                },
            )
        )

        log_group = logs.LogGroup(
            self,
            'TelemetryLogs',
            log_group_name='/aws/lambda/telemetry-lambda',
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.RETAIN,
        )
        runtime_path = Path(__file__).resolve().parents[2] / 'telemetry-api'
        function = lambda_.DockerImageFunction(
            self,
            'TelemetryFunction',
            function_name='telemetry-lambda',
            code=lambda_.DockerImageCode.from_image_asset(
                str(runtime_path),
                file='Dockerfile.lambda',
                platform=ecr_assets.Platform.LINUX_AMD64,
            ),
            architecture=lambda_.Architecture.X86_64,
            memory_size=512,
            timeout=Duration.seconds(28),
            reserved_concurrent_executions=10,
            log_group=log_group,
            environment={
                'COGNITO_ISSUER': PORTAL_ISSUER,
                'COGNITO_CLIENT_ID': PORTAL_CLIENT,
                'COGNITO_GROUP': 'portal-users',
                'NATIVE_UPLOAD_ISSUER': PORTAL_ISSUER,
                'NATIVE_UPLOAD_CLIENT_ID': UPLOAD_CLIENT,
                'BROWSER_UPLOAD_ISSUER': USERNAME_ISSUER,
                'BROWSER_UPLOAD_CLIENT_ID': USERNAME_UPLOAD_CLIENT,
                'PORTAL_ORIGIN': PORTAL_ORIGIN,
                'DOCUMENTS_BUCKET': f'agent-work-history--{ACCOUNT}-{REGION}',
                'LEGACY_DOCUMENTS_BUCKET': f'softrobots-document-api-{ACCOUNT}-{REGION}',
                'USER_FILES_TABLE': 'softrobots-document-api-user-files',
                'PROJECTS_TABLE': 'softrobots-projects',
                'DELETIONS_TABLE': 'softrobots-document-api-local-deletions',
                'MCP_ENABLED': 'true',
                'PUBLIC_BASE_URL': MCP_PUBLIC_BASE,
                'MCP_RESOURCE': MCP_PUBLIC_BASE + '/mcp/',
                'MCP_WRITE_SCOPE': 'markdown-api/write',
                'MCP_ALLOWED_SUB': MCP_OWNER_SUB,
                'MCP_COGNITO_ISSUER': MCP_ISSUER,
                'MCP_COGNITO_CLIENT_ID': MCP_CLIENT,
                'MCP_COGNITO_DOMAIN': MCP_COGNITO_DOMAIN,
                'OAUTH_STATE_TABLE': MCP_OAUTH_TABLE,
                'OAUTH_SECRET_ARN': MCP_OAUTH_SECRET,
                'S3_BUCKET': f'agent-work-history--{ACCOUNT}-{REGION}',
            },
        )

        table_arns = {
            name: Arn.format(
                ArnComponents(service='dynamodb', resource='table', resource_name=name),
                self,
            )
            for name in {*READ_WRITE_TABLES, *QUERY_TABLES, *SCAN_TABLES}
        }

        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
                resources=[table_arns[name] for name in READ_WRITE_TABLES],
            )
        )

        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=['dynamodb:Query'],
                resources=[table_arns[name] for name in QUERY_TABLES],
            )
        )

        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=['dynamodb:Scan'],
                resources=[table_arns[name] for name in SCAN_TABLES],
            )
        )
        bucket_arns = [
            f'arn:aws:s3:::agent-work-history--{ACCOUNT}-{REGION}',
            f'arn:aws:s3:::softrobots-document-api-{ACCOUNT}-{REGION}',
        ]

        function.add_to_role_policy(
            iam.PolicyStatement(actions=['s3:ListBucket'], resources=bucket_arns)
        )

        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=['s3:GetObject', 's3:GetObjectVersion'],
                resources=[f'{bucket_arn}/*' for bucket_arn in bucket_arns],
            )
        )

        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=['s3:PutObject'],
                resources=[f'{bucket_arns[0]}/*'],
            )
        )

        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:DeleteItem'],
                resources=[
                    Arn.format(
                        ArnComponents(
                            service='dynamodb',
                            resource='table',
                            resource_name=MCP_OAUTH_TABLE,
                        ),
                        self,
                    )
                ],
            )
        )

        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=['secretsmanager:GetSecretValue'],
                resources=[
                    f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{MCP_OAUTH_SECRET}-*'
                ],
            )
        )

        api = apigateway.CfnApi(
            self,
            'TelemetryApi',
            name='softrobots-telemetry',
            protocol_type='HTTP',
        )
        api_logs = logs.LogGroup(
            self,
            'ApiLogs',
            log_group_name='/softrobots/telemetry-http-api',
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.RETAIN,
        )
        integration = apigateway.CfnIntegration(
            self,
            'TelemetryIntegration',
            api_id=api.ref,
            integration_type='AWS_PROXY',
            integration_uri=function.function_arn,
            payload_format_version='2.0',
            timeout_in_millis=29000,
        )
        family_authorizer = apigateway.CfnAuthorizer(
            self,
            'FamilyUploadAuthorizer',
            api_id=api.ref,
            authorizer_type='JWT',
            identity_source=['$request.header.Authorization'],
            name='cognito-family-upload',
            jwt_configuration=apigateway.CfnAuthorizer.JWTConfigurationProperty(
                issuer=PORTAL_ISSUER,
                audience=[UPLOAD_CLIENT],
            ),
        )
        username_authorizer = apigateway.CfnAuthorizer(
            self,
            'UsernameUploadAuthorizer',
            api_id=api.ref,
            authorizer_type='JWT',
            identity_source=['$request.header.Authorization'],
            name='cognito-username-upload',
            jwt_configuration=apigateway.CfnAuthorizer.JWTConfigurationProperty(
                issuer=USERNAME_ISSUER,
                audience=[USERNAME_UPLOAD_CLIENT],
            ),
        )
        route_target = f'integrations/{integration.ref}'

        function.add_environment('MCP_ORIGIN_HOST', f'{api.ref}.execute-api.{REGION}.amazonaws.com')

        for route_id, route_key in [
            ('ApiRootRoute', 'ANY /api'),
            ('ApiProxyRoute', 'ANY /api/{proxy+}'),
            ('HealthRoute', 'GET /health'),
            ('McpRootRoute', 'ANY /mcp'),
            ('McpProxyRoute', 'ANY /mcp/{proxy+}'),
            ('McpWellKnownProxyRoute', 'ANY /.well-known/{proxy+}'),
            ('McpMetadataRoute', 'GET /.well-known/oauth-protected-resource'),
            ('McpPathMetadataRoute', 'GET /.well-known/oauth-protected-resource/mcp'),
            ('McpAuthorizeRoute', 'ANY /authorize'),
            ('McpTokenRoute', 'ANY /token'),
            ('McpRegisterRoute', 'ANY /register'),
            ('McpRevokeRoute', 'ANY /revoke'),
            ('McpCallbackRoute', 'ANY /auth/callback'),
            ('McpConsentRoute', 'ANY /consent'),
        ]:
            apigateway.CfnRoute(
                self,
                route_id,
                api_id=api.ref,
                route_key=route_key,
                target=route_target,
            )

        for route_id, route_key, authorizer in [
            ('NativeUploadRoute', 'POST /documents', family_authorizer),
            ('UsernameUploadRoute', 'POST /browser-documents', username_authorizer),
        ]:
            apigateway.CfnRoute(
                self,
                route_id,
                api_id=api.ref,
                route_key=route_key,
                authorization_type='JWT',
                authorizer_id=authorizer.ref,
                authorization_scopes=['aws.cognito.signin.user.admin'],
                target=route_target,
            )

        apigateway.CfnStage(
            self,
            'DefaultStage',
            api_id=api.ref,
            stage_name='$default',
            auto_deploy=True,
            access_log_settings=apigateway.CfnStage.AccessLogSettingsProperty(
                destination_arn=api_logs.log_group_arn,
                format=(
                    '{"requestId":"$context.requestId","status":"$context.status",'
                    '"responseLength":"$context.responseLength",'
                    '"integrationLatency":"$context.integrationLatency"}'
                ),
            ),
            default_route_settings=apigateway.CfnStage.RouteSettingsProperty(
                throttling_burst_limit=20,
                throttling_rate_limit=10,
            ),
        )

        function.add_permission(
            'ApiGatewayInvoke',
            principal=iam.ServicePrincipal('apigateway.amazonaws.com'),
            source_account=ACCOUNT,
            source_arn=f'arn:aws:execute-api:{REGION}:{ACCOUNT}:{api.ref}/*',
        )

        self.ui_bucket = ui_bucket
        self.api_host = f'{api.ref}.execute-api.{REGION}.amazonaws.com'
        self.function = function

        CfnOutput(self, 'UiBucketName', value=ui_bucket.bucket_name)

        CfnOutput(self, 'UiBucketDomain', value=ui_bucket.bucket_regional_domain_name)

        CfnOutput(self, 'ApiHost', value=self.api_host)

        CfnOutput(self, 'ApiHealthUrl', value=f'https://{self.api_host}/api/health')

        CfnOutput(self, 'FunctionName', value=function.function_name)

        CfnOutput(self, 'PortalIssuer', value=PORTAL_ISSUER)

        CfnOutput(self, 'PortalClientId', value=PORTAL_CLIENT)

        CfnOutput(self, 'CognitoDomain', value=COGNITO_DOMAIN)
