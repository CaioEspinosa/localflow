"""The pill's hands-free controls.

X (left) cancels; the red button (right) PAUSES the dictation — its icon
turns from pause (two bars) into play (a triangle) and the wave goes flat —
and a second click resumes. Esc cancels, the hotkey finishes.

No real input is ever sent: clicks are delivered as window messages to OUR
OWN test window, keys are a stubbed keys_down(), the microphone is fed fake
blocks. Nothing can reach any other app.
"""
import sys, time, json, ctypes, types, threading, collections
from ctypes import wintypes
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the project
import numpy as np
import app

sys.stdout.reconfigure(encoding="utf-8")
fails = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}"
          + (f"\n        {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


WM_MOUSEACTIVATE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = \
    0x21, 0x201, 0x202, 0x203
MA_NOACTIVATE = 3
WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x20, 0x08000000


def lparam(x, y):
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


# =================================================== stubbed Tk + Win32 ===
screen = {"w": 1920, "h": 1080}


class FakeRoot:
    def __init__(self): self.geoms = []
    def overrideredirect(self, v): pass
    def attributes(self, *a): pass
    def config(self, **kw): pass
    def winfo_screenwidth(self): return screen["w"]
    def winfo_screenheight(self): return screen["h"]
    def geometry(self, g): self.geoms.append(g)
    def withdraw(self): pass
    def deiconify(self): pass
    def update_idletasks(self): pass
    def winfo_id(self): return 0
    def after(self, ms, fn): pass


class FakeUser32:
    def __init__(self): self.ex, self.cursor = 0, (0, 0)
    def GetDpiForSystem(self): return 96
    def GetSystemMetrics(self, i): return screen["w"] if i == 0 else screen["h"]
    def GetParent(self, h): return 0
    def GetWindowLongW(self, h, i): return self.ex
    def SetWindowLongW(self, h, i, v): self.ex = v; return 0
    def SetWindowLongPtrW(self, h, i, v): return 0x1234   # "previous proc"
    def CallWindowProcW(self, *a): return 0
    def DefWindowProcW(self, *a): return 0
    def GetCursorPos(self, p):                # real one gives whole pixels
        p._obj.x, p._obj.y = (round(v) for v in self.cursor)
        return 1


real_user32, real_surface = app.user32, app.LayeredSurface
fake = FakeUser32()
app.user32 = fake
app.LayeredSurface = lambda hwnd, w, h: None

clicks = []
plain = app.Overlay(FakeRoot(), lambda: 0.0)
ov = app.Overlay(FakeRoot(), lambda: 0.0,
                 on_pause=lambda: clicks.append("pause"),
                 on_cancel=lambda: clicks.append("cancel"))
frames = []
# keep the real _blit (it downsamples the 4x frame to window pixels) and
# catch what it would hand to Windows
ov.surface = types.SimpleNamespace(push=frames.append)
plain.surface = types.SimpleNamespace(push=lambda img: None)

# ---------------------------------------------------------------- layout
print("--- layout")
check("hold-to-talk pill is unchanged (194x52, no buttons)",
      (plain.W, plain.H) == (194, 52) and not plain.btn_centers,
      f"{plain.W}x{plain.H} buttons={plain.btn_centers}")
check("hold-to-talk wave spans exactly what it did before",
      (plain.wave_x0, plain.wave_span) == (11 + 14, 144),
      f"x0={plain.wave_x0} span={plain.wave_span}")
check("hands-free pill is 214x34 (window 236x56)",
      (ov.pw, ov.ph, ov.W, ov.H) == (214, 34, 236, 56),
      f"pill {ov.pw}x{ov.ph} window {ov.W}x{ov.H}")
check("the wave keeps today's width between the buttons",
      (ov.wave_x0, ov.wave_span) == (11 + 35, 144),
      f"x0={ov.wave_x0} span={ov.wave_span}")
cx_c, cy_c = ov.btn_centers["cancel"]
cx_p, cy_p = ov.btn_centers["pause"]
check("X sits at the left end, the red button at the right end",
      (cx_c, cy_c, cx_p, cy_p) == (28, 28, 208, 28),
      f"cancel=({cx_c},{cy_c}) pause=({cx_p},{cy_p})")

# ---------------------------------------------------------- hit-testing
print("--- hit-testing")
check("button centers hit their button",
      ov._button_at(cx_c, cy_c) == "cancel"
      and ov._button_at(cx_p, cy_p) == "pause")
check("a small margin around a button still counts (16 px reach)",
      ov._button_at(cx_p - 15, cy_p) == "pause"
      and ov._button_at(cx_c + 15, cy_c) == "cancel")
check("the wave area and the empty margin hit nothing",
      ov._button_at(ov.W / 2, ov.H / 2) is None
      and ov._button_at(cx_p - 18, cy_p) is None
      and ov._button_at(1, 1) is None)

# --------------------------------------------------------------- clicks
print("--- clicks")


class NoTk:
    """Stands in for root while a window message is handled: touching Tk
    from inside a window procedure aborts Python (tkinter thread state)."""
    def __getattr__(self, name):
        raise AssertionError(f"window procedure touched Tk: root.{name}")


def wndproc(msg, lp):
    real_root, ov.root = ov.root, NoTk()
    try:
        return ov._on_message(msg, lp)
    finally:
        ov.root = real_root


def click(x, y, up_at=None, dbl=False):
    wndproc(WM_LBUTTONDBLCLK if dbl else WM_LBUTTONDOWN, lparam(x, y))
    ux, uy = up_at or (x, y)
    r = wndproc(WM_LBUTTONUP, lparam(ux, uy))
    ov._animate()                  # the animation loop delivers the click
    return r


ov.show_recording()
clicks.clear()
wndproc(WM_LBUTTONDOWN, lparam(cx_p, cy_p))
wndproc(WM_LBUTTONUP, lparam(cx_p, cy_p))
check("the window procedure never calls back into Tk itself", clicks == [],
      f"{clicks}")
ov._animate()
check("...the animation loop delivers the click", clicks == ["pause"],
      f"{clicks}")
clicks.clear(); click(cx_p, cy_p)
check("click on the red button asks to pause", clicks == ["pause"],
      f"{clicks}")
clicks.clear(); click(cx_c, cy_c)
check("click on X cancels", clicks == ["cancel"], f"{clicks}")
clicks.clear(); click(cx_p, cy_p, up_at=(ov.W / 2, ov.H / 2))
check("press on the button then drag off before releasing does nothing",
      clicks == [], f"{clicks}")
clicks.clear(); click(ov.W / 2, ov.H / 2)
check("a click on the wave does nothing", clicks == [])
clicks.clear(); click(cx_p, cy_p, dbl=True)
check("a fast double-click still counts as a click", clicks == ["pause"],
      f"{clicks}")
check("the pill swallows its own clicks",
      click(cx_p, cy_p) == 0 and wndproc(WM_LBUTTONDOWN, lparam(1, 1)) == 0)
check("a click never activates the pill (MA_NOACTIVATE)",
      wndproc(WM_MOUSEACTIVATE, 0) == MA_NOACTIVATE)
check("other messages are left to Tk", wndproc(0x000F, 0) is None)
ov.show_paused()
clicks.clear(); click(cx_p, cy_p)
check("while paused the red button still answers (to resume)",
      clicks == ["pause"], f"{clicks}")
clicks.clear(); click(cx_c, cy_c)
check("while paused X still cancels", clicks == ["cancel"], f"{clicks}")
ov.show_processing()
clicks.clear(); click(cx_p, cy_p); click(cx_c, cy_c)
check("buttons are dead once recording is over", clicks == [], f"{clicks}")

# ------------------------------------------------------- click-through
print("--- click-through")
ov.show_recording()
check("while recording the pill takes clicks (no WS_EX_TRANSPARENT)",
      not fake.ex & WS_EX_TRANSPARENT, f"{fake.ex:#x}")
check("...and still never takes focus (WS_EX_NOACTIVATE)",
      bool(fake.ex & WS_EX_NOACTIVATE), f"{fake.ex:#x}")
ov.show_paused()
check("while paused it keeps taking clicks (to resume or cancel)",
      not fake.ex & WS_EX_TRANSPARENT, f"{fake.ex:#x}")
ov.show_processing()
check("right after recording it keeps eating clicks (a double-click's 2nd "
      "click must not fall into the app below)",
      not fake.ex & WS_EX_TRANSPARENT)
ov._click_until = time.time() - 1          # grace period is over
ov._animate()
check("then clicks go through again while processing",
      bool(fake.ex & WS_EX_TRANSPARENT), f"{fake.ex:#x}")
ov.show_speaking()
check("read-aloud pill lets clicks through", bool(fake.ex & WS_EX_TRANSPARENT))
ov.show_recording(); ov.hide()
check("hidden pill lets clicks through", bool(fake.ex & WS_EX_TRANSPARENT))
fake.ex = 0
plain.show_recording()
check("hold-to-talk pill never takes clicks", plain._clickable is False,
      f"{fake.ex:#x}")

# ---------------------------------------------------------------- hover
print("--- hover")
ov.show_recording()
ox, oy = ov._origin
fake.cursor = (ox + cx_p, oy + cy_p); ov._animate()
check("mouse over the red button -> hover=pause", ov.hover == "pause",
      f"{ov.hover}")
fake.cursor = (ox + cx_c + 3, oy + cy_c - 2); ov._animate()
check("mouse over X -> hover=cancel", ov.hover == "cancel", f"{ov.hover}")
ov.show_paused(); fake.cursor = (ox + cx_p, oy + cy_p); ov._animate()
check("hover works while paused too", ov.hover == "pause", f"{ov.hover}")
fake.cursor = (ox - 300, oy); ov._animate()
check("mouse elsewhere -> no hover", ov.hover is None, f"{ov.hover}")

# ------------------------------------------------------------ rendering
print("--- rendering")
px = lambda a, x, y: tuple(int(v) for v in a[int(y), int(x)])
frames.clear()
ov.show_recording()
fake.cursor = (0, 0)
t0 = time.time()
for i in range(20):
    ov._draw_wave(ov.LAYERS, 0.15 * (i % 3), 12)
ms = (time.time() - t0) * 1000 / 20
check(f"frame time {ms:.1f} ms fits the 30 ms budget", ms < 30)
quiet = np.asarray(frames[0])               # level 0
loud = np.asarray(frames[1])                # level 0.15
red = px(quiet, cx_p + 9, cy_p)             # on the disc, off the icon
check("the button is red", red[0] > 200 and red[1] < 120 and red[2] < 120
      and red[3] == 255, f"{red}")
gap = px(quiet, cx_p, cy_p)                 # between the two pause bars
bar = px(quiet, cx_p - 3, cy_p)             # on the left pause bar
check("recording: the icon is PAUSE (two bars, red gap between them)",
      gap[0] > gap[1] + 60 and min(bar[:3]) > 200, f"gap={gap} bar={bar}")
disc, pill = px(quiet, cx_c + 8, cy_c), px(quiet, ov.W / 2, ov.H - 14)
check("X disc is a light tint over the pill, not a loud color",
      disc[0] > pill[0] and abs(disc[0] - disc[2]) < 25, f"{disc} vs {pill}")
check("X icon is light", min(px(quiet, cx_c, cy_c)[:3]) > 180,
      f"{px(quiet, cx_c, cy_c)}")
r = int(ov.BTN_R * ov.k) + 1
for name, (bx, by) in ov.btn_centers.items():
    box = (slice(int(by - r), int(by + r)), slice(int(bx - r), int(bx + r)))
    check(f"waves never paint over the {name} button",
          np.array_equal(quiet[box], loud[box]))
ov.hover = "pause"; ov._draw_wave(ov.LAYERS, 0.0, 12)
hot = px(np.asarray(frames[-1]), cx_p + 9, cy_p)
check("hovering the red button brightens it",
      hot[0] >= red[0] and hot[1] > red[1], f"{red} -> {hot}")
ov.hover = None

# paused look: the icon flips to play and the wave settles flat and dim
ov._draw_wave(ov.LAYERS, 0.2, 12)
live = np.asarray(frames[-1]).astype(int)
ov.show_paused()
for _ in range(40):                          # let the amplitude settle
    ov._draw_wave(ov.LAYERS, 0.2, 12)        # (the level must be ignored)
paused = np.asarray(frames[-1]).astype(int)
center = px(paused, cx_p, cy_p)
check("paused: the icon is PLAY (a triangle, white in the middle)",
      min(center[:3]) > 200, f"{center}")
check("paused: the button stays red around the icon",
      px(paused, cx_p + 9, cy_p)[1] < 120, f"{px(paused, cx_p + 9, cy_p)}")
wave = (slice(0, ov.H), slice(ov.wave_x0 + 20, ov.wave_x0 + ov.wave_span - 20))
base_img = np.asarray(ov.btn_bases[(None, True)].resize(
    (ov.W, ov.H), app.Image.LANCZOS)).astype(int)
diff = np.abs(paused[wave] - base_img[wave]).max(axis=2)
rows = np.nonzero(diff.max(axis=1) > 12)[0]
check("paused: the wave is a flat line (only a few rows around the middle)",
      len(rows) > 0 and rows.max() - rows.min() <= 6
      and abs((rows.max() + rows.min()) / 2 - ov.H / 2) <= 2,
      f"rows drawn: {rows.tolist()}")
bright_live = (live[wave][:, :, :3].sum(axis=2)).max()
bright_paused = (paused[wave][:, :, :3].sum(axis=2)).max()
check("paused: the line is dimmed", bright_paused < 0.75 * bright_live,
      f"live {bright_live} vs paused {bright_paused}")

# =========================================================== recorder ===
print("--- recorder pause")
rec = app.Recorder.__new__(app.Recorder)       # no real microphone
rec.sr, rec._preroll_ms = 16000, 600
rec._preroll_samples = int(16000 * 0.6)
rec._preroll, rec._preroll_len, rec._frames = collections.deque(), 0, []
rec._recording = rec._paused = False
rec._lock, rec.level, rec.last_cb = threading.Lock(), 0.0, 0.0


def feed(value, blocks=5, n=480):
    for _ in range(blocks):
        rec._callback(np.full((n, 1), value, np.float32), n, None, None)


feed(0.05)                     # before: lands in the pre-roll, as today
rec.start()
feed(0.10)                     # spoken before the pause
rec.pause()
feed(0.90)                     # spoken WHILE paused: must never be kept
rec.resume()
feed(0.20)                     # spoken after resuming
audio = rec.stop()
check("audio spoken during the pause is not kept",
      audio.max() < 0.5, f"max {audio.max():.2f}")
check("words before and after the pause are both kept",
      np.isclose(audio, 0.10).any() and np.isclose(audio, 0.20).any())
check("the pre-roll only applies at the start, not on resume (it would "
      "hold what was said while paused)",
      np.isclose(audio, 0.05).any() and not (audio > 0.5).any())
i10 = np.nonzero(np.isclose(audio, 0.10))[0].max()
i20 = np.nonzero(np.isclose(audio, 0.20))[0].min()
check("a short silence separates the two parts",
      i20 - i10 - 1 >= int(0.25 * 16000)
      and np.all(audio[i10 + 1:i20] == 0), f"gap {i20 - i10 - 1} samples")
rec.start(); rec.pause(); feed(0.3); audio = rec.stop()
check("stopping while paused works and a new recording starts clean",
      not rec._paused and not rec._recording and not (audio == 0.3).any())

# ============================================== App: buttons and Esc ===
print("--- app: pause, resume, cancel, Esc")
calls = []


class Rec:
    level = 0.0
    def start(self): calls.append("rec.start")
    def stop(self): calls.append("rec.stop"); return np.zeros(1600, np.float32)
    def pause(self): calls.append("rec.pause")
    def resume(self): calls.append("rec.resume")
    def ensure_alive(self): pass


class Ov:
    def show_recording(self): calls.append("ov.recording")
    def show_paused(self): calls.append("ov.paused")
    def show_processing(self): calls.append("ov.processing")
    def hide(self): calls.append("ov.hide")


class Snd:
    def start(self): pass
    def done(self): pass
    def nospeech(self): calls.append("snd.nospeech")


held = set()
real_keys_down = app.keys_down
app.keys_down = lambda vks: all(vk in held for vk in vks)


def make_app(esc=True):
    a = app.App.__new__(app.App)
    a.cfg = json.loads(json.dumps(app.DEFAULTS))
    a.cfg["esc_cancels"] = esc
    a.hotkey_vks = [app.VK[k] for k in a.cfg["hotkey"]]
    a.cancel_vks = [0x1B] if esc else []
    a.read_vks = []
    a.prev_down = a.prev_read_down = a.prev_esc = False
    a.state, a._quit, a._silent_cancels = "idle", False, 0
    a.recorder, a.overlay, a.sounds = Rec(), Ov(), Snd()
    a.speaker = types.SimpleNamespace(active=False, level=0.0,
                                      stop=lambda: None)
    a.root = FakeRoot()
    a.results = None
    a.noise_floor = a.rec_start = a.last_voice = a.max_lvl = 0.0
    a.paused_at = 0.0
    a.speech_seen = False
    a._finish_recording = lambda: (calls.append("finish"),
                                   setattr(a, "state", "processing"))
    return a


a = make_app(); a._start_recording(); calls.clear()
a._on_pause_click()
check("red button while recording: pause the mic and show it",
      a.state == "paused" and calls == ["rec.pause", "ov.paused"],
      f"state={a.state} calls={calls}")

a.speech_seen = True
a.last_voice = time.time() - 100           # a long silence...
a.rec_start = time.time() - 170            # ...near the length limit too
held.clear(); a._tick(); a._tick()
check("while paused, silence never ends the dictation",
      a.state == "paused", a.state)
b = make_app(); b._start_recording(); b._on_pause_click()
b.rec_start = time.time() - 60             # never said a word, long ago
b._tick()
check("while paused, the 'no speech heard' timeout does not cancel",
      b.state == "paused", b.state)

a = make_app(); a._start_recording()
start = a.rec_start
a._on_pause_click(); time.sleep(0.3); calls.clear()
a._on_pause_click()
check("red button again: resume recording",
      a.state == "recording" and calls == ["rec.resume", "ov.recording"],
      f"state={a.state} calls={calls}")
check("the pause does not count toward the time limits",
      a.rec_start - start >= 0.29, f"shifted {a.rec_start - start:.2f}s")
check("the silence wait starts over on resume",
      time.time() - a.last_voice < 0.1)
a.speech_seen = True; held.clear(); a._tick()
check("...so resuming does not end the dictation at once",
      a.state == "recording", a.state)

a = make_app(); a._start_recording(); a._on_pause_click(); calls.clear()
a._on_cancel_click()
check("X while paused: discard, nothing pasted",
      a.state == "idle" and calls == ["rec.stop", "ov.hide", "snd.nospeech"],
      f"state={a.state} calls={calls}")
a = make_app(); a._start_recording(); calls.clear()
a._on_cancel_click()
check("X while recording: discard, nothing pasted",
      a.state == "idle" and "snd.nospeech" in calls, f"{calls}")
calls.clear(); a._on_cancel_click(); a._on_pause_click()
check("buttons do nothing when not recording", calls == [], f"{calls}")

a = make_app(); a._start_recording(); a.speech_seen = True
a._on_pause_click(); held.clear(); a._tick(); calls.clear()
held.update(a.hotkey_vks); a._tick()
check("hotkey while paused: paste what was said", calls == ["finish"],
      f"{calls}")
a = make_app(); a._start_recording(); a._on_pause_click()
held.clear(); a._tick(); held.update(a.hotkey_vks); a._tick()
check("hotkey while paused, nothing said yet: just close",
      a.state == "idle", a.state)

a = make_app(); a._start_recording(); a._on_pause_click()
held.clear(); a._tick(); held.add(0x1B); a._tick()
check("Esc while paused cancels", a.state == "idle", a.state)

a = make_app(); a._start_recording(); calls.clear()
held.clear(); a._tick()
check("no Esc -> keeps recording", a.state == "recording", a.state)
held.add(0x1B); a._tick()
check("Esc while recording cancels", a.state == "idle"
      and "ov.hide" in calls, f"state={a.state} calls={calls}")
a = make_app(); held.clear(); held.add(0x1B)
a._tick()                                  # Esc already down while idle
a._start_recording(); a._tick()
check("an Esc held since before the recording does not cancel it",
      a.state == "recording", a.state)
held.clear(); a._tick(); held.add(0x1B); a._tick()
check("...but pressing it again does", a.state == "idle", a.state)
a = make_app(esc=False); a._start_recording(); held.clear(); a._tick()
held.add(0x1B); a._tick()
check("esc_cancels=false leaves Esc alone", a.state == "recording", a.state)
a = make_app(); a._start_recording(); a.speech_seen = True
held.clear(); a._tick(); calls.clear()
held.update(a.hotkey_vks); a._tick()
check("the hotkey still finishes while recording", calls == ["finish"],
      f"{calls}")
held.clear()
app.keys_down = real_keys_down

# ------------------------------------------------ shorter silence wait
print("--- silence wait")
auto = app.load_config()["auto_stop"]
short = app.silence_window(auto, 2.0)
long_ = app.silence_window(auto, 30.0)
check(f"a quick dictation now ends after {short:.1f}s of silence (was 1.8)",
      abs(short - 1.5) < 0.01, f"{short}")
check(f"a long one after {long_:.1f}s (was 3.2)", abs(long_ - 2.6) < 0.01,
      f"{long_}")

# ================================= live window (ours only, no real input)
print("--- live window")
app.user32, app.LayeredSurface = real_user32, real_surface
import tkinter as tk
u = ctypes.WinDLL("user32", use_last_error=True)
u.WindowFromPoint.argtypes = (app.POINT,)
u.WindowFromPoint.restype = wintypes.HWND
u.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
u.GetAncestor.restype = wintypes.HWND
u.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                           wintypes.LPARAM)
u.SendMessageW.restype = ctypes.c_ssize_t
u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                           wintypes.LPARAM)
u.GetForegroundWindow.restype = wintypes.HWND
u.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))

