import asyncio
import json
import os
import sys
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
import paho.mqtt.client as mqtt
import requests

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


def ollama_chat(messages, tools):
    response = requests.post(
        OLLAMA_URL,
        json={"model": MODEL, "messages": messages, "tools": tools, "stream": False},
        timeout=180,
    )
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


async def answer_with_tools(q, tools, tool_sessions):
    tools_used = []
    messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful personal assistant. Use the available MCP tools "
                "whenever they can help answer or fulfill the user's request. "
                "Call the appropriate tool before claiming an action is complete."
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
            answer, tools_used = await answer_with_tools(
                q, app.state.mcp_tools, app.state.tool_sessions
            )
        return {"answer": answer, "tools_used": tools_used}
    except (McpError, requests.RequestException, KeyError, RuntimeError) as exc:
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
