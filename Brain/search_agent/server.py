from mcp.server.fastmcp import FastMCP
import requests
import os

mcp = FastMCP("web_search")

API_KEY = os.environ["SERPAPI_API_KEY"]

@mcp.tool()
def web_search(query: str) -> list:
    """Search the web and return the top 5 results."""

    r = requests.get(
        "https://serpapi.com/search.json",
        params={
            "engine": "google",
            "q": query,
            "api_key": API_KEY,
            "num": 5,
        },
    )

    r.raise_for_status()

    results = r.json().get("organic_results", [])

    return [
        {
            "title": result.get("title", ""),
            "url": result.get("link", ""),
            "snippet": result.get("snippet", ""),
        }
        for result in results
    ]

if __name__ == "__main__":
    mcp.run(transport="stdio")
