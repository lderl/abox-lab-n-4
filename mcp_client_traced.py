"""
MCP Client з Phoenix Tracing
==============================
Клієнт який викликає наш MCP сервер — теж інструментований,
щоб traces client→server злились в єдиний trace в Phoenix.

Запуск (в окремому терміналі після запуску сервера):
  export PHOENIX_COLLECTOR_ENDPOINT="http://localhost:4317"
  export PHOENIX_CLIENT_HEADERS="authorization=Bearer <api_key>"
  python mcp_client_traced.py
"""

import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openinference.instrumentation.mcp import MCPInstrumentor
from phoenix.otel import register

# Підключаємось до Phoenix
tracer_provider = register(
    project_name="mcp-fetch-server",
    auto_instrument=True,
)

# Інструментуємо MCP клієнт — пропагує trace context до сервера
MCPInstrumentor().instrument(tracer_provider=tracer_provider)

tracer = tracer_provider.get_tracer("mcp-client")


async def main():
    server_params = StdioServerParameters(
        command="python",
        args=["mcp_server_traced.py"],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("[Client] Доступні tools:")
            tools = await session.list_tools()
            for tool in tools.tools:
                print(f"  - {tool.name}: {tool.description}")

            # Викликаємо echo — простий тест
            with tracer.start_as_current_span("client.echo"):
                result = await session.call_tool("echo", {"message": "Hello Phoenix!"})
                print(f"\n[Client] echo → {result.content[0].text}")

            # Викликаємо fetch_url — реальна робота
            with tracer.start_as_current_span("client.fetch_url"):
                result = await session.call_tool(
                    "fetch_url",
                    {"url": "https://example.com", "max_length": 500}
                )
                print(f"\n[Client] fetch_url → {result.content[0].text[:200]}...")

            print("\n[Client] Готово! Відкрий Phoenix UI → Tracing → mcp-fetch-server")


if __name__ == "__main__":
    asyncio.run(main())