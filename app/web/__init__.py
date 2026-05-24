"""FastAPI + WebSocket layer that exposes the SessionController to a React UI.

Runs on a separate uvicorn instance from the MCP server; both share one
``SessionController``. See :mod:`app.web.server` and :mod:`app.web.runner`.
"""
