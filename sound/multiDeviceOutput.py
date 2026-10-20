"""
Multi-Output Audio Router for Windows
=====================================
Captures whatever Windows is currently playing (WASAPI loopback) and sends the
same audio to any number of output devices at once (Bluetooth headphones,
speakers, HDMI, ...).

Install:
    pip install PyAudioWPatch numpy

Run:
    python multi_audio_router.py

How to use:
 1. Pick a "capture source". This is the device Windows is currently playing to
    (normally your Windows default output, e.g. the laptop speakers).
 2. Tick the devices that should receive the audio.
 3. Press Start. You can tick/untick devices while it is running.
 4. Bluetooth devices have different latencies. If one sounds late/early,
    raise the "Delay" of the one that is AHEAD until they line up.

Avoiding echo/feedback:
 - Do NOT tick the capture-source device itself as an output (the app blocks it).
 - Set the Windows default output to a device you are NOT listening on
   (e.g. laptop speakers) and mute/lower it. On most systems loopback capture
   is unaffected by that device's volume. If yours is, install a virtual cable
   (e.g. VB-CABLE), set it as the Windows default output and capture from it.
 - Pick the stereo "Headphones" entry of a Bluetooth device, not the
   "Headset / Hands-Free" one (that is low-quality mono telephony mode).
"""

import collections
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    sys.exit("Missing dependency. Run:  pip install PyAudioWPatch numpy")

BLOCK = 1024          # frames per audio callback
JITTER_MS = 60        # base buffer that smooths out timing jitter
MAX_EXTRA_MS = 250    # buffer above target is dropped (limits drift/latency)


# --------------------------------------------------------------------------- #
# Audio engine
# --------------------------------------------------------------------------- #
class FrameBuffer:
    """Thread-safe FIFO of float32 audio frames, shape (n, channels)."""

    def __init__(self, channels):
        self.ch = channels
        self.chunks = collections.deque()
        self.frames = 0
        self.lock = threading.Lock()

    def write(self, arr, max_frames):
        with self.lock:
            self.chunks.append(arr)
            self.frames += len(arr)
            while self.frames > max_frames and len(self.chunks) > 1:
                self.frames -= len(self.chunks.popleft())

    def read(self, n):
        out, got = [], 0
        with self.lock:
            while got < n and self.chunks:
                c = self.chunks.popleft()
                need = n - got
                if len(c) > need:
                    self.chunks.appendleft(c[need:])
                    c = c[:need]
                out.append(c)
                got += len(c)
            self.frames -= got
        if not out:
            return np.zeros((0, self.ch), np.float32)
        return np.concatenate(out)

    def available(self):
        return self.frames

    def clear(self):
        with self.lock:
            self.chunks.clear()
            self.frames = 0


class Output:
    """One playback device fed from the shared capture stream."""

    def __init__(self, pa, info, src_rate, src_ch, delay_ms=0):
        self.pa = pa
        self.info = info
        self.rate = int(info["defaultSampleRate"])
        self.ch = 2 if info["maxOutputChannels"] >= 2 else 1
        self.src_rate = src_rate
        self.src_ch = src_ch
        self.delay_ms = delay_ms
        self.buf = FrameBuffer(self.ch)
        self.primed = False
        self.carry = 0.0
        self.stream = None

    # -- lifecycle --
    def start(self):
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.ch,
            rate=self.rate,
            output=True,
            output_device_index=self.info["index"],
            frames_per_buffer=BLOCK,
            stream_callback=self._callback,
        )

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def set_delay(self, ms):
        self.delay_ms = max(0, int(ms))
        self.primed = False      # re-prime the buffer with the new delay
        self.buf.clear()

    # -- data path --
    def _target_frames(self):
        return int((JITTER_MS + self.delay_ms) / 1000 * self.rate)

    def _convert(self, x):
        n, c = x.shape
        if c != self.ch:
            if self.ch == 1:
                x = x.mean(axis=1, keepdims=True)
            elif c == 1:
                x = np.repeat(x, 2, axis=1)
            else:
                x = x[:, :2]
        if self.rate != self.src_rate:
            exact = n * self.rate / self.src_rate + self.carry
            m = int(exact)
            self.carry = exact - m
            if m <= 0:
                return np.zeros((0, self.ch), np.float32)
            pos = np.linspace(0, n - 1, m)
            idx = np.arange(n)
            x = np.stack(
                [np.interp(pos, idx, x[:, k]) for k in range(self.ch)], axis=1
            ).astype(np.float32)
        return x

    def push(self, data):
        x = self._convert(data)
        if len(x):
            limit = int((JITTER_MS + self.delay_ms + MAX_EXTRA_MS) / 1000 * self.rate)
            self.buf.write(x, limit)

    def _callback(self, _in, frame_count, _time, _status):
        if not self.primed and self.buf.available() >= self._target_frames():
            self.primed = True
        if self.primed:
            block = self.buf.read(frame_count)
            if len(block) < frame_count:          # underrun -> re-prime
                self.primed = False
        else:
            block = np.zeros((0, self.ch), np.float32)
        if len(block) < frame_count:
            pad = np.zeros((frame_count - len(block), self.ch), np.float32)
            block = np.concatenate([block, pad])
        pcm = (np.clip(block, -1.0, 1.0) * 32767).astype(np.int16)
        return (pcm.tobytes(), pyaudio.paContinue)


