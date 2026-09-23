"""LocalFlow — local voice dictation for Windows.

Press the hotkey anywhere in Windows and speak; stop talking and the speech
is transcribed locally with faster-whisper, optionally cleaned up by a local
Ollama model, and pasted into whatever text field has focus. While
recording, a small floating pill shows a live waveform, with an X to cancel
and a red button to pause and resume. Hold-to-talk is available too.

Read-aloud: select text anywhere and press the read hotkey (Ctrl+Alt+S) to
hear it spoken by Piper, a local neural TTS. Press again to stop.
"""

import collections
import ctypes
import difflib
import json
import math
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

# under pythonw.exe (background mode) there is no console: send prints to a
# log file instead so errors remain diagnosable
if sys.stdout is None or sys.stderr is None:
    _log = open(Path(__file__).parent / "localflow.log", "a",
                encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

# crisp overlay rendering on high-DPI displays
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# --- CUDA DLLs shipped as pip packages must be on the DLL search path
#     before ctranslate2 loads. ctranslate2 resolves them through PATH,
#     so os.add_dll_directory alone is not enough. -------------------------
_SITE = Path(sys.prefix) / "Lib" / "site-packages"
for _sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
    _p = _SITE / _sub
    if _p.is_dir():
        os.add_dll_directory(str(_p))
        os.environ["PATH"] = str(_p) + os.pathsep + os.environ["PATH"]

import numpy as np
import pyperclip
import requests
import sounddevice as sd
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from faster_whisper import WhisperModel

CONFIG_PATH = Path(__file__).parent / "config.json"
WHISPER_SR = 16000

# ---------------------------------------------------------------- config ---

DEFAULTS = {
    "whisper": {"model": "large-v3-turbo", "device": "auto",
                "language": ["pt", "en"], "beam_size": 1},
    "hotkey": ["ctrl", "win"],
    "activation": "toggle",
    # hands-free ("toggle") only: the pill shows an X that cancels and a red
    # button that pauses/resumes; Esc cancels too
    "pill_buttons": True,
    "esc_cancels": True,
    # silence_ms_long/long_after_sec: while composing a long dictation people
    # pause to think for much longer than they do in a one-liner, so the
    # window to keep listening widens once the recording gets long (for a
    # longer think there is the pause button)
    "auto_stop": {"silence_ms": 1500, "silence_ms_long": 2600,
                  "long_after_sec": 12, "min_level": 0.008,
                  "start_timeout_sec": 8, "max_sec": 180},
    "preroll_ms": 600,
    "tail_ms": 300,
    "min_speech_sec": 0.3,
    "inject_mode": "paste",
    "sounds": True,
    "sound_volume": 0.15,
    # chunk_chars: a 4B model stops reproducing faithfully somewhere past a
    # few hundred characters and starts skipping whole sentences, so long
    # transcripts are cleaned in pieces. budget_sec caps the whole job.
    "ollama": {"enabled": True, "url": "http://localhost:11434",
               "model": "gemma3:4b", "timeout_sec": 15,
               "chunk_chars": 600, "budget_sec": 40},
    "read_aloud": {"enabled": True, "hotkey": ["ctrl", "alt", "s"],
                   "voices": {"pt": "pt_BR-faber-medium",
                              "en": "en_US-lessac-medium"},
                   "speed": 1.0},
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for key, val in user.items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(val)
            else:
                cfg[key] = val
    return cfg


# ------------------------------------------------------- win32 key input ---

user32 = ctypes.WinDLL("user32", use_last_error=True)

VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "capslock": 0x14, "space": 0x20,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}
VK.update({c: 0x41 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")})
VK.update({str(d): 0x30 + d for d in range(10)})

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
ULONG_PTR = ctypes.c_size_t

# --- per-pixel alpha for the overlay window (UpdateLayeredWindow) ---------
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
ULW_ALPHA = 0x00000002


class SIZE(ctypes.Structure):
    _fields_ = (("cx", wintypes.LONG), ("cy", wintypes.LONG))


class POINT(ctypes.Structure):
    _fields_ = (("x", wintypes.LONG), ("y", wintypes.LONG))


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = (("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte),
                ("AlphaFormat", ctypes.c_byte))


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD))


# handles are pointer-sized: without these the return values get truncated
# to 32 bits on 64-bit Python and every GDI call silently fails
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = (wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD)
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = (wintypes.HWND,)
user32.UpdateLayeredWindow.argtypes = (
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(POINT), wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD)

# --- the pill's stop / cancel buttons --------------------------------------
# Tk can't be used for these clicks: its window procedure answers
# WM_MOUSEACTIVATE with MA_ACTIVATE — a click on the pill would pull focus
# away from the app we are about to paste into — and it builds button events
# from the physical mouse state. The pill's window procedure is subclassed
# instead and handles its clicks itself.
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)
GWLP_WNDPROC = -4
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020          # clicks pass straight through
WM_MOUSEACTIVATE, MA_NOACTIVATE = 0x0021, 3
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int,
                                     ctypes.c_void_p)
user32.CallWindowProcW.restype = ctypes.c_ssize_t
user32.CallWindowProcW.argtypes = (ctypes.c_void_p, wintypes.HWND,
                                   wintypes.UINT, wintypes.WPARAM,
                                   wintypes.LPARAM)
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)
user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR))


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR))


class INPUT(ctypes.Structure):
    class _I(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT))

    _anonymous_ = ("i",)
    _fields_ = (("type", wintypes.DWORD), ("i", _I))


def _send_key_events(events):
    """events: list of (wVk, wScan, flags)."""
    arr = (INPUT * len(events))()
    for slot, (vk, scan, flags) in zip(arr, events):
        slot.type = INPUT_KEYBOARD
        slot.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags,
                             time=0, dwExtraInfo=0)
    user32.SendInput(len(arr), arr, ctypes.sizeof(INPUT))


def send_ctrl_v():
    _send_key_events([
        (VK["ctrl"], 0, 0),
        (0x56, 0, 0),                      # V down
        (0x56, 0, KEYEVENTF_KEYUP),        # V up
        (VK["ctrl"], 0, KEYEVENTF_KEYUP),
    ])


def send_ctrl_c():
    _send_key_events([
        (VK["ctrl"], 0, 0),
        (0x43, 0, 0),                      # C down
        (0x43, 0, KEYEVENTF_KEYUP),        # C up
        (VK["ctrl"], 0, KEYEVENTF_KEYUP),
    ])


