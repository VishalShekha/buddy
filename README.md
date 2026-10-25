# Multi-Agent Personal Assistant — Rookie-Friendly Build Guide

### Windows GPU laptop ("muscle") + Ubuntu laptop ("server")

**Simplification that will save you a lot of pain:** don't try to run GPU workloads inside Docker on Windows (that means fighting WSL2 GPU passthrough as a first project). Instead:

- **Windows laptop** runs two things _natively_ (just Python, no Docker): Ollama, and the face-recognition script.
- **Ubuntu laptop** runs _everything else_ in Docker containers, since Docker on Linux is simple and GPU-free here.

They talk to each other over Tailscale, which you already have set up.

---

## Part 0 — What "done" looks like

```
[Windows laptop - muscle]              [Ubuntu laptop - server]
  Ollama (local LLM)      <---Tailscale--->  Mosquitto (message bus)
  Face recognition script                    Reminders agent
                                              Web search agent
                                              Personal assistant + memory
                                              Orchestrator
                                              Gitea (your own GitHub)
                                              ntfy (push notifications)
```

We build it bottom-up, testing each piece before moving to the next. Don't skip the "test it" steps — that's how you catch problems while they're small.

---

## Quick start: how to run this project

Use this when you want to bring the system up locally after the repo is on your machine.

### 1) Start the core infrastructure

On the Ubuntu/server machine:

```bash
cd ~/agent-stack
cd mosquitto && docker compose up -d
cd ../ntfy && docker compose up -d
```

This starts the message broker and push notification service.

### 2) Install Python dependencies

Create and activate a virtual environment, then install the packages used by the agents and API:

```bash
cd ~/agent-stack
python3 -m venv .venv
source .venv/bin/activate
python -m pip install fastapi uvicorn "mcp<2" paho-mqtt requests apscheduler chromadb sentence-transformers
```

If you are running the Windows face-recognition script, install its extra dependencies there too:

```powershell
pip install opencv-python insightface numpy paho-mqtt
```

### 3) Configure web search

The orchestrator starts the MCP agent servers automatically. Set the SerpAPI key
in the same terminal before starting it; this is required by the web-search agent:

```bash
export SERPAPI_API_KEY="your-serpapi-key"
```

Keep the key private and do not commit it to the repository.

### 4) Start the orchestrator API

```bash
cd ~/agent-stack/Brain/orchestrator
uvicorn main:app --host 0.0.0.0 --port 8000
```

The orchestrator starts the assistant, reminders, and web-search MCP servers and
lets Ollama choose and call their tools for each question. Test a memory write and
recall:

```bash
curl -G "http://localhost:8000/ask" --data-urlencode "q=Remember that my favorite color is blue"
curl -G "http://localhost:8000/ask" --data-urlencode "q=What is my favorite color?"
```

For web search, ask naturally, for example:

```bash
curl -G "http://localhost:8000/ask" --data-urlencode "q=Search the web for today's top technology news"
```

### 5) Run the Windows face-recognition worker

On the Windows laptop, update the Tailscale IPs in:

- `Brain/orchestrator/main.py`
- `Muscle/watch.py`

Then run:

```powershell
cd path\to\buddy
python Muscle\watch.py
```

This publishes `agent/facerecog/unknown_detected` events to your Mosquitto broker.

### 6) Optional audio relay

If you want the sound path enabled:

- On the Ubuntu machine run:

```bash
cd ~/agent-stack/sound
python reciever.py
```

- On the Windows machine run:

```powershell
cd path\to\buddy\sound
python sender.py
```

This sends live audio from the Windows machine to the Ubuntu server.

> Important: the hard-coded IP addresses in `Brain/orchestrator/main.py` and `Muscle/watch.py` must match your Tailscale network before the system will work end-to-end.

---

## Part 1 — Set up the Ubuntu server

### 1.1 Update the system

```bash
sudo apt update && sudo apt upgrade -y
```

### 1.2 Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

Log out and back in (or `newgrp docker`) so the group change takes effect. Test:

```bash
docker run hello-world
```

You should see a "Hello from Docker!" message.

### 1.3 Install Docker Compose (bundled with modern Docker, but confirm)

```bash
docker compose version
```

If that fails: `sudo apt install docker-compose-plugin -y`

### 1.4 Install Python and Git

```bash
sudo apt install python3 python3-venv python3-pip git -y
```

