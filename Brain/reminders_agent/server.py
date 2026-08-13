from mcp.server.fastmcp import FastMCP
from apscheduler.schedulers.background import BackgroundScheduler
import paho.mqtt.client as mqtt
import json, datetime

mcp = FastMCP("reminders")
mqttc = mqtt.Client()
mqttc.connect("localhost", 1883)   # broker is on this same machine
scheduler = BackgroundScheduler()
scheduler.start()

reminders_db = []

@mcp.tool()
def add_reminder(text: str, when_iso: str) -> str:
    """Schedule a reminder. when_iso format: 2026-07-25T18:30:00"""
    run_time = datetime.datetime.fromisoformat(when_iso)
    scheduler.add_job(fire_reminder, "date", run_date=run_time, args=[text])
    reminders_db.append({"text": text, "when": when_iso})
    return f"Reminder set for {when_iso}"

@mcp.tool()
def list_reminders() -> list:
    """Return all pending reminders."""
    return reminders_db

def fire_reminder(text):
    mqttc.publish("agent/reminders/due", json.dumps({"text": text}))
    print(f"[fired] {text}")

if __name__ == "__main__":
    mcp.run(transport="stdio")