def type_unicode(text):
    """Type text directly via KEYEVENTF_UNICODE (clipboard-free fallback)."""
    units = text.encode("utf-16-le")
    events = []
    for i in range(0, len(units), 2):
        cu = int.from_bytes(units[i:i + 2], "little")
        events.append((0, cu, KEYEVENTF_UNICODE))
        events.append((0, cu, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    # send in chunks so very long texts don't overflow the input queue
    for i in range(0, len(events), 100):
        _send_key_events(events[i:i + 100])
        time.sleep(0.005)


def keys_down(vk_codes):
    return all(user32.GetAsyncKeyState(vk) & 0x8000 for vk in vk_codes)


MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # shift ctrl alt lwin rwin


def wait_modifiers_released(timeout=1.0):
    """A physically held modifier would corrupt the synthetic Ctrl+V (e.g.
    a still-held Win key turns it into Win+V), so give the user's fingers
    time to leave the keys."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000
                   for vk in MODIFIER_VKS):
            return
        time.sleep(0.01)


def release_all_modifiers():
    """After a synthetic Ctrl+V (or the Ctrl+Win hotkey) a modifier can be
    left logically 'down' in the target app even though the physical key is
    up, so every following keypress becomes a shortcut and the user 'can no
    longer type'. Force a key-up for each modifier to clear that state. The
    Ctrl held around the Win-ups masks the Win key so releasing it can never
    pop the Start menu."""
    _send_key_events([
        (VK["ctrl"], 0, 0),                    # mask: another key is 'down'
        (0x5B, 0, KEYEVENTF_KEYUP),            # LWin up
        (0x5C, 0, KEYEVENTF_KEYUP),            # RWin up
        (VK["shift"], 0, KEYEVENTF_KEYUP),
        (VK["alt"], 0, KEYEVENTF_KEYUP),
        (VK["ctrl"], 0, KEYEVENTF_KEYUP),      # release mask + any stuck Ctrl
    ])


# ---------------------------------------------------------------- sounds ---

class Sounds:
    """Soft sine tones with fade envelopes, played through sounddevice.
    Much gentler than winsound.Beep's square wave."""

    SR = 44100

    def __init__(self, enabled, volume):
        self.enabled = enabled
        self.volume = volume
        self.start_tone = self._melody([(523, 70), (659, 100)])
        self.done_tone = self._melody([(659, 60), (587, 90)])
        self.nospeech_tone = self._melody([(330, 160)])

    def _melody(self, notes):
        parts = []
        for freq, ms in notes:
            n = int(self.SR * ms / 1000)
            t = np.arange(n) / self.SR
            wave = np.sin(2 * np.pi * freq * t)
            # soft attack/release envelope so there is no click or harshness
            env = np.minimum(1.0, np.minimum(
                np.arange(n) / (0.25 * n), (n - np.arange(n)) / (0.45 * n)))
            parts.append(wave * env)
            parts.append(np.zeros(int(self.SR * 0.02)))
        return (np.concatenate(parts) * self.volume).astype(np.float32)

    def _play(self, tone):
        if not self.enabled:
            return
        try:
            sd.play(tone, self.SR)
        except Exception:
            pass

    def start(self):
        self._play(self.start_tone)

    def done(self):
        self._play(self.done_tone)

    def nospeech(self):
        self._play(self.nospeech_tone)


# -------------------------------------------------------------- recorder ---

def resample_to_16k(audio, sr):
    if sr == WHISPER_SR or len(audio) == 0:
        return audio.astype(np.float32)
    n = int(len(audio) * WHISPER_SR / sr)
    x = np.linspace(0, len(audio) - 1, n)
    return np.interp(x, np.arange(len(audio)), audio).astype(np.float32)


class Recorder:
    """Always-open mic stream at the device's native sample rate, with a
    rolling pre-roll buffer so words spoken right as the hotkey lands are
    not clipped. Exposes a live RMS level for the waveform overlay."""

    def __init__(self, preroll_ms):
        self._preroll_ms = preroll_ms
        self._preroll = collections.deque()
        self._preroll_len = 0
        self._frames = []
        self._recording = False
        self._paused = False
        self._lock = threading.Lock()
        self.level = 0.0
        self.last_cb = time.time()
        self._open_stream()

    def _open_stream(self):
        try:
            dev = sd.query_devices(kind="input")
            self.sr = int(dev["default_samplerate"]) or 44100
        except Exception:
            self.sr = 44100
        self._preroll_samples = int(self.sr * self._preroll_ms / 1000)
        self._stream = sd.InputStream(
            samplerate=self.sr, channels=1, dtype="float32",
            blocksize=int(self.sr * 0.03), callback=self._callback)
        self._stream.start()
        self.last_cb = time.time()

    def ensure_alive(self):
        """The input stream dies silently when Windows switches, removes or
        reconfigures the microphone, freezing self.level forever. Detect the
        stall (no callback for 1.5s) and reconnect to the current default
        device. Returns False while no microphone is usable."""
        if time.time() - self.last_cb < 1.5:
            return True
        return self.rebind()

    def rebind(self):
        """Close the stream and reopen on the CURRENT default device. Needed
        when Windows switches the default mic while the old device keeps
        delivering (near-silent) audio, so the stall watchdog never fires."""
        try:
            self._stream.close()
        except Exception:
            pass
        with self._lock:
            self._preroll.clear()
            self._preroll_len = 0
            self._frames = []
        self.level = 0.0
        try:
            # PortAudio caches the device list, so re-init to see the
            # current default mic (safe: our only input stream is closed)
            sd._terminate()
            sd._initialize()
            self._open_stream()
            print(f"[mic] reconnected at {self.sr} Hz")
            return True
        except Exception as e:
            self.last_cb = time.time()   # wait 1.5s before retrying
            print(f"[mic] no microphone available ({e})")
            return False

    def _callback(self, indata, frames, time_info, status):
        self.last_cb = time.time()
        block = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(block ** 2)))
        # fast attack, slow decay, so the bars feel alive but not jittery
        self.level = max(rms, self.level * 0.82)
        with self._lock:
            if self._recording and not self._paused:
                self._frames.append(block)
            else:
                self._preroll.append(block)
                self._preroll_len += len(block)
                while (self._preroll_len - len(self._preroll[0])
                       >= self._preroll_samples):
                    self._preroll_len -= len(self._preroll.popleft())

    def start(self):
        with self._lock:
            self._frames = list(self._preroll)
            self._recording = True
            self._paused = False

    def pause(self):
        """Stop keeping audio but hold on to what was said so far."""
        with self._lock:
            self._paused = True

    def resume(self):
        """Keep audio again. Unlike start() this does NOT prepend the
        pre-roll: it holds the last moments of the pause, which the speaker
        meant to keep out of the transcript."""
        with self._lock:
            if self._frames:
                # a short silence marks the cut, so the words before and
                # after the pause don't run together
                self._frames.append(np.zeros(int(self.sr * 0.3), np.float32))
            self._paused = False

    def stop(self):
        with self._lock:
            self._recording = False
            self._paused = False
            frames, self._frames = self._frames, []
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return resample_to_16k(np.concatenate(frames), self.sr)


# --------------------------------------------------------------- overlay ---