fg_before = u.GetForegroundWindow()
root = tk.Tk()
live_clicks = []
lv = app.Overlay(root, lambda: 0.08,
                 on_pause=lambda: live_clicks.append("pause"),
                 on_cancel=lambda: live_clicks.append("cancel"))


def pump(sec=0.25):
    end = time.time() + sec
    while time.time() < end:
        root.update(); time.sleep(0.01)


lv.show_recording(); pump()
rc = wintypes.RECT(); u.GetWindowRect(lv.hwnd, ctypes.byref(rc))
ex = real_user32.GetWindowLongW(lv.hwnd, -20)
check("real window: clickable while recording, never activates",
      not ex & WS_EX_TRANSPARENT and ex & WS_EX_NOACTIVATE, f"{ex:#x}")
for name, h in (("inner", root.winfo_id()), ("outer", lv.hwnd)):
    ret = u.SendMessageW(h, WM_MOUSEACTIVATE, lv.hwnd,
                         (WM_LBUTTONDOWN << 16) | 1)
    check(f"real window ({name}): WM_MOUSEACTIVATE -> MA_NOACTIVATE",
          ret == MA_NOACTIVATE, f"got {ret}")


def at(x, y):
    h = u.WindowFromPoint(app.POINT(rc.left + int(x), rc.top + int(y)))
    return h, (u.GetAncestor(h, 2) == lv.hwnd) if h else False


