"""The overlay must follow the CURRENT screen size, not the one it saw at
startup. No real window is created: Tk is stubbed out entirely."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the project
import app

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
    def after(self, ms, fn): pass          # never runs the animation loop


class FakeUser32:
    def GetDpiForSystem(self): return 96
    def GetSystemMetrics(self, i): return screen["w"] if i == 0 else screen["h"]
    def GetParent(self, h): return 0
    def GetWindowLongW(self, h, i): return 0
    def SetWindowLongW(self, h, i, v): return 0


app.user32 = FakeUser32()
app.LayeredSurface = lambda hwnd, w, h: None    # no real window to paint on

root = FakeRoot()
ov = app.Overlay(root, lambda: 0.0)
ov._blit = lambda img: None            # nothing to blit without a window
w, h = ov.W, ov.H
bottom = ov.GAP - ov.m                 # GAP is measured to the pill edge


def want_geom(sw, sh):
    return f"{w}x{h}+{(sw - w) // 2}+{sh - h - bottom}"


first = root.geoms[-1]
assert first == want_geom(1920, 1080), f"unexpected startup geometry: {first}"
print(f"startup on 1920x1080: {first}")

# the screen shrinks while the app keeps running (game exits, monitor swap)
screen["w"], screen["h"] = 1440, 900
ov.show_recording()
placed = root.geoms[-1]
want = want_geom(1440, 900)
assert placed == want, (
    f"overlay stayed at {placed} after the screen became 1440x900 "
    f"(expected {want}) — the pill is drawn off-screen")
print(f"after resolution change: {placed}")

# and it must stay put while nothing changes (no needless geometry churn)
n = len(root.geoms)
ov.show_recording()
ov._animate()
assert len(root.geoms) == n, "geometry re-applied even though nothing changed"
print("no churn when the screen is unchanged")

# a change while the pill is already visible is picked up by the animation loop
screen["w"], screen["h"] = 2560, 1440
ov._animate()
want = want_geom(2560, 1440)
assert root.geoms[-1] == want, (
    f"live resolution change ignored: {root.geoms[-1]} != {want}")
print(f"live change while visible: {root.geoms[-1]}")

print("\nALL OVERLAY PLACEMENT TESTS PASSED")
