from mcp.server.fastmcp import FastMCP
from memory import add_memory, query_memory
import uuid

mcp = FastMCP("assistant")

@mcp.tool()
def remember(fact: str) -> str:
    """Store a fact/note for later recall."""
    add_memory(fact, str(uuid.uuid4()))
    return "Stored."

@mcp.tool()
def recall(question: str) -> list:
    """Retrieve relevant stored facts/notes for a question."""
    return query_memory(question)

if __name__ == "__main__":
    mcp.run(transport="stdio")