class Router:
    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.capture = None
        self.src_info = None
        self.src_rate = 0
        self.src_ch = 0
        self.outputs = {}      # device index -> Output
        self.running = False

    # -- device discovery --
    def loopback_devices(self):
        return list(self.pa.get_loopback_device_info_generator())

    def default_loopback(self):
        try:
            return self.pa.get_default_wasapi_loopback()
        except Exception:
            return None

    def output_devices(self):
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        devs = []
        for i in range(self.pa.get_device_count()):
            d = self.pa.get_device_info_by_index(i)
            if (
                d["hostApi"] == wasapi["index"]
                and d["maxOutputChannels"] > 0
                and not d.get("isLoopbackDevice", False)
            ):
                devs.append(d)
        return devs

    # -- control --
    def start(self, src_info):
        self.src_info = src_info
        self.src_rate = int(src_info["defaultSampleRate"])
        self.src_ch = int(src_info["maxInputChannels"])
        self.capture = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.src_ch,
            rate=self.src_rate,
            input=True,
            input_device_index=src_info["index"],
            frames_per_buffer=BLOCK,
            stream_callback=self._on_capture,
        )
        self.running = True

    def stop(self):
        self.running = False
        for idx in list(self.outputs):
            self.remove_output(idx)
        if self.capture is not None:
            try:
                self.capture.stop_stream()
                self.capture.close()
            except Exception:
                pass
            self.capture = None

    def add_output(self, info, delay_ms=0):
        idx = info["index"]
        if idx in self.outputs:
            return
        out = Output(self.pa, info, self.src_rate, self.src_ch, delay_ms)
        out.start()                      # may raise; caller handles it
        self.outputs[idx] = out

    def remove_output(self, idx):
        out = self.outputs.pop(idx, None)
        if out:
            out.stop()

    def _on_capture(self, data, frames, _time, _status):
        try:
            if frames and data:
                arr = np.frombuffer(data, dtype=np.int16).reshape(-1, self.src_ch)
                arr = arr.astype(np.float32) / 32768.0
                for out in list(self.outputs.values()):
                    out.push(arr)
        except Exception:
            pass
        return (None, pyaudio.paContinue)

    def close(self):
        self.stop()
        self.pa.terminate()


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Multi-Output Audio Router")
        self.minsize(520, 360)
        self.router = Router()
        self.rows = {}          # device index -> dict(info, var, delay)
        self.loopbacks = []

        pad = {"padx": 10, "pady": 4}

        ttk.Label(self, text="Capture source (the device Windows plays to):").pack(
            anchor="w", **pad
        )
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10)
        self.src_var = tk.StringVar()
        self.src_box = ttk.Combobox(top, textvariable=self.src_var, state="readonly")
        self.src_box.pack(side="left", fill="x", expand=True)
        self.refresh_btn = ttk.Button(top, text="Refresh", command=self.refresh)
        self.refresh_btn.pack(side="left", padx=(6, 0))

        ttk.Label(self, text="Send audio to:").pack(anchor="w", **pad)
        self.dev_frame = ttk.Frame(self)
        self.dev_frame.pack(fill="both", expand=True, padx=10)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=10)
        self.start_btn = ttk.Button(bottom, text="Start", command=self.toggle)
        self.start_btn.pack(side="left")
        self.status = ttk.Label(bottom, text="Stopped")
        self.status.pack(side="left", padx=12)

        ttk.Label(
            self,
            text="Tip: raise the Delay on whichever device plays AHEAD to sync them.",
            foreground="#666",
        ).pack(anchor="w", padx=10, pady=(0, 8))

        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # -- helpers --
    def source_name(self):
        return self.src_var.get().replace(" [Loopback]", "").strip()

    def current_source(self):
        for d in self.loopbacks:
            if d["name"] == self.src_var.get():
                return d
        return None

    def refresh(self):
        if self.router.running:
            return
        self.loopbacks = self.router.loopback_devices()
        names = [d["name"] for d in self.loopbacks]
        self.src_box["values"] = names
        default = self.router.default_loopback()
        if default and default["name"] in names:
            self.src_var.set(default["name"])
        elif names:
            self.src_var.set(names[0])

        for w in self.dev_frame.winfo_children():
            w.destroy()
        self.rows.clear()

        header = ttk.Frame(self.dev_frame)
        header.pack(fill="x")
        ttk.Label(header, text="Device").pack(side="left")
        ttk.Label(header, text="Delay (ms)").pack(side="right", padx=(0, 6))

        for d in self.router.output_devices():
            row = ttk.Frame(self.dev_frame)
            row.pack(fill="x", pady=2)
            var = tk.BooleanVar(value=False)
            delay = tk.StringVar(value="0")
            idx = d["index"]
            ttk.Checkbutton(
                row, text=d["name"], variable=var,
                command=lambda i=idx: self.on_toggle(i),
            ).pack(side="left")
            sp = ttk.Spinbox(row, from_=0, to=2000, increment=10, width=6,
                             textvariable=delay)
            sp.pack(side="right")
            delay.trace_add("write", lambda *_a, i=idx: self.on_delay(i))
            self.rows[idx] = {"info": d, "var": var, "delay": delay}

    def delay_of(self, idx):
        try:
            return max(0, int(self.rows[idx]["delay"].get()))
        except ValueError:
            return 0

    # -- events --
    def on_toggle(self, idx):
        row = self.rows[idx]
        if not self.router.running:
            return
        if row["var"].get():
            self.try_add(idx)
        else:
            self.router.remove_output(idx)
        self.update_status()

    def on_delay(self, idx):
        out = self.router.outputs.get(idx)
        if out:
            out.set_delay(self.delay_of(idx))

    def try_add(self, idx):
        row = self.rows[idx]
        if row["info"]["name"] == self.source_name():
            messagebox.showwarning(
                "Feedback loop",
                "That device is the capture source. Sending audio back to it would "
                "create an echo loop.\n\nChoose a different capture source or device.",
            )
            row["var"].set(False)
            return False
        try:
            self.router.add_output(row["info"], self.delay_of(idx))
            return True
        except Exception as e:
            row["var"].set(False)
            messagebox.showerror(
                "Could not open device",
                f"{row['info']['name']}\n\n{e}\n\n"
                "Make sure it is connected and not in use in exclusive mode.",
            )
            return False

    def toggle(self):
        if self.router.running:
            self.router.stop()
            self.start_btn.config(text="Start")
            self.src_box.config(state="readonly")
            self.refresh_btn.config(state="normal")
            self.status.config(text="Stopped")
            return

        src = self.current_source()
        if not src:
            messagebox.showerror("No source", "No loopback capture device found.")
            return
        try:
            self.router.start(src)
        except Exception as e:
            messagebox.showerror("Capture failed", str(e))
            return
        for idx, row in self.rows.items():
            if row["var"].get():
                self.try_add(idx)
        self.start_btn.config(text="Stop")
        self.src_box.config(state="disabled")
        self.refresh_btn.config(state="disabled")
        self.update_status()

    def update_status(self):
        n = len(self.router.outputs)
        self.status.config(text=f"Running - {n} output device{'s' if n != 1 else ''}")

    def on_close(self):
        self.router.close()
        self.destroy()


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("This tool uses WASAPI loopback and only works on Windows.")
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()