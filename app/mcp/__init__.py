"""MCP server: lets external AI agents read and write case data.

Mounted at ``/mcp`` (see ``app.mcp.server``); authentication is the bearer
token handled by ``AuthGateMiddleware``. Everything here acts as the token's
owner, narrowed to the token's scope (``principal.McpPrincipal``).
"""
