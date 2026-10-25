import asyncio
import json
import os
import re
import sys
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
import paho.mqtt.client as mqtt
import requests

import logging

logger = logging.getLogger(__name__)

BRAIN_DIR = Path(__file__).resolve().parents[1]
MCP_SERVERS = {
    "assistant": BRAIN_DIR / "assistant_agent",
    "reminders": BRAIN_DIR / "reminders_agent",
    "search": BRAIN_DIR / "search_agent",
}
MUSCLE_IP = os.getenv("MUSCLE_IP", "100.125.233.104")
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
OLLAMA_URL = f"http://{MUSCLE_IP}:11434/api/chat"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        tools = []
        tool_sessions = {}

        for server_name, server_dir in MCP_SERVERS.items():
            params = StdioServerParameters(
                command=sys.executable,
                args=["server.py"],
                cwd=str(server_dir),
            )
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(params)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            server_tools = await session.list_tools()
            for tool in server_tools.tools:
                ollama_name = f"{server_name}_{tool.name}"
                tools.append({
                    "type": "function",
                    "function": {
                        "name": ollama_name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema,
                    },
                })
                tool_sessions[ollama_name] = (session, tool.name)

        app.state.mcp_tools = tools
        app.state.tool_sessions = tool_sessions
        app.state.mcp_lock = asyncio.Lock()
        yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET"],
    allow_headers=["*"],
)


def ollama_chat(messages, tools):
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "stream": False,
    }

    response = requests.post(
        OLLAMA_URL,
        json=payload,
        timeout=180,
    )

    if not response.ok:
        print(f"Ollama HTTP status: {response.status_code}")
        print(f"Ollama response body: {response.text}")
        print(f"Model: {MODEL}")
        print(f"Number of tools: {len(tools)}")

    response.raise_for_status()
    return response.json()["message"]



def tool_result_text(result):
    if result.isError:
        details = "\n".join(
            item.text for item in result.content if hasattr(item, "text")
        )
        raise RuntimeError(f"MCP tool failed: {details or 'no error details'}")
    if result.structuredContent is not None:
        return json.dumps(result.structuredContent, ensure_ascii=False)
    return "\n".join(
        item.text for item in result.content if hasattr(item, "text")
    )


def requested_memory_fact(q):
    match = re.match(
        r"^\s*(?:please\s+)?(?:remember|save|store|note)\s+(?:that\s+)?(.+?)\s*[.!?]*\s*$",
        q,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


async def call_memory_tool(tool_name, arguments, tool_sessions):
    entry = tool_sessions.get(f"assistant_{tool_name}")
    if entry is None:
        raise RuntimeError(f"The assistant MCP server does not provide {tool_name}")
    session, mcp_tool_name = entry
    result = await session.call_tool(mcp_tool_name, arguments=arguments)
    return tool_result_text(result)


async def answer_with_tools(q, tools, tool_sessions, saved_memories):
    tools_used = ["assistant_recall"]
    messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful personal assistant. Use the available MCP tools "
                "whenever they can help answer or fulfill the user's request. "
                "Call the appropriate tool before claiming an action is complete. "
                "Use the saved memories retrieved for this request when relevant. "
                "If they do not contain the answer, say you do not know rather than "
                "inventing personal information.\n\n"
                f"Saved memories retrieved via MCP:\n{saved_memories}"
            ),
        },
        {"role": "user", "content": q},
    ]

    for _ in range(6):
        message = await asyncio.to_thread(ollama_chat, messages, tools)
        messages.append(message)
        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            return message.get("content", ""), tools_used

        for call in tool_calls:
            function = call["function"]
            tool_name = function["name"]
            session, mcp_tool_name = tool_sessions[tool_name]
            tools_used.append(tool_name)
            result = await session.call_tool(
                mcp_tool_name, arguments=function.get("arguments", {})
            )
            messages.append({
                "role": "tool",
                "tool_name": tool_name,
                "content": tool_result_text(result),
            })

    raise RuntimeError("The model exceeded the MCP tool-call limit")


@app.get("/ask")
async def ask(q: str):
    try:
        async with app.state.mcp_lock:
            fact = requested_memory_fact(q)
            if fact:
                await call_memory_tool(
                    "remember", {"fact": fact}, app.state.tool_sessions
                )
                return {
                    "answer": f"I'll remember that: {fact}",
                    "tools_used": ["assistant_remember"],
                }

            saved_memories = await call_memory_tool(
                "recall", {"question": q}, app.state.tool_sessions
            )
            answer, tools_used = await answer_with_tools(
                q, app.state.mcp_tools, app.state.tool_sessions, saved_memories
            )
        return {"answer": answer, "tools_used": tools_used}
    except (McpError, requests.RequestException, KeyError, RuntimeError) as exc:
        logger.exception("Failed to process /ask request: %s", q)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def on_mqtt_message(client, userdata, msg):
    print(f"[event] {msg.topic}: {msg.payload.decode()}")
    if msg.topic == "agent/facerecog/unknown_detected":
        requests.post("http://localhost:8080/myhouse-alerts", data="Unknown face detected!")
    if msg.topic == "agent/reminders/due":
        requests.post("http://localhost:8080/myhouse-alerts", data=f"Reminder: {payload}")

def start_mqtt_listener():
    c = mqtt.Client()
    c.connect("localhost", 1883)
    c.subscribe("agent/#")
    c.on_message = on_mqtt_message
    c.loop_forever()

threading.Thread(target=start_mqtt_listener, daemon=True).start()