class LayeredSurface:
    """Per-pixel-alpha canvas for a frameless window.

    Windows is handed a 32-bit premultiplied BGRA bitmap through
    UpdateLayeredWindow, so anti-aliased corners and the drop shadow blend
    with whatever is behind the pill. The old chroma-key approach
    (-transparentcolor) could only make ONE exact color disappear, so every
    half-transparent edge pixel survived as a black fringe around the pill."""

    def __init__(self, hwnd, w, h):
        self.hwnd, self.w, self.h = hwnd, w, h
        self.screen_dc = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(bi)
        bi.biWidth, bi.biHeight = w, -h        # negative height = top-down
        bi.biPlanes, bi.biBitCount = 1, 32
        self.bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(self.screen_dc, ctypes.byref(bi), 0,
                                          ctypes.byref(self.bits), None, 0)
        self.prev = gdi32.SelectObject(self.dc, self.bmp)
        self.size = SIZE(w, h)
        self.origin = POINT(0, 0)
        # AC_SRC_OVER + AC_SRC_ALPHA: use the bitmap's own alpha channel
        self.blend = BLENDFUNCTION(0, 0, 255, 1)

    def push(self, img):
        """img: RGBA image already at the window size."""
        a = np.asarray(img.convert("RGBA"), dtype=np.uint8)
        alpha = a[:, :, 3].astype(np.uint16)
        out = np.empty_like(a)
        for dst, src in ((0, 2), (1, 1), (2, 0)):        # RGBA -> BGRA
            out[:, :, dst] = (a[:, :, src] * alpha // 255).astype(np.uint8)
        out[:, :, 3] = a[:, :, 3]                        # premultiplied
        buf = np.ascontiguousarray(out).tobytes()
        ctypes.memmove(self.bits, buf, len(buf))
        user32.UpdateLayeredWindow(self.hwnd, self.screen_dc, None, self.size,
                                   self.dc, self.origin, 0, self.blend,
                                   ULW_ALPHA)


class Overlay:
    """Frameless always-on-top pill at the bottom center of the screen.
    While recording it shows layered flowing sine waves that swell with the
    voice (Siri style); while transcribing, pulsing dots.
    Drawn with true per-pixel alpha: soft drop shadow, smooth pill ends, no
    chroma-key fringe. Never steals focus (WS_EX_NOACTIVATE, and every click
    answered with MA_NOACTIVATE).

    Given on_pause/on_cancel it gets two buttons: an X at the left end
    cancels, a red button at the right end pauses — its icon turns from
    pause into play and the wave settles into a dim flat line — and
    resumes on the next click. Clicks pass
    straight through the pill (WS_EX_TRANSPARENT) except while those
    buttons are up."""

    PILL_W, PILL_H = 172, 30      # the visible pill itself: slim, discreet
    MARGIN = 11                   # window padding that holds the drop shadow
    GAP = 90                      # gap between the pill and the screen edge

    # the pill with buttons is wider so the wave between them keeps exactly
    # the width it has without buttons
    BTN_PILL_W, BTN_PILL_H = 214, 34
    BTN_R = 12                    # button radius
    BTN_INSET = 5                 # pill end -> button edge
    BTN_GAP = 6                   # button edge -> where the wave may start
    BTN_REACH = 4                 # extra hit radius: a 24 px disc is small
    CLICK_GRACE = 0.6             # seconds the pill keeps catching clicks after
                                  # recording ends, so the second click of a
                                  # double-click can't land in the app below
    RED_TOP, RED_BOTTOM = (250, 92, 82), (218, 56, 50)
    RED_TOP_HOT, RED_BOTTOM_HOT = (255, 99, 90), (236, 72, 64)
    RED_GLOW = (255, 90, 80, 120)
    CANCEL_FILL, CANCEL_FILL_HOT = (255, 255, 255, 22), (255, 255, 255, 52)
    ICON, ICON_HOT = (218, 220, 240), (255, 255, 255)

    FILL_TOP = (42, 42, 54)       # subtle vertical gradient, lit from above
    FILL_BOTTOM = (20, 20, 26)
    FILL_ALPHA = 240              # a touch of translucency
    EDGE = (168, 170, 200, 58)    # single hairline rim, no double ring
    SHADOW = (0, 0, 0, 130)

    # wave layers, drawn dim-to-bright: (amplitude, cycles across the pill,
    # travel speed in cycles/s, line width, color)
    LAYERS = (
        (0.42, 2.6, -1.35, 1.2, (74, 77, 120)),
        (0.58, 1.9,  1.60, 1.2, (108, 112, 168)),
        (0.76, 2.2, -0.95, 1.3, (159, 163, 217)),
        (1.00, 1.5,  1.10, 1.6, (223, 225, 255)),
    )
    # teal palette while reading aloud, so "speaking" and "recording"
    # are distinguishable at a glance
    SPEAK_LAYERS = (
        (0.42, 2.8, -1.70, 1.2, (46, 94, 86)),
        (0.58, 2.0,  2.05, 1.2, (63, 138, 124)),
        (0.76, 2.3, -1.25, 1.3, (104, 191, 171)),
        (1.00, 1.6,  1.45, 1.6, (216, 255, 242)),
    )
    POINTS = 96       # polyline resolution: fewer points bead at the joints
    PAD = 14         # horizontal inset where the waves pinch to zero
    FADE = 0.22       # fraction of the span over which the waves fade out
    SS = 4            # supersampling factor: render big, downscale = smooth

    def __init__(self, root, get_level, get_speak_level=None,
                 on_pause=None, on_cancel=None):
        self.root = root
        self.get_level = get_level
        self.get_speak_level = get_speak_level or (lambda: 0.0)
        self.on_pause, self.on_cancel = on_pause, on_cancel
        self.buttons_on = bool(on_pause or on_cancel)
        self.mode = "hidden"
        self.amp = 0.0
        self.hover = None             # button under the mouse: "pause"/"cancel"
        self._pressed = None          # button the mouse went down on
        self._clicked = None          # button clicked, waiting for _animate
        self._clickable = False
        self._click_until = 0.0

        # scale the pill with the Windows display scaling factor so it is
        # the same physical size on 100% and 150% displays
        try:
            self.k = max(1.0, user32.GetDpiForSystem() / 96)
        except Exception:
            self.k = 1.0
        pw, ph = ((self.BTN_PILL_W, self.BTN_PILL_H) if self.buttons_on
                  else (self.PILL_W, self.PILL_H))
        self.pw = int(pw * self.k)
        self.ph = int(ph * self.k)
        self.m = int(self.MARGIN * self.k)
        self.W = self.pw + 2 * self.m
        self.H = self.ph + 2 * self.m
        self.PAD = int(self.PAD * self.k)
        if self.buttons_on:
            off = self.m + (self.BTN_INSET + self.BTN_R) * self.k
            self.btn_centers = {"cancel": (off, self.H / 2),
                                "pause": (self.W - off, self.H / 2)}
            inset = int((self.BTN_INSET + 2 * self.BTN_R + self.BTN_GAP)
                        * self.k)
        else:
            self.btn_centers = {}
            inset = self.PAD
        self.wave_x0 = self.m + inset
        self.wave_span = self.pw - 2 * inset

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self._screen = (0, 0)
        self._place()
        root.withdraw()
        root.update_idletasks()

        self.hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        self._prevent_focus_steal()
        if self.buttons_on:
            try:
                self._hook_clicks()
            except Exception as e:
                # without the hook a click could take focus from the app we
                # paste into, so the pill stays click-through instead
                print(f"[overlay] pill buttons disabled ({e}) — the hotkey "
                      f"and Esc still work")
                self.buttons_on = False
        self.surface = LayeredSurface(self.hwnd, self.W, self.H)
        self.base = self._render_base()
        # every look of the pill with buttons — (button under the mouse,
        # paused?) — rendered once; the waves never overlap them
        self.btn_bases = ({(hot, paused): self._render_buttons(hot, paused)
                           for hot in (None, "cancel", "pause")
                           for paused in (False, True)}
                          if self.buttons_on else {})
        self.fade = self._fade_mask()
        self._animate()

    def _render_base(self):
        """The pill itself (shadow, body, rim) rendered once at SS
        resolution; the waves are drawn on a copy of it every frame."""
        s = self.SS
        w, h = self.W * s, self.H * s
        m, r = self.m * s, self.ph * s / 2
        box = (m, m, w - m, h - m)

        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (box[0] + s, box[1] + 3 * s, box[2] - s, box[3] + 3 * s),
            radius=r, fill=self.SHADOW)
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(4 * s)))

        t = np.linspace(0.0, 1.0, h)[:, None]
        rgb = (np.array(self.FILL_TOP) * (1 - t)
               + np.array(self.FILL_BOTTOM) * t)          # h x 3
        arr = np.zeros((h, w, 4), np.uint8)
        arr[:, :, :3] = rgb[:, None, :].astype(np.uint8)
        body = Image.fromarray(arr, "RGBA")
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(box, radius=r, fill=255)
        body.putalpha(mask.point(lambda v: v * self.FILL_ALPHA // 255))
        img.alpha_composite(body)

        rim = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(rim).rounded_rectangle(
            box, radius=r, outline=self.EDGE,
            width=max(1, round(1.2 * self.k)) * s)
        img.alpha_composite(rim)
        return img

    def _render_buttons(self, hot, paused):
        """The pill with its X and red button drawn in; `hot` is the button
        under the mouse. The X is a quiet translucent disc — cancelling
        should never be what the eye lands on first. The red button shows
        what a click does: pause (two bars) while recording, play (a
        triangle) while paused."""
        s, k = self.SS, self.k
        img = self.base.copy()
        r = self.BTN_R * k * s
        xc, yc = (v * s for v in self.btn_centers["cancel"])
        xs, ys = (v * s for v in self.btn_centers["pause"])

        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.ellipse((xc - r, yc - r, xc + r, yc + r),
                  fill=self.CANCEL_FILL_HOT if hot == "cancel"
                  else self.CANCEL_FILL)
        icon = self.ICON_HOT if hot == "cancel" else self.ICON
        a, lw = r * 0.33, 1.6 * k * s
        for sy in (1, -1):
            d.line((xc - a, yc - sy * a, xc + a, yc + sy * a), fill=icon,
                   width=round(lw))
        for px in (xc - a, xc + a):              # round caps on the X
            for py in (yc - a, yc + a):
                d.ellipse((px - lw / 2, py - lw / 2, px + lw / 2,
                           py + lw / 2), fill=icon)
        img.alpha_composite(layer)

        if hot == "pause":
            glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
            g = r + 1.5 * k * s
            ImageDraw.Draw(glow).ellipse((xs - g, ys - g, xs + g, ys + g),
                                         fill=self.RED_GLOW)
            img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(
                3.2 * k * s)))
        top, bottom = ((self.RED_TOP_HOT, self.RED_BOTTOM_HOT)
                       if hot == "pause" else (self.RED_TOP, self.RED_BOTTOM))
        w, h = img.size
        t = np.clip((np.arange(h) - (ys - r)) / (2 * r), 0, 1)[:, None]
        arr = np.zeros((h, w, 4), np.uint8)
        arr[:, :, :3] = (np.array(top) * (1 - t) + np.array(bottom) * t
                         ).astype(np.uint8)[:, None, :]
        disc = Image.fromarray(arr, "RGBA")
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).ellipse((xs - r, ys - r, xs + r, ys + r),
                                     fill=255)
        disc.putalpha(mask)
        img.alpha_composite(disc)

        icon = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(icon)
        white = (255, 255, 255, 255)
        if paused:
            # play, nudged right: a triangle centred on its box looks like
            # it leans left, because its visual weight sits near the base
            tw, th = r * 0.68, r * 0.78
            x0 = xs - tw / 2 + tw * 0.12
            pts = [(x0, ys - th / 2), (x0 + tw, ys), (x0, ys + th / 2)]
            d.polygon(pts, fill=white)
            d.line(pts + pts[:2], fill=white, width=round(0.9 * k * s),
                   joint="curve")                  # softened corners
        else:
            bw, bh, gap = r * 0.22, r * 0.72, r * 0.26
            for x in (xs - gap / 2 - bw, xs + gap / 2):
                d.rounded_rectangle((x, ys - bh / 2, x + bw, ys + bh / 2),
                                    radius=bw * 0.45, fill=white)
        img.alpha_composite(icon)
        return img

    def _fade_mask(self):
        """Horizontal alpha ramp so the waves dissolve before they reach the
        rim (or the buttons). Without it every stroke ends in a hard flat
        stub against the edge of the pill."""
        s = self.SS
        x0 = self.wave_x0 * s
        span = self.wave_span * s
        ramp = np.zeros(self.W * s, dtype=np.float64)
        u = (np.arange(self.W * s) - x0) / span              # 0..1 inside
        edge = np.clip(np.minimum(u, 1 - u) / self.FADE, 0, 1)
        ramp[:] = np.where((u >= 0) & (u <= 1), edge, 0)
        ramp = np.sin(ramp * math.pi / 2) ** 2              # smoothstep-ish
        col = (ramp * 255).astype(np.uint8)
        return Image.fromarray(np.tile(col, (self.H * s, 1)), "L")

    def _place(self):
        """Park the pill at the bottom center of the CURRENT screen.
        The resolution can change long after startup (a fullscreen game, a
        monitor unplugged, a display-scale change); geometry fixed at launch
        then leaves the pill off-screen and the waveform simply stops being
        visible. GetSystemMetrics is always live, unlike Tk's cached
        winfo_screenwidth."""
        try:
            sw = user32.GetSystemMetrics(0)     # SM_CXSCREEN
            sh = user32.GetSystemMetrics(1)     # SM_CYSCREEN
        except Exception:
            sw = sh = 0
        if not sw or not sh:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if (sw, sh) == self._screen:
            return
        self._screen = (sw, sh)
        # GAP is measured to the pill, not to the shadow padding around it
        bottom = int(self.GAP * self.k) - self.m
        x, y = (sw - self.W) // 2, sh - self.H - bottom
        self._origin = (x, y)          # to map the mouse onto the buttons
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

    def _blit(self, img):
        self.surface.push(img.resize((self.W, self.H), Image.LANCZOS))

    def _prevent_focus_steal(self):
        WS_EX_LAYERED = 0x00080000
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                              style | WS_EX_LAYERED | WS_EX_TRANSPARENT
                              | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

    # ---- buttons ----
    def _hook_clicks(self):
        """Subclass the pill's window procedures (Tk's outer frame and its
        inner window) so its clicks come to _on_message first."""
        self._procs = []               # the callbacks must outlive the windows
        for hwnd in dict.fromkeys((self.hwnd, self.root.winfo_id())):
            prev = []

            def proc(h, msg, wp, lp, prev=prev):
                try:
                    handled = self._on_message(msg, lp)
                except Exception as e:     # an error must never eat input
                    print(f"[overlay] {type(e).__name__}: {e}")
                    handled = None
                if handled is not None:
                    return handled
                if not prev:
                    return user32.DefWindowProcW(h, msg, wp, lp)
                return user32.CallWindowProcW(prev[0], h, msg, wp, lp)

            cb = WNDPROC(proc)
            old = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC,
                                           ctypes.cast(cb, ctypes.c_void_p))
            if not old:
                raise ctypes.WinError(ctypes.get_last_error())
            prev.append(old)
            self._procs.append(cb)

    def _on_message(self, msg, lp):
        """Returns a result to swallow the message, or None to pass it on."""
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE       # never take focus from the paste target
        if msg in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK, WM_LBUTTONUP):
            x = ctypes.c_short(lp & 0xFFFF).value
            y = ctypes.c_short((lp >> 16) & 0xFFFF).value
            hit = (self._button_at(x, y)
                   if self.mode in ("recording", "paused") else None)
            if msg != WM_LBUTTONUP:
                self._pressed = hit
            else:
                # a button fires on release, and only if the mouse went down
                # on that same button: sliding off it is how you back out
                pressed, self._pressed = self._pressed, None
                if hit and hit == pressed:
                    # _animate delivers it. Calling into Tk from inside a
                    # window procedure — even root.after() — clobbers
                    # tkinter's saved thread state and Python aborts with a
                    # fatal PyEval_RestoreThread error
                    self._clicked = hit
            return 0                   # the pill eats its own clicks
        return None

    def _button_at(self, x, y):
        """Which button is at window pixel (x, y), if any."""
        reach = (self.BTN_R + self.BTN_REACH) * self.k
        for name, (cx, cy) in self.btn_centers.items():
            if (x - cx) ** 2 + (y - cy) ** 2 <= reach * reach:
                return name
        return None

    def _set_clickable(self, on):
        """Only a pill showing its buttons may catch clicks; otherwise they
        go straight through to whatever is underneath."""
        on = on and self.buttons_on
        if on == self._clickable:
            return
        self._clickable = on
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                              style & ~WS_EX_TRANSPARENT if on
                              else style | WS_EX_TRANSPARENT)

    def _track_pointer(self):
        pt = POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            self.hover = self._button_at(pt.x - self._origin[0],
                                         pt.y - self._origin[1])

    # ---- modes ----
    def show_recording(self):
        self.amp = 0.0
        self.mode = "recording"
        self.hover = self._pressed = self._clicked = None
        self._place()
        self._set_clickable(True)
        self.root.deiconify()
        self.root.attributes("-topmost", True)

    def show_paused(self):
        """Mic off: the red button turns into play, the wave settles flat.
        The pill keeps taking clicks — to resume or to cancel."""
        self.mode = "paused"
        self._pressed = None

    def show_processing(self):
        self.mode = "processing"
        self.hover = self._pressed = None
        self._click_until = time.time() + self.CLICK_GRACE

    def show_speaking(self):
        self.amp = 0.0
        self.mode = "speaking"
        self.hover = self._pressed = None
        self._place()
        self._set_clickable(False)
        self.root.deiconify()
        self.root.attributes("-topmost", True)

    def hide(self):
        self.mode = "hidden"
        self.hover = self._pressed = None
        self._set_clickable(False)
        self.root.withdraw()

    def _deliver_click(self):
        clicked, self._clicked = self._clicked, None
        cb = {"pause": self.on_pause, "cancel": self.on_cancel}.get(clicked)
        if cb and self.mode in ("recording", "paused"):
            try:
                cb()
            except Exception as e:     # must not kill the animation loop
                print(f"[overlay] {clicked}: {type(e).__name__}: {e}")

    def _animate(self):
        self._deliver_click()
        if self.mode != "hidden":
            self._place()      # the screen can change while the pill is up
        if self.mode in ("recording", "paused"):
            if self.buttons_on:
                self._track_pointer()
            self._draw_wave(self.LAYERS, self.get_level(), 12)
        elif self.mode == "speaking":
            self._draw_wave(self.SPEAK_LAYERS, self.get_speak_level(), 5)
        elif self.mode == "processing":
            if self._clickable and time.time() > self._click_until:
                self._set_clickable(False)
            self._draw_dots()
        self.root.after(30, self._animate)

    def _draw_wave(self, layers, level, gain):
        now = time.time()
        paused = self.mode == "paused"
        # smooth the loudness so the waves swell and settle, not twitch;
        # paused, the mic is off whatever it hears, so they flatten out
        target_amp = 0.0 if paused else min(1.0, (level * gain) ** 0.6)
        self.amp += (target_amp - self.amp) * 0.35
        # idle floor: waves keep rippling gently so it reads as "listening"
        amp = (self.amp if paused
               else max(self.amp, 0.10 + 0.04 * math.sin(now * 1.8)))

        s = self.SS
        img = (self.btn_bases[(self.hover, paused)].copy()
               if self.mode in ("recording", "paused") and self.buttons_on
               else self.base.copy())
        # the halo and the crisp strokes go on their own transparent layers:
        # drawing a translucent color straight onto the pill would blend its
        # ALPHA down too, punching see-through holes in the body
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        crisp = Image.new("RGBA", img.size, (0, 0, 0, 0))
        dg, dc = ImageDraw.Draw(glow), ImageDraw.Draw(crisp)
        cy = self.H * s / 2
        max_a = (self.ph - 9 * self.k) * s / 2
        x0 = self.wave_x0 * s
        span = self.wave_span * s
        for rel_a, cycles, speed, width, rgb in layers:
            pts = []
            for i in range(self.POINTS + 1):
                u = i / self.POINTS                     # 0..1 across pill
                envelope = 1.0 - (2 * u - 1) ** 2       # pinch at the edges
                y = cy + max_a * amp * rel_a * envelope ** 1.4 \
                    * math.sin(2 * math.pi * (cycles * u + speed * now))
                pts.append((x0 + u * span, y))
            # width is applied at supersampled resolution, so a stroke can be
            # a fraction of a pixel wide once the frame is scaled back down
            w = max(2, round(width * self.k * s))
            if rel_a >= 0.7:      # the bright layers carry a soft halo
                dg.line(pts, fill=rgb + (44,), width=round(w * 2.6),
                        joint="curve")
            dc.line(pts, fill=rgb + (255,), width=w, joint="curve")
        glow.alpha_composite(crisp)
        alpha = ImageChops.multiply(glow.getchannel("A"), self.fade)
        if paused:
            # a dim line that slowly breathes: the mic is off, but the app
            # is not frozen
            dim = 0.30 + 0.08 * math.sin(now * 2.4)
            alpha = alpha.point(lambda v: int(v * dim))
        glow.putalpha(alpha)
        img.alpha_composite(glow)
        self._blit(img)

    def _draw_dots(self):
        s = self.SS
        img = self.base.copy()
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        cx, cy = self.W * s / 2, self.H * s / 2
        # each dot breathes through a CONTINUOUS colour and size ramp; the
        # old version snapped between four fixed shades, which read as a
        # jerky, cheap blink
        base_r = self.ph * s * 0.105
        gap = base_r * 4.0
        phase = time.time() * 3.4
        dim, lit = (104, 106, 128), (223, 225, 255)
        for i in range(3):
            # a third of the cycle apart, so one dot is always at its peak
            # and the light walks left-to-right instead of all three fading
            # out together
            b = (math.sin(phase - i * (2 * math.pi / 3)) + 1) / 2
            b = b * b * (3 - 2 * b)                    # smoothstep
            col = tuple(round(dim[c] + (lit[c] - dim[c]) * b) for c in range(3))
            r = base_r * (0.78 + 0.34 * b)
            x = cx + (i - 1) * gap
            halo = r * 2.5
            d.ellipse((x - halo, cy - halo, x + halo, cy + halo),
                      fill=col + (round(34 * b),))
            d.ellipse((x - r, cy - r, x + r, cy + r), fill=col + (255,))
        img.alpha_composite(layer)
        self._blit(img)


