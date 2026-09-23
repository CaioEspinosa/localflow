"""Verify the inject() hardening WITHOUT sending any real keystrokes.
Every low-level input sender is stubbed, so this touches nothing on screen."""
import sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the project
import app

KEYUP = app.KEYEVENTF_KEYUP
sent = []          # records the synthetic key event batches
app._send_key_events = lambda events: sent.append(list(events))

# ---- 1. release_all_modifiers clears every modifier, Win masked by Ctrl ----
sent.clear()
app.release_all_modifiers()
ev = sent[0]
assert ev[0] == (app.VK["ctrl"], 0, 0), "must press Ctrl first as the mask"
ups = {vk for vk, sc, fl in ev if fl & KEYUP}
assert 0x5B in ups and 0x5C in ups, "both Win keys must be released"
assert app.VK["ctrl"] in ups and app.VK["shift"] in ups and app.VK["alt"] in ups
# the Win-ups must happen while Ctrl (the mask) is still held down
ctrl_up_idx = next(i for i, (vk, sc, fl) in enumerate(ev)
                   if vk == app.VK["ctrl"] and fl & KEYUP)
lwin_up_idx = next(i for i, (vk, sc, fl) in enumerate(ev)
                   if vk == 0x5B and fl & KEYUP)
assert lwin_up_idx < ctrl_up_idx, "Win must be released before the Ctrl mask"
print("release_all_modifiers: OK (Win masked, all modifiers released)")

# ---- 2. inject paste path: copies, pastes, then releases modifiers ----
calls = []
app.send_ctrl_v = lambda: calls.append("ctrl_v")
app.type_unicode = lambda t: calls.append(("type", t))
clip = {"v": "OLD CLIP"}
app.pyperclip.paste = lambda: clip["v"]
def good_copy(t): clip["v"] = t
app.pyperclip.copy = good_copy
app.time.sleep = lambda s: None      # no real waiting

sent.clear(); calls.clear()
app.inject("olá mundo", "paste")
assert "ctrl_v" in calls, "paste path must send Ctrl+V"
assert sent, "paste path must release modifiers afterwards"
assert clip["v"] == "OLD CLIP", "original clipboard must be restored"
print("inject paste path: OK (pastes, releases modifiers, restores clipboard)")

# ---- 3. clipboard locked -> falls back to typing, never raises ----
def locked_copy(t): raise app.pyperclip.PyperclipException("clipboard busy")
app.pyperclip.copy = locked_copy
sent.clear(); calls.clear()
try:
    app.inject("texto de teste", "paste")
except Exception as e:
    raise AssertionError(f"inject must not raise when clipboard is locked: {e}")
assert any(c == ("type", "texto de teste") for c in calls), \
    "must type directly when the clipboard stays locked"
assert "ctrl_v" not in calls, "must not try to paste when copy failed"
print("inject clipboard-locked fallback: OK (types directly, no error)")

# ---- 4. _clipboard_copy retries then succeeds ----
attempts = {"n": 0}
def flaky_copy(t):
    attempts["n"] += 1
    if attempts["n"] < 3:
        raise app.pyperclip.PyperclipException("busy")
app.pyperclip.copy = flaky_copy
assert app._clipboard_copy("x") is True and attempts["n"] == 3
print("_clipboard_copy retry: OK (recovered on 3rd try)")

print("\nALL INJECT-FIX TESTS PASSED")
