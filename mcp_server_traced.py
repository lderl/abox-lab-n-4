"""
MCP Server з Phoenix Tracing
=============================
Власний MCP сервер інструментований OpenInference + Phoenix.

Запуск:
  export PHOENIX_COLLECTOR_ENDPOINT="http://localhost:4317"
  export PHOENIX_CLIENT_HEADERS="authorization=Bearer <api_key>"
  python mcp_server_traced.py
"""

from mcp.server.fastmcp import FastMCP
from phoenix.otel import register

# Підключаємось до Phoenix — auto_instrument=True підхоплює всі MCP spans
tracer_provider = register(
    project_name="mcp-fetch-server",
    auto_instrument=True,
)
tracer = tracer_provider.get_tracer("mcp-fetch-server")

mcp = FastMCP("fetch-server")


@mcp.tool()
def fetch_url(url: str, max_length: int = 2000) -> str:
    """Fetches content from a URL and returns it as text."""
    import urllib.request

    with tracer.start_as_current_span("fetch_url") as span:
        span.set_attribute("url", url)
        span.set_attribute("max_length", max_length)

        with urllib.request.urlopen(url, timeout=10) as r:
            content = r.read().decode("utf-8", errors="replace")[:max_length]

        span.set_attribute("content_length", len(content))
        return content


@mcp.tool()
def echo(message: str) -> str:
    """Echoes back the message — для тесту трейсингу."""
    with tracer.start_as_current_span("echo") as span:
        span.set_attribute("message", message)
        return f"Echo: {message}"


if __name__ == "__main__":
    print("[MCP] Сервер запущено. Traces → Phoenix")
    mcp.run()