# ------------------------------------------------------------------ tray ---

class Tray:
    """System tray icon (next to the clock) so LocalFlow can run in the
    background with no console window. Right-click -> Sair to quit."""

    def __init__(self, on_quit):
        import pystray
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 14, 62, 50), radius=18, fill="#1b1b21",
                            outline="#6c70a8", width=3)
        pts = [(10 + i * 44 / 20,
                32 - 8 * math.sin(i / 20 * math.pi) ** 1.4
                * math.sin(i / 20 * 3 * math.pi)) for i in range(21)]
        d.line(pts, fill="#dfe1ff", width=4)
        menu = pystray.Menu(
            pystray.MenuItem("Ctrl+Win: ditar   Ctrl+Alt+S: ler",
                             None, enabled=False),
            pystray.MenuItem("Sair", lambda icon, item: on_quit()))
        self.icon = pystray.Icon("LocalFlow", img, "LocalFlow", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


def silence_window(auto, elapsed):
    """How long a pause may last before the recording is considered over.

    A one-line dictation should paste the moment you stop talking, but
    people composing a long thought out loud pause for two or three seconds
    mid-sentence — with a single short window those recordings were cut in
    half. So the window stays at silence_ms while the dictation is still
    short, then grows to silence_ms_long by long_after_sec: anyone still
    talking that far in is composing, not answering."""
    short = auto["silence_ms"] / 1000
    long_ms = auto.get("silence_ms_long")
    after = auto.get("long_after_sec", 12)
    if not long_ms or after <= 0:
        return short
    grown = max(short, long_ms / 1000)
    ramp = (elapsed - after / 2) / (after / 2)      # 0 at half time, 1 at full
    return short + (grown - short) * min(1.0, max(0.0, ramp))


def already_running():
    """One LocalFlow at a time — two instances would fight over hotkeys."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, "LocalFlow_single_instance")
    return ctypes.get_last_error() == 183      # ERROR_ALREADY_EXISTS


# --------------------------------------------------------------- cleanup ---

CLEANUP_SYSTEM_PROMPT = """\
You clean up raw speech-to-text transcripts for dictation.
Rules:
- Remove filler words and false starts (um, uh, hmm, "tipo", "né" as filler)
  and collapse accidentally repeated words ("pra pra" -> "pra").
- Fix punctuation and capitalization.
- If the speaker dictates a list, format it as a list.
- Change as little as possible: keep the speaker's exact words. Never
  replace a word with a synonym, never rewrite times or numbers
  ("três da tarde" stays "três da tarde", never "15h").
- Keep the speaker's language. Do not translate.
- Never answer questions or add content: if the transcript is a question,
  output the cleaned-up question itself.
- Output ONLY the cleaned text, nothing else."""

# few-shot examples teach small models the exact behavior, especially in
# Portuguese where they tend to skip punctuation and list formatting
CLEANUP_EXAMPLES = [
    ("um so like I think we should uh maybe move the meeting to to Thursday",
     "So I think we should maybe move the meeting to Thursday."),
    ("é tipo eu queria saber se se você pode uh me mandar o relatório até "
     "até sexta né",
     "Eu queria saber se você pode me mandar o relatório até sexta."),
    ("primeiro item comprar leite segundo item pagar a conta de luz "
     "terceiro item ligar pro dentista",
     "1. Comprar leite\n2. Pagar a conta de luz\n3. Ligar pro dentista"),
]


def split_for_cleanup(text, limit):
    """Break a transcript into pieces of at most `limit` characters.

    gemma3:4b reproduces a few hundred characters faithfully but starts
    dropping whole sentences from longer input — measured: an 873-character
    transcript came back missing its last sentence, the same text in two
    pieces came back complete. Splits on sentence ends where possible."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    pieces = []
    for piece in re.split(r"(?<=[.!?…])\s+", text):
        # dictation often runs on for a whole paragraph with no punctuation
        # at all — cut those on word boundaries, into equal parts so one
        # request isn't left with a stub
        n = math.ceil(len(piece) / limit)
        if n > 1:
            size = math.ceil(len(piece) / n)
            while len(piece) > size:
                cut = piece.rfind(" ", 0, size + 1)
                if cut <= size // 2:            # one absurdly long word
                    cut = size
                pieces.append(piece[:cut].strip())
                piece = piece[cut:].strip()
        if piece:
            pieces.append(piece)
    # then fill each request as full as it may go: every extra request costs
    # ~3s of fixed overhead
    parts, buf = [], ""
    for piece in pieces:
        if not buf or len(buf) + len(piece) + 1 <= limit:
            buf = f"{buf} {piece}".strip()
        else:
            parts.append(buf)
            buf = piece
    if buf:
        parts.append(buf)
    return parts


def dropped_words(raw, cleaned):
    """How much of what the speaker said did the cleanup throw away?

    Returns (longest run of consecutive words dropped, words dropped off
    the very end). Removing fillers deletes a word or two at a time, so a
    long run — or anything missing at the end — means the model skipped
    content instead of cleaning it."""
    a = re.findall(r"[\w%]+", raw.lower(), re.UNICODE)
    b = re.findall(r"[\w%]+", cleaned.lower(), re.UNICODE)
    worst = tail = 0
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            worst = max(worst, i2 - i1)
            if i2 == len(a):                 # ran off the end of the raw text
                tail = i2 - i1
    return worst, tail


# a cleanup that swallows this many words in a row is skipping content, not
# removing fillers; at the end of the text even a few words is a lost thought
MAX_DROP_RUN = 6
MAX_DROP_TAIL = 3


def resolve_ollama_url(url):
    """Point the URL at an address that actually answers.

    On Windows "localhost" resolves to ::1 before 127.0.0.1, and Ollama
    listens on IPv4 only — so every request spent ~2 seconds waiting for the
    IPv6 connection to fail before falling back. Measured on this machine:
    2058 ms to connect to localhost, 0.3 ms to 127.0.0.1, for a request the
    server answers in 200 ms. Only loopback names are touched; a real host
    is left exactly as configured."""
    m = re.match(r"^(https?://)(localhost)(:\d+)?(/.*)?$", url.strip(), re.I)
    if not m:
        return url
    scheme, _, port, path = m.groups()
    for host, family in (("127.0.0.1", "127.0.0.1"), ("[::1]", "::1")):
        try:
            with socket.create_connection(
                    (family, int((port or ":80")[1:])), timeout=0.4):
                return f"{scheme}{host}{port or ''}{path or ''}"
        except OSError:
            continue
    return url        # nothing answered: leave it alone and let it fail loudly


class OllamaCleaner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.available = False
        self._next_probe = 0.0
        # one session for the whole run: re-opening the socket on every
        # dictation re-pays connection setup for nothing
        self.http = requests.Session()
        self.url = resolve_ollama_url(cfg["url"])
        if self.url != cfg["url"]:
            print(f"[ollama] using {self.url} (skips a ~2s IPv6 timeout)")
        if not cfg["enabled"]:
            return
        if not self._probe():
            print("[ollama] not ready yet — will keep retrying in the "
                  "background")

    def _probe(self):
        """Check whether the server is up and has the model. Called again
        from clean() because Ollama may boot slower than we do."""
        self._next_probe = time.time() + 20
        # Ollama is usually still booting when LocalFlow starts, so the
        # address could not be resolved yet — try again now that it may answer
        if "localhost" in self.url:
            self.url = resolve_ollama_url(self.url)
        try:
            r = self.http.get(self.url + "/api/tags", timeout=3)
            models = [m["name"] for m in r.json().get("models", [])]
            self.available = any(
                m == self.cfg["model"] or m.startswith(self.cfg["model"] + ":")
                for m in models)
            if not self.available:
                print(f"[ollama] model {self.cfg['model']!r} not found "
                      f"(installed: {models}) — cleanup disabled")
        except requests.RequestException:
            self.available = False
        return self.available

    def warmup(self):
        """Load the model into VRAM in the background so the first real
        dictation doesn't hit a cold-start timeout."""
        if not self.available:
            return

        def _go():
            try:
                self.http.post(
                    self.url + "/api/generate",
                    json={"model": self.cfg["model"], "prompt": "hi",
                          "stream": False, "keep_alive": "2h"},
                    timeout=120)
                print("[ollama] model warm")
            except requests.RequestException:
                pass

        threading.Thread(target=_go, daemon=True).start()

    def clean(self, text, lang=None):
        if not text.strip():
            return text
        if not self.available:
            if (not self.cfg["enabled"] or time.time() < self._next_probe
                    or not self._probe()):
                return text
            print("[ollama] server is up now — cleanup enabled")
            self.warmup()
            return text   # skip this one: the model is still cold
        chunks = split_for_cleanup(text, self.cfg.get("chunk_chars", 600))
        if len(chunks) > 1:
            print(f"[ollama] long dictation ({len(text)} chars) — cleaning in "
                  f"{len(chunks)} parts")
        deadline = time.time() + self.cfg.get("budget_sec", 40)
        out = []
        for i, chunk in enumerate(chunks):
            # past the budget (or cleanup just got disabled by a timeout)
            # keep the speaker's own words rather than stalling the paste
            if not self.available or time.time() > deadline:
                if i:
                    print(f"[ollama] out of time — parts {i + 1}-{len(chunks)}"
                          f" pasted as spoken")
                out.extend(chunks[i:])
                break
            out.append(self._clean_chunk(chunk, lang))
        return " ".join(out)

    def _clean_chunk(self, text, lang):
        """One request. Returns the raw text unchanged if anything about the
        answer looks wrong — a bad cleanup is worse than no cleanup."""
        system = CLEANUP_SYSTEM_PROMPT
        if lang in ("pt", "en"):
            name = "Portuguese" if lang == "pt" else "English"
            system += (f"\n- This transcript is in {name}. The output MUST "
                       f"be in {name} too — translating it is an error.")
        messages = [{"role": "system", "content": system}]
        for raw, cleaned in CLEANUP_EXAMPLES:
            messages.append({"role": "user", "content": raw})
            messages.append({"role": "assistant", "content": cleaned})
        messages.append({"role": "user", "content": text})
        try:
            r = self.http.post(
                self.url + "/api/chat",
                json={
                    "model": self.cfg["model"],
                    "stream": False,
                    "keep_alive": "2h",
                    "options": {"temperature": 0.1},
                    "messages": messages,
                },
                timeout=self.cfg["timeout_sec"])
            r.raise_for_status()
            cleaned = r.json()["message"]["content"].strip()
            if not cleaned:
                return text
            # safety net: if the model translated anyway, keep the raw text
            if lang in ("pt", "en"):
                pt, en = pt_en_scores(cleaned)
                got = "pt" if pt > en + 1 else "en" if en > pt + 1 else lang
                if got != lang:
                    print("[ollama] cleanup changed the language — "
                          "using raw transcript")
                    return text
            # safety net: small models quietly skip sentences on long input.
            # Losing words the speaker actually said is the worst outcome,
            # so an incomplete cleanup is thrown away entirely.
            worst, tail = dropped_words(text, cleaned)
            if worst >= MAX_DROP_RUN or tail >= MAX_DROP_TAIL:
                where = "the ending" if tail >= MAX_DROP_TAIL else "a passage"
                print(f"[ollama] cleanup dropped {where} "
                      f"({max(worst, tail)} words) — using raw transcript")
                return text
            return cleaned
        except (requests.RequestException, KeyError) as e:
            print(f"[ollama] cleanup failed ({e}) — using raw transcript")
            if isinstance(e, requests.Timeout):
                # generation is wedged (usually another app hogging the
                # GPU) — stop stalling every dictation, retry in 2 min
                self.available = False
                self._next_probe = time.time() + 120
                print("[ollama] pausing cleanup for 2 min (GPU busy?)")
            return text


# ----------------------------------------------------- language guessing ---

# frequent words used to tell PT from EN text: picks the right neural voice
# for read-aloud, and catches the cleanup LLM translating by accident
PT_HINT = frozenset(
    "de que não uma para com você é em um eu mais por se na no como mas"
    " ele ela seu sua ou ser muito já está também isso essa esse meu"
    " minha tenho fazer pode bem aqui agora então tudo obrigado sim nós"
    " foi são tem vai da do dos das ao aos à às pelo pela até depois"
    " quando onde porque coisa dia hoje amanhã".split())
EN_HINT = frozenset(
    "the be to of and that have it for not on with he as you do at this"
    " but his by from they we say her she or an will my one all would"
    " there their what out about who get which when make can like time"
    " just know take into your good some could them see other than then"
    " now only come think also back after use two how our work well way"
    " even new want any these give day most".split())


def pt_en_scores(text):
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    pt = sum(w in PT_HINT for w in words)
    en = sum(w in EN_HINT for w in words)
    if any(c in text.lower() for c in "ãõçáéíóúâêôà"):
        pt += 3                          # accents are a strong PT signal
    return pt, en


# ------------------------------------------------------------ read aloud ---

def get_selected_text():
    """Copy the current selection via a synthetic Ctrl+C and return it.
    Falls back to whatever text was already on the clipboard if nothing
    is selected. The user's clipboard is restored afterwards."""
    wait_modifiers_released()
    try:
        old = pyperclip.paste()
    except pyperclip.PyperclipException:
        old = ""
    try:
        pyperclip.copy("")            # so we can tell whether Ctrl+C copied
    except pyperclip.PyperclipException:
        return old.strip()
    send_ctrl_c()
    release_all_modifiers()           # don't leave Ctrl stuck in the app
    text = ""
    deadline = time.time() + 0.5      # apps fill the clipboard asynchronously
    while time.time() < deadline:
        time.sleep(0.04)
        try:
            text = pyperclip.paste()
        except pyperclip.PyperclipException:
            text = ""
        if text:
            break
    if old:
        try:
            pyperclip.copy(old)
        except pyperclip.PyperclipException:
            pass
    return (text or old).strip()


class Speaker:
    """Reads text aloud with Piper (local neural TTS). Piper streams raw
    PCM sentence by sentence, so speech starts well before long texts are
    fully synthesized. Falls back to the built-in Windows voice (SAPI)
    when Piper or its voice model is missing."""

    MAX_CHARS = 20000

    def __init__(self, cfg):
        self.cfg = cfg
        base = Path(__file__).parent / "piper"
        self.exe = base / "piper" / "piper.exe"
        self.voices = {}                 # lang -> (onnx path, sample rate)
        for lang, name in cfg["voices"].items():
            path = base / "voices" / (name + ".onnx")
            if not path.is_file():
                continue
            sr = 22050
            try:
                meta = json.loads(Path(str(path) + ".json")
                                  .read_text(encoding="utf-8"))
                sr = int(meta["audio"]["sample_rate"])
            except Exception:
                pass
            self.voices[lang] = (path, sr)
        self.piper_ok = self.exe.is_file() and bool(self.voices)
        self.active = False
        self.level = 0.0
        self._stop = threading.Event()
        self._proc = None

    def _guess_lang(self, text):
        pt, en = pt_en_scores(text)
        lang = "pt" if pt >= en else "en"
        return lang if lang in self.voices else next(iter(self.voices), "pt")

    def speak_selection(self, sounds):
        """Grab the selection and start speaking it (returns immediately)."""
        self.active = True
        self._stop.clear()

        def _go():
            try:
                text = get_selected_text()[:self.MAX_CHARS]
                if not text:
                    print("[read] nothing selected and clipboard is empty")
                    sounds.nospeech()
                    return
                preview = text[:60].replace("\n", " ")
                lang = self._guess_lang(text)
                print(f"[read] speaking {len(text)} chars ({lang}): "
                      f"{preview}" + ("…" if len(text) > 60 else ""))
                if self.piper_ok:
                    self._piper_speak(text, lang)
                else:
                    self._sapi_speak(text, lang)
            except Exception as e:
                print(f"[read] error: {type(e).__name__}: {e}")
            finally:
                self.level = 0.0
                self.active = False

        threading.Thread(target=_go, daemon=True).start()

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _piper_speak(self, text, lang):
        voice, sr = self.voices[lang]
        cmd = [str(self.exe), "--model", str(voice), "--output_raw",
               "--length_scale", f"{1.0 / max(0.5, self.cfg['speed']):.2f}",
               "--sentence_silence", "0.25"]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self._proc = proc

        def _feed():                    # own thread: avoids pipe deadlock
            try:
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass

        threading.Thread(target=_feed, daemon=True).start()

        stream = sd.OutputStream(samplerate=sr, channels=1,
                                 dtype="int16", latency="low")
        stream.start()
        try:
            while not self._stop.is_set():
                # ~46ms chunks + low latency keep the overlay wave in sync
                # with the words instead of lagging behind them
                chunk = proc.stdout.read(2048)
                if not chunk:
                    break
                data = np.frombuffer(chunk, dtype=np.int16)
                norm = data.astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(norm ** 2)))
                # fast attack, slow decay — same feel as the mic wave
                self.level = max(rms, self.level * 0.86)
                stream.write(data)
        finally:
            if proc.poll() is None:
                proc.kill()
            self._proc = None
            try:
                stream.abort()
                stream.close()
            except Exception:
                pass

    def _sapi_speak(self, text, lang):
        """Fallback: Windows built-in voice (Maria pt-BR / Zira en-US)."""
        culture = "pt-BR" if lang == "pt" else "en-US"
        tmp = Path(os.environ.get("TEMP", ".")) / "localflow_read.txt"
        tmp.write_text(text, encoding="utf-8")
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "try { $s.SelectVoiceByHints(0, 0, 0, "
            f"[System.Globalization.CultureInfo]'{culture}') }} catch {{}}; "
            f"$s.Speak([IO.File]::ReadAllText('{tmp}', "
            "[Text.Encoding]::UTF8))"
        )
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps],
            creationflags=subprocess.CREATE_NO_WINDOW)
        self._proc = proc
        try:
            while proc.poll() is None and not self._stop.is_set():
                # no PCM to measure, so give the overlay a gentle pulse
                self.level = 0.05 + 0.03 * math.sin(time.time() * 4)
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                proc.kill()
            self._proc = None


