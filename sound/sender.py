import socket
import struct
import soundcard as sc
import numpy as np

# =========================
# CONFIGURATION
# =========================

UBUNTU_IP = "172.20.10.2"
PORT = 5000

SAMPLE_RATE = 48000
CHANNELS = 2

# 10 ms at 48 kHz
BLOCK_SAMPLES = 32

# =========================

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)

# Get the Windows default speaker
speaker = sc.default_speaker()

# Create a loopback microphone corresponding to that speaker
loopback = sc.get_microphone(
    id=str(speaker.name),
    include_loopback=True
)

print("Capturing audio from:")
print("  ", speaker.name)

print()
print(f"Destination: {UBUNTU_IP}:{PORT}")
print(f"Sample rate: {SAMPLE_RATE} Hz")
print(f"Channels:    {CHANNELS}")
print(f"Packet:      {BLOCK_SAMPLES / SAMPLE_RATE * 1000:.1f} ms")
print()
print("Press Ctrl+C to stop.")
print()

sequence = 0

with loopback.recorder(
    samplerate=SAMPLE_RATE,
    channels=CHANNELS,
    blocksize=BLOCK_SAMPLES,
) as recorder:

    while True:

        audio = recorder.record(
            numframes=BLOCK_SAMPLES
        )

        # Float [-1, 1] -> signed 16-bit PCM
        audio = np.clip(audio, -1.0, 1.0)

        pcm = (
            audio * 32767
        ).astype(np.int16)

        packet = (
            struct.pack("!I", sequence)
            + pcm.tobytes()
        )

        sock.sendto(
            packet,
            (UBUNTU_IP, PORT)
        )

        sequence = (
            sequence + 1
        ) & 0xFFFFFFFF