### 1.5 Find your Tailscale IP

```bash
tailscale ip -4
```

Write this number down (e.g. `100.101.1.2`). This is **your server's Tailscale IP** — you'll use it everywhere below. Call it `SERVER_IP` from now on.

### 1.6 Make a project folder

```bash
mkdir -p ~/agent-stack && cd ~/agent-stack
```

Everything on the server side lives under here.

---

## Part 2 — Set up the Windows (muscle) laptop

### 2.1 Confirm your GPU driver

Open **NVIDIA Control Panel** or run in PowerShell:

```powershell
nvidia-smi
```

If this shows your GPU and driver version, you're good. If not, install the latest driver from nvidia.com first.

### 2.2 Install Python

Download Python 3.11 from python.org. **During install, check "Add python.exe to PATH."** Confirm in PowerShell:

```powershell
python --version
```

### 2.3 Install Ollama (runs your local LLM)

Download the Windows installer from ollama.com. After installing, open PowerShell:

```powershell
ollama pull qwen2.5:14b
ollama run qwen2.5:14b
```

Type a test message, confirm you get a response, then type `/bye` to exit. Ollama automatically uses your GPU — no extra config needed.

### 2.4 Find your Tailscale IP

Open Tailscale on Windows, or in PowerShell:

```powershell
tailscale ip -4
```

Write this down — call it `MUSCLE_IP`.

### 2.5 Test Ollama is reachable from the server

Ollama needs to listen on your network, not just `localhost`. Set an environment variable (System Properties → Environment Variables → New):

```
Name: OLLAMA_HOST
Value: 0.0.0.0
```

Restart Ollama (it runs as a background service — restart your machine or restart the "Ollama" service in Task Manager). Then, **from the Ubuntu server**, test:

```bash
curl http://MUSCLE_IP:11434/api/tags
```

(replace `MUSCLE_IP` with the real number). You should get back a JSON list of your models. **If this fails, stop and fix it before continuing** — everything downstream depends on this connection working.

### 2.6 Windows Firewall

If the curl test above times out, Windows Firewall is probably blocking it. Allow port 11434:

```powershell
New-NetFirewallRule -DisplayName "Ollama" -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow
```

---

## Part 3 — Message bus (Mosquitto), on the server

This is the "nervous system" — every agent publishes events here, and the orchestrator listens.

```bash
mkdir -p ~/agent-stack/mosquitto/config
cd ~/agent-stack/mosquitto
```

Create `config/mosquitto.conf`:

```
listener 1883 0.0.0.0
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
```

Create `docker-compose.yml` in `~/agent-stack/mosquitto/`:

```yaml
services:
  mosquitto:
    image: eclipse-mosquitto:2
    ports:
      - "1883:1883"
    volumes:
      - ./config:/mosquitto/config
      - ./data:/mosquitto/data
    restart: unless-stopped
```

Run it:

```bash
docker compose up -d
```

**Test it works** — open two terminals on the server:

```bash
# terminal 1: install a test client and listen
sudo apt install mosquitto-clients -y
mosquitto_sub -h localhost -t 'test/topic' -v

# terminal 2: publish a message
mosquitto_pub -h localhost -t 'test/topic' -m "hello world"
```

You should see `hello world` appear in terminal 1. That confirms the broker works. Delete the test topic mentally — it's not persisted anywhere important.

---

## Part 4 — Your first agent: Reminders

We build this one first because it's the smallest complete loop: schedule → time passes → event fires → message published.

```bash
mkdir -p ~/agent-stack/reminders_agent && cd ~/agent-stack/reminders_agent
python3 -m venv venv
source venv/bin/activate
pip install mcp apscheduler paho-mqtt
```

Create `server.py`:

```python
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
```

**Test it without any orchestrator yet**, using plain Python so you understand what's happening before adding MCP/LLM complexity:

```python
# quick_test.py (separate file, same folder)
import server
print(server.add_reminder("test reminder", "2026-07-27T15:05:00"))  # set 1-2 min in the future
```

Run it, then watch terminal 1 (your `mosquitto_sub` from Part 3) — a message should appear on `agent/reminders/due` at the scheduled time. **This is your first real milestone.**

---

## Part 5 — Web search agent

```bash
cd ~/agent-stack
mkdir search_agent && cd search_agent
python3 -m venv venv && source venv/bin/activate
pip install mcp requests
```