# -------------------------------------------------------------- pipeline ---

def _clipboard_copy(text, tries=6):
    """The Windows clipboard is a single shared resource; another app
    polling it (password managers, clipboard managers, browsers) briefly
    locks it and pyperclip.copy raises. Retry a few times before giving up."""
    for i in range(tries):
        try:
            pyperclip.copy(text)
            return True
        except pyperclip.PyperclipException:
            time.sleep(0.04)
    return False


def inject(text, mode):
    wait_modifiers_released()
    if mode == "type":
        type_unicode(text)
        release_all_modifiers()
        return
    try:
        old = pyperclip.paste()
    except pyperclip.PyperclipException:
        old = None
    if not _clipboard_copy(text):
        # clipboard stayed locked — type the text directly instead of
        # raising an error and pasting nothing into the user's field
        print("[inject] clipboard busy — typing the text directly")
        type_unicode(text)
        release_all_modifiers()
        return
    time.sleep(0.05)
    send_ctrl_v()
    time.sleep(0.5)
    release_all_modifiers()   # never leave a modifier stuck in the target app
    # restoring '' would clobber non-text clipboard content (a copied image
    # reads back as empty text), so only restore real text
    if old:
        try:
            pyperclip.copy(old)
        except pyperclip.PyperclipException:
            pass


