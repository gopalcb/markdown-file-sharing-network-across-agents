"""Dispatch portal and MCP traffic inside one telemetry Lambda process."""

from a2wsgi import WSGIMiddleware
from app import create_app as create_portal_app
from mcp_api.app import create_app as create_mcp_app

MCP_EXACT_PATHS = {
    '/.well-known/oauth-protected-resource',
    '/.well-known/oauth-protected-resource/mcp',
    '/authorize',
    '/token',
    '/register',
    '/revoke',
    '/auth/callback',
    '/consent',
}


class TelemetryApplication:
    """Route MCP-owned paths to ASGI and all other paths to the Flask portal."""

    def __init__(self, portal, mcp) -> None:
        self.portal = WSGIMiddleware(portal)
        self.mcp = mcp

    async def __call__(self, scope, receive, send) -> None:
        """Delegate one request without changing either application's middleware."""

        if scope['type'] == 'lifespan':
            await self.mcp(scope, receive, send)

            return

        path = scope.get('path', '')
        target = (
            self.mcp
            if path in MCP_EXACT_PATHS
            or path.startswith('/.well-known/')
            or path == '/mcp'
            or path.startswith('/mcp/')
            else self.portal
        )
        await target(scope, receive, send)


def create_app(
    portal_values: dict | None = None,
    portal_keys=None,
    mcp_config: dict | None = None,
    mcp_verifier=None,
    mcp_oauth=None,
):
    """Build the single-process telemetry runtime with injectable test dependencies."""
    portal = create_portal_app(auth_values=portal_values, keys=portal_keys)
    mcp = create_mcp_app(config=mcp_config, verifier=mcp_verifier, oauth=mcp_oauth)

    return TelemetryApplication(portal, mcp)
