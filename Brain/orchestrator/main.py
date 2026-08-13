from fastapi import FastAPI
import paho.mqtt.client as mqtt
import requests, threading

app = FastAPI()
MUSCLE_IP = "100.125.233.104"  # replace with your Windows laptop's Tailscale IP

def call_llm(prompt: str) -> str:
    r = requests.post(
        f"http://{MUSCLE_IP}:11434/api/generate",
        json={"model": "qwen2.5:14b", "prompt": prompt, "stream": False},
    )
    data = r.json()

    print(data)

    return r.json()["response"]

@app.get("/ask")
def ask(q: str):
    answer = call_llm(q)
    return {"answer": answer}

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
