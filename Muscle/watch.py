import cv2, json, time
import paho.mqtt.client as mqtt
from insightface.app import FaceAnalysis
import numpy as np

app = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider"])
app.prepare(ctx_id=0)
known_embeddings = np.load("known_faces.npy")

mqttc = mqtt.Client()
mqttc.connect("100.100.180.6", 1883)  # your Ubuntu server's Tailscale IP

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