class App:
    def __init__(self):
        self.cfg = load_config()
        self.hotkey_vks = [VK[k] for k in self.cfg["hotkey"]]
        hands_free = self.cfg["activation"] == "toggle"
        self.cancel_vks = ([0x1B] if hands_free and self.cfg["esc_cancels"]
                           else [])                          # Esc
        self.sounds = Sounds(self.cfg["sounds"], self.cfg["sound_volume"])
        self.results = queue.Queue()
        self.state = "idle"
        self.tail_until = 0.0
        self.prev_down = False
        self.prev_esc = False
        self.rec_start = 0.0
        self.paused_at = 0.0
        self.speech_seen = False
        self.last_voice = 0.0
        self.noise_floor = 0.0

        rcfg = self.cfg["read_aloud"]
        self.read_vks = ([VK[k] for k in rcfg["hotkey"]]
                         if rcfg["enabled"] else [])
        self.prev_read_down = False
        self.speaker = Speaker(rcfg)
        self._silent_cancels = 0
        self._quit = False
        self.tray = Tray(self._request_quit)

        print("LocalFlow starting…")
        wcfg = self.cfg["whisper"]
        device = wcfg["device"]
        if device == "auto":
            try:
                import ctranslate2
                device = ("cuda" if ctranslate2.get_cuda_device_count() > 0
                          else "cpu")
            except Exception:
                device = "cpu"
        compute = "float16" if device == "cuda" else "int8"
        print(f"[whisper] loading {wcfg['model']!r} on {device} ({compute})…")
        t0 = time.time()
        self.model = WhisperModel(wcfg["model"], device=device,
                                  compute_type=compute)
        # warm up so the first real dictation isn't slow
        list(self.model.transcribe(np.zeros(WHISPER_SR, dtype=np.float32),
                                   beam_size=1)[0])
        print(f"[whisper] ready in {time.time() - t0:.1f}s")

        self.cleaner = OllamaCleaner(self.cfg["ollama"])
        if self.cleaner.available:
            print(f"[ollama] cleanup enabled with "
                  f"{self.cfg['ollama']['model']!r}")
            self.cleaner.warmup()

        self.recorder = Recorder(self.cfg["preroll_ms"])
        print(f"[mic] recording at {self.recorder.sr} Hz native")
        # language: "auto" (free detection), "pt" (fixed), or a list like
        # ["pt", "en"] — detect, but only among those languages, which is
        # far more reliable than free detection on short clips
        lang = wcfg["language"]
        if isinstance(lang, str) and "," in lang:
            lang = [x.strip() for x in lang.split(",")]
        self.language = None if lang == "auto" else lang

        self.root = tk.Tk()
        buttons = hands_free and self.cfg["pill_buttons"]
        self.overlay = Overlay(
            self.root, lambda: self.recorder.level, lambda: self.speaker.level,
            on_pause=self._on_pause_click if buttons else None,
            on_cancel=self._on_cancel_click if buttons else None)

        hotkey_name = "+".join(self.cfg["hotkey"])
        if self.cfg["activation"] == "toggle":
            print(f"\nPress {hotkey_name} and speak; pause (or press again) "
                  f"to finish. Ctrl+C here to quit.")
            if self.overlay.buttons_on:
                print("While recording: the red button in the pill pauses "
                      "and resumes, the X (or Esc) cancels.")
            elif self.cancel_vks:
                print("While recording: Esc cancels.")
        else:
            print(f"\nHold {hotkey_name} and speak; release to transcribe. "
                  f"Ctrl+C here to quit.")
        if self.read_vks:
            engine = ("Piper" if self.speaker.piper_ok
                      else "Windows voice (Piper not found)")
            print(f"Press {'+'.join(rcfg['hotkey'])} to read the selected "
                  f"text aloud ({engine}); press again to stop.\n")
        else:
            print()

    # ---- worker (background thread) ----
    def _process(self, audio):
        # the state machine waits on self.results, so a result must be
        # delivered no matter what fails here or the app hangs on the dots
        result = "nospeech"
        try:
            result = self._transcribe_and_paste(audio)
        except Exception as e:
            print(f"[error] {type(e).__name__}: {e}")
        finally:
            self.results.put(result)

    def _transcribe_and_paste(self, audio):
        cfg, wcfg = self.cfg, self.cfg["whisper"]
        dur = len(audio) / WHISPER_SR
        if dur < cfg["min_speech_sec"]:
            return "nospeech"
        # gentle gain for quiet mics
        peak = float(np.max(np.abs(audio)))
        if 0.001 < peak < 0.3:
            audio = audio * min(10.0, 0.5 / peak)
        t = time.time()
        language = self.language
        if isinstance(language, list):
            _, _, probs = self.model.detect_language(audio, vad_filter=True)
            ranked = sorted(((p, l) for l, p in probs if l in language),
                            reverse=True)
            conf, language = ranked[0] if ranked else (1.0, language[0])
        raw, score = self._run_whisper(audio, language, wcfg)
        # if detection was unsure, also transcribe as the runner-up language
        # and keep whichever Whisper itself scored higher — a wrong forced
        # language makes Whisper mistranslate the speech
        if (isinstance(self.language, list) and len(ranked) > 1
                and conf < 0.75):
            alt = ranked[1][1]
            alt_raw, alt_score = self._run_whisper(audio, alt, wcfg)
            if alt_score > score:
                print(f"[stt] detection said {language} ({conf:.0%}) but "
                      f"{alt} transcribes better — using {alt}")
                raw, language = alt_raw, alt
        stt_ms = (time.time() - t) * 1000
        if not raw:
            print("[stt] (no speech detected)")
            return "nospeech"
        print(f"[stt {stt_ms:.0f}ms {language}] {raw}")
        t = time.time()
        text = self.cleaner.clean(raw, lang=language)
        if text != raw:
            print(f"[llm {(time.time() - t) * 1000:.0f}ms] {text}")
        inject(text, cfg["inject_mode"])
        return "done"

    def _run_whisper(self, audio, language, wcfg):
        segments, _ = self.model.transcribe(
            audio, language=language, beam_size=wcfg["beam_size"],
            vad_filter=True,
            vad_parameters={"threshold": 0.35,
                            "min_silence_duration_ms": 500,
                            "speech_pad_ms": 400},
            condition_on_previous_text=False)
        segs = list(segments)
        raw = "".join(s.text for s in segs).strip()
        score = (sum(s.avg_logprob for s in segs) / len(segs)
                 if segs else -10.0)
        return raw, score

    def _start_recording(self):
        self.noise_floor = self.recorder.level
        self.rec_start = time.time()
        self.speech_seen = False
        self.last_voice = self.rec_start
        self.max_lvl = 0.0
        self.recorder.start()
        self.sounds.start()
        self.overlay.show_recording()
        self.state = "recording"

    def _finish_recording(self):
        self._silent_cancels = 0
        audio = self.recorder.stop()
        self.overlay.show_processing()
        threading.Thread(target=self._process, args=(audio,),
                         daemon=True).start()
        self.state = "processing"

    def _cancel_recording(self):
        self.recorder.stop()
        self.overlay.hide()
        self.sounds.nospeech()
        self.state = "idle"

    def _stop_recording(self):
        """Hotkey pressed again: transcribe what was said — or, if nothing
        was, just close."""
        if self.speech_seen:
            self._finish_recording()
        else:
            self._cancel_recording()

    def _cancel_by_user(self):
        """The X in the pill, or Esc: throw the recording away."""
        print("[rec] cancelled — nothing pasted")
        self._cancel_recording()

    def _pause_recording(self):
        self.recorder.pause()
        self.paused_at = time.time()
        self.overlay.show_paused()
        self.state = "paused"
        print("[rec] paused")

    def _resume_recording(self):
        now = time.time()
        # time spent paused doesn't count toward the silence wait, the
        # "no speech heard" timeout or the length limit: they all run on
        # rec_start / last_voice, so shift them past the pause
        self.rec_start += now - self.paused_at
        self.last_voice = now
        self.recorder.resume()
        self.overlay.show_recording()
        self.state = "recording"
        print("[rec] resumed")

    # the pill's buttons; Overlay runs these on the Tk thread
    def _on_pause_click(self):
        if self.state == "recording":
            self._pause_recording()
        elif self.state == "paused":
            self._resume_recording()

    def _on_cancel_click(self):
        if self.state in ("recording", "paused"):
            self._cancel_by_user()

    def _start_speaking(self):
        self.overlay.show_speaking()
        self.speaker.speak_selection(self.sounds)
        self.state = "speaking"

    def _request_quit(self):
        # called from the tray thread; the Tk thread does the actual quit
        self._quit = True

    # ---- state machine (Tk main thread, every 15 ms) ----
    def _tick(self):
        if self._quit:
            self.speaker.stop()
            self.root.destroy()
            return
        # mic watchdog: reconnect a dead input stream while nothing is
        # playing (reinitializing PortAudio would cut ongoing TTS audio)
        if self.state == "idle" and not self.speaker.active:
            self.recorder.ensure_alive()
        down = keys_down(self.hotkey_vks)
        pressed = down and not self.prev_down
        self.prev_down = down
        r_down = bool(self.read_vks) and keys_down(self.read_vks)
        r_pressed = r_down and not self.prev_read_down
        self.prev_read_down = r_down
        # Esc is edge-triggered: one already held when recording starts
        # must not cancel it
        esc = bool(self.cancel_vks) and keys_down(self.cancel_vks)
        esc_pressed = esc and not self.prev_esc
        self.prev_esc = esc
        toggle = self.cfg["activation"] == "toggle"
        auto = self.cfg["auto_stop"]

        if self.state == "idle" and r_pressed:
            self._start_speaking()

        elif self.state == "speaking":
            if r_pressed or pressed:      # either hotkey stops the reading
                self.speaker.stop()
            if not self.speaker.active:
                self.overlay.hide()
                self.state = "idle"

        elif self.state == "idle" and (pressed if toggle else down):
            self._start_recording()

        elif self.state == "recording":
            if toggle:
                now = time.time()
                lvl = self.recorder.level
                speech_thresh = max(self.noise_floor * 3.0,
                                    auto["min_level"])
                keep_thresh = max(self.noise_floor * 2.0,
                                  auto["min_level"] * 0.6)
                # skip the first 250 ms: the start chime can leak from the
                # speakers into the mic and read as speech
                if now - self.rec_start > 0.25:
                    self.max_lvl = max(self.max_lvl, lvl)
                    if lvl > speech_thresh:
                        self.speech_seen = True
                        self.last_voice = now
                    elif self.speech_seen and lvl > keep_thresh:
                        self.last_voice = now

                if pressed:                       # press again = stop now
                    self._stop_recording()
                elif esc_pressed:                 # Esc = throw it away
                    self._cancel_by_user()
                elif (self.speech_seen and
                      now - self.last_voice >
                      silence_window(auto, now - self.rec_start)):
                    self._finish_recording()
                elif (not self.speech_seen and
                      now - self.rec_start > auto["start_timeout_sec"]):
                    print(f"[rec] no speech heard — cancelled "
                          f"(peak level {self.max_lvl:.4f}, needed "
                          f"{speech_thresh:.4f}; lower auto_stop.min_level "
                          f"in config.json if you were speaking)")
                    self._cancel_recording()
                    # two silent recordings in a row usually mean Windows
                    # switched the default mic under us — rebind to it
                    self._silent_cancels += 1
                    if self._silent_cancels >= 2:
                        self._silent_cancels = 0
                        print("[mic] repeated silence — rebinding to the "
                              "current default microphone")
                        self.recorder.rebind()
                elif now - self.rec_start > auto["max_sec"]:
                    self._finish_recording()
            elif not down:
                # hold mode: capture a short tail so the last word survives
                self.tail_until = time.time() + self.cfg["tail_ms"] / 1000
                self.state = "tail"

        elif self.state == "paused":
            # the mic is off, so no silence wait and no time limits here;
            # only the red button (via Overlay) resumes
            if pressed:                           # hotkey = done: paste it
                self._stop_recording()
            elif esc_pressed:
                self._cancel_by_user()

        elif self.state == "tail":
            if down:
                self.state = "recording"
            elif time.time() >= self.tail_until:
                self._finish_recording()

        elif self.state == "processing":
            try:
                result = self.results.get_nowait()
            except queue.Empty:
                result = None
            if result is not None:
                self.overlay.hide()
                (self.sounds.done if result == "done"
                 else self.sounds.nospeech)()
                self.state = "idle"

        self.root.after(15, self._tick)

    def run(self):
        self.root.after(15, self._tick)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.tray.stop()
            print("bye")


def demo():
    """Preview the overlay with fake voice levels: python app.py demo"""
    root = tk.Tk()
    lvl = [0.0]
    ov = Overlay(root, lambda: lvl[0], on_pause=lambda: print("[demo] pause"),
                 on_cancel=lambda: print("[demo] cancel"))
    ov.show_recording()
    t0 = time.time()

    def drive():
        t = time.time() - t0
        if t < 8:
            # alternate fake speech bursts and pauses
            speaking = math.sin(t * 0.9) > -0.2
            lvl[0] = (max(0.0, 0.07 + 0.05 * math.sin(t * 6)
                          + 0.04 * math.sin(t * 15)) if speaking else 0.003)
            root.after(30, drive)
        elif t < 11:
            ov.show_processing()
            root.after(30, drive)
        else:
            root.destroy()

    drive()
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    elif already_running():
        user32.MessageBoxW(
            None, "O LocalFlow já está aberto — procure o ícone perto do "
            "relógio, no canto da barra de tarefas.", "LocalFlow", 0x40)
    else:
        App().run()