Sign up for a free **Brave Search API** key (or SerpAPI) — don't scrape Google, it's against their terms and will get your IP blocked.

```bash
export BRAVE_API_KEY="your-key-here"
```

(Add this to `~/.bashrc` so it persists across reboots.)

`server.py`:

```python
from mcp.server.fastmcp import FastMCP
import requests, os

mcp = FastMCP("web_search")
API_KEY = os.environ["BRAVE_API_KEY"]

@mcp.tool()
def web_search(query: str) -> list:
    """Search the web and return top results."""
    r = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": API_KEY},
        params={"q": query, "count": 5},
    )
    results = r.json().get("web", {}).get("results", [])
    return [{"title": x["title"], "url": x["url"], "snippet": x.get("description", "")} for x in results]

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Test the same way — a quick script that imports `server` and calls `web_search("test query")` directly, confirming you get real results back before wiring it into anything bigger.

---

## Part 6 — Personal assistant agent (with memory)

```bash
cd ~/agent-stack
mkdir assistant_agent && cd assistant_agent
python3 -m venv venv && source venv/bin/activate
pip install mcp chromadb sentence-transformers
```

`memory.py`:

```python
import chromadb
from sentence_transformers import SentenceTransformer

client = chromadb.PersistentClient(path="./chroma_data")
collection = client.get_or_create_collection("personal_memory")
embedder = SentenceTransformer("all-MiniLM-L6-v2")  # small, runs fine on CPU

def add_memory(text: str, doc_id: str):
    emb = embedder.encode([text])[0].tolist()
    collection.add(ids=[doc_id], embeddings=[emb], documents=[text])

def query_memory(text: str, n: int = 5):
    emb = embedder.encode([text])[0].tolist()
    res = collection.query(query_embeddings=[emb], n_results=n)
    return res["documents"][0]
```

`server.py`:

```python
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
```

Test: store a couple of facts (`remember("My dentist appointment is Aug 3rd")`), then call `recall("when is my dentist appointment")` and confirm it comes back. This is your RAG pipeline — small, but real. Calendar integration (Google Calendar API) can be added later as another `@mcp.tool()` once this baseline works.

---

## Part 7 — The orchestrator (server)

This is the piece that ties everything together: it starts the assistant,
reminders, and web-search MCP servers over stdio, discovers their tools, and
provides those tools to Ollama on the Windows muscle laptop. The model can then
choose and call tools—such as remembering a fact, recalling it, searching the
web, or scheduling a reminder—before composing its answer. It also listens on
MQTT for system-triggered events (like an unknown face).

The runnable implementation is `Brain/orchestrator/main.py`. Install the
dependencies from the quick-start section, set `SERPAPI_API_KEY` as shown above,
then start it:

```bash
cd ~/agent-stack/Brain/orchestrator
uvicorn main:app --host 0.0.0.0 --port 8000
```

Test MCP tool usage from any machine on your tailnet:

```bash
curl -G "http://SERVER_IP:8000/ask" --data-urlencode "q=Remember that my favorite color is blue"
curl -G "http://SERVER_IP:8000/ask" --data-urlencode "q=What is my favorite color?"
```

The first request should invoke the assistant MCP server's `remember` tool; the
second should invoke `recall`. The JSON response includes the answer and a
`tools_used` list so you can confirm which MCP tools the model called.

---

## Part 8 — Face recognition (Windows laptop, native — uses the GPU)

On the Windows laptop:

```powershell
pip install opencv-python insightface onnxruntime-gpu paho-mqtt numpy
```

First, build a reference file of known faces. `enroll.py`:

```python
import cv2
from insightface.app import FaceAnalysis
import numpy as np

app = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider"])
app.prepare(ctx_id=0)

# put a few clear photos of yourself in a folder called reference_photos/
import glob
embeddings = []
for path in glob.glob("reference_photos/*.jpg"):
    img = cv2.imread(path)
    faces = app.get(img)
    if faces:
        embeddings.append(faces[0].normed_embedding)

np.save("known_faces.npy", np.array(embeddings))
print(f"Enrolled {len(embeddings)} reference faces.")
```

Run this once after dropping 3-5 photos of yourself into `reference_photos/`.

`watch.py`:

```python
import cv2, json, time
import paho.mqtt.client as mqtt
from insightface.app import FaceAnalysis
import numpy as np

app = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider"])
app.prepare(ctx_id=0)
known_embeddings = np.load("known_faces.npy")

mqttc = mqtt.Client()
mqttc.connect("SERVER_IP", 1883)  # your Ubuntu server's Tailscale IP

cap = cv2.VideoCapture(0)
THRESHOLD = 0.35

while True:
    ok, frame = cap.read()
    if not ok:
        continue
    faces = app.get(frame)
    for f in faces:
        similarities = known_embeddings @ f.normed_embedding
        if similarities.max() < THRESHOLD:
            mqttc.publish("agent/facerecog/unknown_detected", json.dumps({"time": time.time()}))
            print("Unknown face detected — event published")
    time.sleep(1)
```

Run `python watch.py`, then sit in front of the webcam (known face → nothing happens) and then have someone else step in front of it, or hold up a photo of a stranger (unknown face → check terminal 1's `mosquitto_sub` on the server for the event).

---

## Part 9 — Notifications (so "do some things" actually does something)

Simplest option: **ntfy**, self-hosted on the server.

`~/agent-stack/ntfy/docker-compose.yml`:

```yaml
services:
  ntfy:
    image: binwiederhier/ntfy
    command: serve
    ports:
      - "8080:80"
    restart: unless-stopped
```

```bash
docker compose up -d
```

Install the **ntfy** app on your phone, subscribe to a topic (e.g. `myhouse-alerts`) pointing at `http://SERVER_IP:8080`. Then in the orchestrator's `on_mqtt_message`:

```python
import requests
def on_mqtt_message(client, userdata, msg):
    if msg.topic == "agent/facerecog/unknown_detected":
        requests.post("http://localhost:8080/myhouse-alerts", data="Unknown face detected!")
    if msg.topic == "agent/reminders/due":
        payload = msg.payload.decode()
        requests.post("http://localhost:8080/myhouse-alerts", data=f"Reminder: {payload}")
```

Test end to end: trigger an unknown face on the Windows laptop → confirm a push notification arrives on your phone within a couple seconds.

---

## Part 10 — Code management (Gitea, on the server)

```bash
cd ~/agent-stack
mkdir gitea && cd gitea
```

`docker-compose.yml`:

```yaml
services:
  gitea:
    image: gitea/gitea:latest
    ports:
      - "3000:3000"
      - "222:22"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

```bash
docker compose up -d
```

Visit `http://SERVER_IP:3000` in a browser, run through the first-time setup wizard, create an account, create a repo. Push your `agent-stack` folder there:

```bash
cd ~/agent-stack
git init
git remote add origin http://SERVER_IP:3000/yourname/agent-stack.git
git add .
git commit -m "initial commit"
git push -u origin main
```

Later, mirror this same repo to GitHub (Gitea has a built-in "Mirror" push option in repo settings) so it's visible publicly on your resume/portfolio without exposing your home server.

Deployment (CloudFront/S3, dashboards, etc.) — parking that for later as you said. Everything above already gives you a genuinely complete, demoable system without it.

---

## Testing checklist

- [ ] `mosquitto_pub`/`sub` test works on the server
- [ ] Reminder set → fires → message appears on `agent/reminders/due`
- [ ] `curl SERVER_IP:8000/ask` returns a real LLM answer
- [ ] Unknown face on webcam → event published → ntfy notification arrives on phone
- [ ] Known face → nothing happens (no false positives)
- [ ] `assistant_agent`: store a fact, recall it successfully
- [ ] Restarting the Ubuntu laptop brings Docker services back automatically (`restart: unless-stopped` handles this)
- [ ] Everything is committed and pushed to Gitea

---

## Resume bullets (once built)

- Designed and built a distributed, event-driven multi-agent system across two networked machines (Tailscale VPN), using MQTT pub/sub to coordinate real-time face recognition, scheduling, and search agents
- Implemented agents as MCP (Model Context Protocol) servers, orchestrated by a locally-hosted LLM (Ollama) running on dedicated GPU hardware
- Built a RAG pipeline (ChromaDB + sentence-transformer embeddings) giving the assistant long-term memory over personal notes
- Built a GPU-accelerated face recognition service (OpenCV + InsightFace) that triggers automated notifications via an event bus
- Set up self-hosted Git (Gitea) for version control across the project
