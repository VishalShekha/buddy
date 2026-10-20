import socket
import struct
import sounddevice as sd

# ============================================================
# CONFIGURATION
# ============================================================

PORT = 5000

SAMPLE_RATE = 48000
CHANNELS = 2

# Must match the sender.
BLOCK_SAMPLES = 32

# Keep the UDP receive queue reasonably small.
SOCKET_BUFFER = 64 * 1024

# Maximum UDP packet size.
MAX_PACKET_SIZE = 65535

# ============================================================

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

sock.setsockopt(
    socket.SOL_SOCKET,
    socket.SO_RCVBUF,
    SOCKET_BUFFER,
)

sock.bind(("0.0.0.0", PORT))

print("UDP audio receiver")
print()
print(f"Listening on : 0.0.0.0:{PORT}")
print(f"Sample rate  : {SAMPLE_RATE} Hz")
print(f"Channels     : {CHANNELS}")
print(f"Block        : {BLOCK_SAMPLES} samples")
print(
    f"Audio/packet : "
    f"{BLOCK_SAMPLES / SAMPLE_RATE * 1000:.3f} ms"
)
print()
print("Waiting for Windows audio...")
print("Press Ctrl+C to stop.")
print()

expected_sequence = None
packets_lost = 0
packets_received = 0

with sd.RawOutputStream(
    samplerate=SAMPLE_RATE,
    channels=CHANNELS,
    dtype="int16",
    blocksize=BLOCK_SAMPLES,
    latency="low",
) as output:

    while True:

        data, _ = sock.recvfrom(MAX_PACKET_SIZE)

        # Need at least the 4-byte sequence number.
        if len(data) <= 4:
            continue

        sequence = struct.unpack_from(
            "!I",
            data,
            0,
        )[0]

        pcm = data[4:]

        # Detect missing UDP packets.
        if expected_sequence is not None:

            difference = (
                sequence - expected_sequence
            ) & 0xFFFFFFFF

            if difference:
                packets_lost += difference

        expected_sequence = (
            sequence + 1
        ) & 0xFFFFFFFF

        packets_received += 1

        # Immediately send PCM to the audio device.
        output.write(pcm)
