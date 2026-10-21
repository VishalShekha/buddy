"""
Multi-Output Audio Router for Windows  (low-latency edition)
============================================================
Captures whatever Windows is currently playing (WASAPI loopback) and sends the
same audio to any number of output devices at once. Every output device has its
own Volume, Bass, Treble, Balance, Mute and Delay.

Install:
    pip install PyAudioWPatch numpy scipy

Run:
    python multi_audio_router_lowlatency.py

What makes this version low-latency
-----------------------------------
 * Adaptive buffer: each output watches the LOWEST fill level it reaches and
   steers it to "one block + a small safety margin". The buffer therefore
   shrinks to exactly what your devices need instead of a fixed guess.
 * Drift correction: capture and playback run on different clocks. A tiny
   (<= 0.5 %) resampling adjustment keeps the buffer level steady, so there are
   no periodic clicks even with a very small buffer.
 * One continuous resampler (also used for 44.1 <-> 48 kHz), with no
   block-boundary artefacts.
 * Pre-allocated ring buffers, no per-block list/concat work, and the audio
   threads are registered with Windows MMCSS ("Pro Audio") for scheduling
   priority.
 * Self-tuning: after an underrun (a crackle) the safety margin of that output
   grows by 2 ms automatically, up to 40 ms.

Latency modes (bottom of the window, choose before pressing Start):
   Ultra-low  128-frame blocks, 2 ms margin
   Low        256-frame blocks, 5 ms margin
   Safe       512-frame blocks, 15 ms margin
If you hear crackling in Ultra-low, move to Low.

Limits you cannot remove in software:
 * Windows shared-mode audio itself adds roughly 10-20 ms.
 * Bluetooth adds 100-250 ms depending on the codec.
 * Set all devices to the same sample rate in Windows Sound settings
   (e.g. 48000 Hz) so no rate conversion is needed at all.

Per-device controls:
 - Volume   0-200 %  (peaks are soft-limited, not hard clipped)
 - Bass     -12..+12 dB  (low shelf at 100 Hz)
 - Treble   -12..+12 dB  (high shelf at 8 kHz)
 - Balance, Mute, Delay (use Delay only to sync a slower device, e.g. Bluetooth)

Avoiding echo/feedback:
 - The capture-source device cannot be used as an output. To control EVERY
   device, set a virtual cable (e.g. VB-CABLE) as the Windows default output and
   capture from "CABLE Input [Loopback]".
 - Pick the stereo "Headphones" entry of a Bluetooth device, not "Hands-Free".
"""

import math
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np

try:
    import pyaudiowpatch as pyaudio
    from scipy.signal import sosfilt
except ImportError:
    sys.exit("Missing dependency. Run:  pip install PyAudioWPatch numpy scipy")

# name -> (frames per block, safety margin in ms)
LATENCY_MODES = {
    "Ultra-low  (128 frames, 2 ms margin)": (128, 2),
    "Low  (256 frames, 5 ms margin)": (256, 5),
    "Safe  (512 frames, 15 ms margin)": (512, 15),
}

PRIME_EXTRA_MS = 12     # extra audio gathered before playback (re)starts
MAX_EXTRA_MS = 40       # anything buffered beyond this is dropped (caps latency)
BUMP_MS = 2.0           # margin added after each underrun
MAX_BUMP_MS = 40.0
WINDOW_S = 0.25         # drift controller measures the minimum over this window
KP = 1.0                # controller gain (ratio change per second of error)
MAX_ADJ = 0.005         # max resampling adjustment (+-0.5 %)

BASS_FREQ = 100.0       # Hz, low-shelf corner
TREBLE_FREQ = 8000.0    # Hz, high-shelf corner

_tls = threading.local()


