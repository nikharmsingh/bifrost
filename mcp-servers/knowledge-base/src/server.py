from fastmcp import FastMCP
from core.logging import get_logger

logger = get_logger(__name__)
mcp = FastMCP("knowledge-base")


@mcp.tool()
def search_knowledge_base(query: str) -> str:
    """Search the knowledge base for relevant information."""
    logger.info("Searching knowledge base: %s", query)
    # TODO: implement vector search / document retrieval
    return f"No results found for: {query}"


@mcp.resource("kb://status")
def status() -> str:
    return "knowledge-base MCP server is running"


if __name__ == "__main__":
    mcp.run()