hit, ours = at(cx_p, cy_p)
check("real window: the red button is under the mouse, not the app below",
      ours)
for (x, y), want in (((cx_p, cy_p), ["pause"]), ((cx_c, cy_c), ["cancel"]),
                     ((lv.W / 2, lv.H / 2), [])):
    live_clicks.clear()
    u.PostMessageW(hit, WM_LBUTTONDOWN, 1, lparam(x, y))
    u.PostMessageW(hit, WM_LBUTTONUP, 0, lparam(x, y))
    pump(0.15)
    check(f"real window: click at ({x:.0f},{y:.0f}) -> {want}",
          live_clicks == want, f"{live_clicks}")
lv.show_paused(); pump(0.1)
live_clicks.clear()
u.PostMessageW(hit, WM_LBUTTONDOWN, 1, lparam(cx_p, cy_p))
u.PostMessageW(hit, WM_LBUTTONUP, 0, lparam(cx_p, cy_p))
pump(0.15)
check("real window: paused pill still takes the resume click",
      live_clicks == ["pause"] and at(cx_p, cy_p)[1], f"{live_clicks}")
lv.show_processing(); pump(0.1)
check("real window: still catching clicks right after recording",
      at(cx_p, cy_p)[1])
pump(0.8)
check("real window: clicks pass through again after the grace period",
      not at(cx_p, cy_p)[1])
check("real window: corner (transparent) never catches clicks",
      not at(1, 1)[1])
check("foreground window never changed",
      u.GetForegroundWindow() == fg_before)
root.destroy()

print()
if fails:
    sys.exit(f"{len(fails)} FAILED: {', '.join(fails)}")
print("ALL PILL-BUTTON TESTS PASSED")