def _boost_thread():
    """Register the calling audio thread with MMCSS (once per thread)."""
    if getattr(_tls, "done", False):
        return
    _tls.done = True
    try:
        import ctypes
        ctypes.windll.avrt.AvSetMmThreadCharacteristicsW(
            "Pro Audio", ctypes.byref(ctypes.c_ulong(0))
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# DSP helpers
# --------------------------------------------------------------------------- #
def shelf_sos(kind, freq, gain_db, fs):
    """RBJ-cookbook shelving biquad (slope S=1) returned as one SOS row."""
    freq = min(freq, fs * 0.45)
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * freq / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / 2 * math.sqrt(2)
    t = 2 * math.sqrt(A) * alpha
    if kind == "low":
        b0 = A * ((A + 1) - (A - 1) * cw + t)
        b1 = 2 * A * ((A - 1) - (A + 1) * cw)
        b2 = A * ((A + 1) - (A - 1) * cw - t)
        a0 = (A + 1) + (A - 1) * cw + t
        a1 = -2 * ((A - 1) + (A + 1) * cw)
        a2 = (A + 1) + (A - 1) * cw - t
    else:
        b0 = A * ((A + 1) + (A - 1) * cw + t)
        b1 = -2 * A * ((A - 1) + (A + 1) * cw)
        b2 = A * ((A + 1) + (A - 1) * cw - t)
        a0 = (A + 1) - (A - 1) * cw + t
        a1 = 2 * ((A - 1) - (A + 1) * cw)
        a2 = (A + 1) - (A - 1) * cw - t
    return [b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]


def soft_limit(x, knee=0.8):
    """Transparent below `knee`, smoothly compresses peaks toward +-1."""
    ax = np.abs(x)
    over = ax > knee
    if over.any():
        span = 1.0 - knee
        x = np.where(over, np.sign(x) * (knee + span * np.tanh((ax - knee) / span)), x)
    return x


# --------------------------------------------------------------------------- #
# Audio engine
# --------------------------------------------------------------------------- #
class RingBuffer:
    """Fixed-size float32 ring buffer, shape (frames, channels)."""

    def __init__(self, channels, capacity):
        self.ch = channels
        self.cap = capacity
        self.data = np.zeros((capacity, channels), np.float32)
        self.r = self.w = self.n = 0
        self.lock = threading.Lock()

    def write(self, x, max_fill):
        m = len(x)
        if m == 0:
            return
        max_fill = min(max_fill, self.cap)
        if m > max_fill:
            x, m = x[-max_fill:], max_fill
        with self.lock:
            over = self.n + m - max_fill          # drop OLDEST audio on overflow
            if over > 0:
                self.r = (self.r + over) % self.cap
                self.n -= over
            end = self.w + m
            if end <= self.cap:
                self.data[self.w:end] = x
            else:
                k = self.cap - self.w
                self.data[self.w:] = x[:k]
                self.data[:m - k] = x[k:]
            self.w = end % self.cap
            self.n += m

    def read(self, m):
        with self.lock:
            m = min(m, self.n)
            out = np.empty((m, self.ch), np.float32)
            if m:
                end = self.r + m
                if end <= self.cap:
                    out[:] = self.data[self.r:end]
                else:
                    k = self.cap - self.r
                    out[:k] = self.data[self.r:]
                    out[k:] = self.data[:m - k]
                self.r = end % self.cap
                self.n -= m
        return out

    def clear(self):
        with self.lock:
            self.r = self.w = self.n = 0


class Output:
    """One playback device fed from the shared capture stream."""

    def __init__(self, pa, info, src_rate, src_ch, block, margin_ms,
                 delay_ms=0, settings=None):
        self.pa = pa
        self.info = info
        self.rate = int(info["defaultSampleRate"])
        self.ch = 2 if info["maxOutputChannels"] >= 2 else 1
        self.src_rate = src_rate
        self.src_ch = src_ch
        self.block = block
        self.margin_ms = margin_ms
        self.delay_ms = delay_ms
        self.bump_ms = 0.0
        self.stream = None

        # buffering / resampling state (touched by the playback thread only,
        # except `ring`, which the capture thread writes to)
        self.base_ratio = src_rate / self.rate
        self.ring = RingBuffer(self.ch, int(src_rate * 4))
        self._hist = np.zeros((0, self.ch), np.float32)
        self._frac = 0.0
        self._adj = 0.0
        self._arange = {}
        self._win_len = max(4, int(WINDOW_S * self.rate / block))
        self._win_min = None
        self._win_cnt = 0
        self.primed = False
        self._reset_req = False
        self._recalc()

        # sound controls
        self.volume = 1.0
        self.bass_db = 0.0
        self.treble_db = 0.0
        self.balance = 0.0
        self.muted = False
        self._sos = None
        self._zi = None
        self._prev_gain = None
        self._ramps = {}
        self.set_sound(**(settings or {}))

    # -- buffer sizing (all in SOURCE-rate frames) --
    def _recalc(self):
        sr = self.src_rate
        block_in = self.block * self.base_ratio
        extra_s = (self.margin_ms + self.bump_ms + self.delay_ms) / 1000.0
        self._desired = int(block_in + extra_s * sr) + 2   # target for the minimum fill
        self._prime = self._desired + int(PRIME_EXTRA_MS / 1000 * sr)
        self._maxfill = self._prime + int(MAX_EXTRA_MS / 1000 * sr)

    def _buffered(self):
        return self.ring.n + len(self._hist)

    # -- sound controls (safe to call from the GUI thread) --
    def set_sound(self, volume=None, bass_db=None, treble_db=None,
                  balance=None, muted=None):
        if volume is not None:
            self.volume = max(0.0, float(volume))
        if balance is not None:
            self.balance = max(-1.0, min(1.0, float(balance)))
        if muted is not None:
            self.muted = bool(muted)
        if bass_db is not None or treble_db is not None:
            if bass_db is not None:
                self.bass_db = float(bass_db)
            if treble_db is not None:
                self.treble_db = float(treble_db)
            self._build_eq()

    def _build_eq(self):
        rows = []
        if abs(self.bass_db) >= 0.1:
            rows.append(shelf_sos("low", BASS_FREQ, self.bass_db, self.rate))
        if abs(self.treble_db) >= 0.1:
            rows.append(shelf_sos("high", TREBLE_FREQ, self.treble_db, self.rate))
        if not rows:
            self._sos, self._zi = None, None
            return
        sos = np.array(rows, dtype=np.float64)
        if self._zi is None or self._zi.shape[0] != len(rows):
            self._zi = np.zeros((len(rows), 2, self.ch))
        self._sos = sos

    def _process(self, block):
        """EQ -> volume/balance (ramped) -> soft limiter. block: (n, ch) float32."""
        n = len(block)
        sos, zi = self._sos, self._zi
        if sos is not None and zi is not None:
            block, zi = sosfilt(sos, block.astype(np.float64), axis=0, zi=zi)
            self._zi = zi
            block = block.astype(np.float32)

        target = np.full(self.ch, 0.0 if self.muted else self.volume, np.float32)
        if self.ch == 2:
            target[0] *= 1.0 - max(0.0, self.balance)
            target[1] *= 1.0 + min(0.0, self.balance)
        prev = self._prev_gain if self._prev_gain is not None else target
        ramp = self._ramps.get(n)
        if ramp is None:
            ramp = self._ramps[n] = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
        gain = prev + (target - prev) * ramp
        self._prev_gain = target
        return soft_limit(block * gain)

    # -- lifecycle --
    def start(self):
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.ch,
            rate=self.rate,
            output=True,
            output_device_index=self.info["index"],
            frames_per_buffer=self.block,
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
        self._recalc()
        self._reset_req = True      # the playback thread performs the reset

    def _do_reset(self):
        self._reset_req = False
        self.ring.clear()
        self._hist = np.zeros((0, self.ch), np.float32)
        self._frac = 0.0
        self._adj = 0.0
        self._reset_window()
        self.primed = False

    def _reset_window(self):
        self._win_min = None
        self._win_cnt = 0

    # -- data path --
    def push(self, x):
        """Called from the capture thread with float32 (n, src_ch) audio."""
        c = x.shape[1]
        if c != self.ch:
            if self.ch == 1:
                x = x.mean(axis=1, keepdims=True)
            elif c == 1:
                x = np.repeat(x, 2, axis=1)
            else:
                x = x[:, :2]
        self.ring.write(x, self._maxfill)

    def _track(self, buffered):
        """Drift controller: keep the window-minimum fill at `_desired`."""
        if self._win_min is None or buffered < self._win_min:
            self._win_min = buffered
        self._win_cnt += 1
        if self._win_cnt >= self._win_len:
            err = (self._win_min - self._desired) / self.src_rate     # seconds
            self._adj = max(-MAX_ADJ, min(MAX_ADJ, KP * err))
            self._reset_window()

    def _read(self, n):
        """Return n output frames (resampled + drift-corrected) or None on underrun."""
        ratio = self.base_ratio * (1.0 + self._adj)
        self._track(self._buffered())

        frac = self._frac
        last = frac + (n - 1) * ratio
        p_end = frac + n * ratio
        need = max(int(last) + 2, int(p_end))

        src = self._hist
        if len(src) < need:
            more = self.ring.read(need - len(src))
            if len(more):
                src = more if len(src) == 0 else np.concatenate((src, more))
            if len(src) < need:
                self._hist = src
                return None

        ar = self._arange.get(n)
        if ar is None:
            ar = self._arange[n] = np.arange(n, dtype=np.float64)
        pos = frac + ratio * ar
        i0 = pos.astype(np.intp)
        w = (pos - i0).astype(np.float32)[:, None]
        out = src[i0] * (1.0 - w) + src[i0 + 1] * w

        used = int(p_end)
        self._frac = p_end - used
        self._hist = src[used:]
        return out

    def _callback(self, _in, n, _time, _status):
        _boost_thread()
        if self._reset_req:
            self._do_reset()
        if not self.primed and self._buffered() >= self._prime:
            self.primed = True
            self._reset_window()

        block = None
        if self.primed:
            try:
                block = self._read(n)
                if block is None:                       # underrun
                    self.primed = False
                    self._adj = 0.0
                    self.bump_ms = min(self.bump_ms + BUMP_MS, MAX_BUMP_MS)
                    self._recalc()
                else:
                    block = self._process(block)
            except Exception:
                block = None
        if block is None:
            return (bytes(n * self.ch * 2), pyaudio.paContinue)
        pcm = (np.clip(block, -1.0, 1.0) * 32767).astype(np.int16)
        return (pcm.tobytes(), pyaudio.paContinue)


class Router:
    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.capture = None
        self.src_info = None
        self.src_rate = 0
        self.src_ch = 0
        self.block = 128
        self.margin_ms = 2
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
    def start(self, src_info, block=128, margin_ms=2):
        self.src_info = src_info
        self.block = block
        self.margin_ms = margin_ms
        self.src_rate = int(src_info["defaultSampleRate"])
        self.src_ch = int(src_info["maxInputChannels"])
        self.capture = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.src_ch,
            rate=self.src_rate,
            input=True,
            input_device_index=src_info["index"],
            frames_per_buffer=block,
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

    def add_output(self, info, delay_ms=0, settings=None):
        idx = info["index"]
        if idx in self.outputs:
            return
        out = Output(self.pa, info, self.src_rate, self.src_ch,
                     self.block, self.margin_ms, delay_ms, settings)
        out.start()                      # may raise; caller handles it
        self.outputs[idx] = out

    def remove_output(self, idx):
        out = self.outputs.pop(idx, None)
        if out:
            out.stop()

    def _on_capture(self, data, frames, _time, _status):
        _boost_thread()
        try:
            if frames and data:
                arr = np.frombuffer(data, dtype=np.int16).reshape(-1, self.src_ch)
                arr = arr.astype(np.float32) * (1.0 / 32768.0)
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
class Slider(ttk.Frame):
    """Label + slider + live value readout, laid out on a grid cell."""

    def __init__(self, parent, text, var, lo, hi, fmt, command, width=110):
        super().__init__(parent)
        ttk.Label(self, text=text, width=8).pack(side="left")
        ttk.Scale(self, from_=lo, to=hi, variable=var, length=width,
                  command=lambda _v: command()).pack(side="left")
        self.val = ttk.Label(self, width=7, anchor="e")
        self.val.pack(side="left")
        var.trace_add("write", lambda *_a: self.val.config(text=fmt(var.get())))
        self.val.config(text=fmt(var.get()))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Multi-Output Audio Router")
        self.minsize(680, 480)
        self.router = Router()
        self.rows = {}          # device index -> dict(...)
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

        # scrollable device list
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=10)
        self.canvas = tk.Canvas(wrap, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.dev_frame = ttk.Frame(self.canvas)
        self.win = self.canvas.create_window((0, 0), window=self.dev_frame, anchor="nw")
        self.dev_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width)
        )
        self.canvas.bind_all(
            "<MouseWheel>",
            lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"),
        )

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=10)
        self.start_btn = ttk.Button(bottom, text="Start", command=self.toggle)
        self.start_btn.pack(side="left")
        self.status = ttk.Label(bottom, text="Stopped")
        self.status.pack(side="left", padx=12)
        self.mode_var = tk.StringVar(value=list(LATENCY_MODES)[0])
        self.mode_box = ttk.Combobox(
            bottom, textvariable=self.mode_var, values=list(LATENCY_MODES),
            state="readonly", width=34,
        )
        self.mode_box.pack(side="right")
        ttk.Label(bottom, text="Latency:").pack(side="right", padx=(0, 4))

        ttk.Label(
            self,
            text="Crackling? Pick a safer Latency mode. Raise Delay only on the device "
                 "that plays AHEAD (to sync Bluetooth).",
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

        for d in self.router.output_devices():
            idx = d["index"]
            box = ttk.LabelFrame(self.dev_frame, padding=6)
            box.pack(fill="x", pady=4)

            var = tk.BooleanVar(value=False)
            delay = tk.StringVar(value="0")
            vol = tk.DoubleVar(value=100)
            bass = tk.DoubleVar(value=0)
            treble = tk.DoubleVar(value=0)
            bal = tk.DoubleVar(value=0)
            mute = tk.BooleanVar(value=False)

            head = ttk.Frame(box)
            head.pack(fill="x")
            ttk.Checkbutton(
                head, text=d["name"], variable=var,
                command=lambda i=idx: self.on_toggle(i),
            ).pack(side="left")
            ttk.Spinbox(head, from_=0, to=2000, increment=10, width=6,
                        textvariable=delay).pack(side="right")
            ttk.Label(head, text="Delay (ms)").pack(side="right", padx=(0, 4))
            delay.trace_add("write", lambda *_a, i=idx: self.on_delay(i))

            cb = lambda i=idx: self.on_sound(i)
            grid = ttk.Frame(box)
            grid.pack(fill="x", pady=(4, 0))
            s_vol = Slider(grid, "Volume", vol, 0, 200, lambda v: f"{v:.0f} %", cb)
            s_bas = Slider(grid, "Bass", bass, -12, 12, lambda v: f"{v:+.0f} dB", cb)
            s_tre = Slider(grid, "Treble", treble, -12, 12, lambda v: f"{v:+.0f} dB", cb)
            s_bal = Slider(grid, "Balance", bal, -100, 100, self._fmt_bal, cb)
            s_vol.grid(row=0, column=0, sticky="w", padx=(0, 12))
            s_bas.grid(row=0, column=1, sticky="w")
            s_tre.grid(row=1, column=0, sticky="w", padx=(0, 12))
            s_bal.grid(row=1, column=1, sticky="w")
            ttk.Checkbutton(grid, text="Mute", variable=mute, command=cb).grid(
                row=0, column=2, rowspan=2, padx=(12, 0))
            ttk.Button(grid, text="Reset", width=6,
                       command=lambda i=idx: self.reset_sound(i)).grid(
                row=0, column=3, rowspan=2, padx=(6, 0))

            self.rows[idx] = {
                "info": d, "var": var, "delay": delay,
                "vol": vol, "bass": bass, "treble": treble, "bal": bal,
                "mute": mute,
            }

    @staticmethod
    def _fmt_bal(v):
        if abs(v) < 2:
            return "center"
        return f"L {abs(v):.0f}" if v < 0 else f"R {v:.0f}"

    def delay_of(self, idx):
        try:
            return max(0, int(self.rows[idx]["delay"].get()))
        except ValueError:
            return 0

    def sound_of(self, idx):
        r = self.rows[idx]
        return {
            "volume": r["vol"].get() / 100.0,
            "bass_db": r["bass"].get(),
            "treble_db": r["treble"].get(),
            "balance": r["bal"].get() / 100.0,
            "muted": r["mute"].get(),
        }

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

    def on_sound(self, idx):
        out = self.router.outputs.get(idx)
        if out:
            out.set_sound(**self.sound_of(idx))

    def reset_sound(self, idx):
        r = self.rows[idx]
        r["vol"].set(100)
        r["bass"].set(0)
        r["treble"].set(0)
        r["bal"].set(0)
        r["mute"].set(False)
        self.on_sound(idx)

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
            self.router.add_output(row["info"], self.delay_of(idx), self.sound_of(idx))
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
            self.mode_box.config(state="readonly")
            self.refresh_btn.config(state="normal")
            self.status.config(text="Stopped")
            return

        src = self.current_source()
        if not src:
            messagebox.showerror("No source", "No loopback capture device found.")
            return
        block, margin = LATENCY_MODES[self.mode_var.get()]
        try:
            self.router.start(src, block, margin)
        except Exception as e:
            messagebox.showerror("Capture failed", str(e))
            return
        for idx, row in self.rows.items():
            if row["var"].get():
                self.try_add(idx)
        self.start_btn.config(text="Stop")
        self.src_box.config(state="disabled")
        self.mode_box.config(state="disabled")
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
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)   # HIGH priority
    except Exception:
        pass
    sys.setswitchinterval(0.0005)      # let audio threads grab the GIL quickly
    App().mainloop()