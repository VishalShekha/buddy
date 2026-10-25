from mcp.server.fastmcp import FastMCP
import requests
import os

mcp = FastMCP("web_search")

API_KEY = "55d27ca6532a2f6d17e2ee78b8e1bcb500b0d498791365ff9d29fbe20bd55ba2"

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
