"""Every Ollama call was paying a ~2s IPv6 timeout: "localhost" resolves to
::1 first, Ollama listens on IPv4 only, and Windows takes two seconds to
give up. These tests pin the fix without needing Ollama running."""
import sys, socket, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the project
import app

fails = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}"
          + (f"\n        {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# a stand-in for Ollama: IPv4-only listener, exactly how it binds by default
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.bind(("127.0.0.1", 0))
srv.listen(8)
port = srv.getsockname()[1]
threading.Thread(target=lambda: [srv.accept() for _ in range(50)],
                 daemon=True).start()

url = app.resolve_ollama_url(f"http://localhost:{port}")
check("localhost is swapped for the address that answers",
      url == f"http://127.0.0.1:{port}", f"got {url}")

# nothing listening at all: leave the URL alone rather than guess
dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
dead.bind(("127.0.0.1", 0))
dead_port = dead.getsockname()[1]
dead.close()
kept = app.resolve_ollama_url(f"http://localhost:{dead_port}")
check("an unreachable localhost URL is left untouched",
      kept == f"http://localhost:{dead_port}", f"got {kept}")

# a real host must never be rewritten to loopback
remote = app.resolve_ollama_url("http://gpu-box.lan:11434")
check("a remote host is left untouched",
      remote == "http://gpu-box.lan:11434", f"got {remote}")

check("an already-numeric URL is left untouched",
      app.resolve_ollama_url(f"http://127.0.0.1:{port}")
      == f"http://127.0.0.1:{port}")

# the cleaner must resolve once, at construction, and reuse one connection
cfg = dict(app.DEFAULTS["ollama"])
cfg["url"] = f"http://localhost:{port}"
cfg["enabled"] = False              # skip the probe, we only want the URL
cleaner = app.OllamaCleaner(cfg)
check("the cleaner uses the fast URL", cleaner.url == f"http://127.0.0.1:{port}",
      f"got {getattr(cleaner, 'url', None)}")
check("the cleaner keeps a session open for connection reuse",
      isinstance(getattr(cleaner, "http", None), app.requests.Session))

# and the resolve itself must be quick even when it has to fall back
t = time.time()
app.resolve_ollama_url(f"http://localhost:{dead_port}")
took = (time.time() - t) * 1000
check("resolving is fast even when nothing answers", took < 900,
      f"took {took:.0f} ms")

srv.close()
print()
if fails:
    sys.exit(f"{len(fails)} FAILED: {', '.join(fails)}")
print("ALL OLLAMA SPEED TESTS PASSED")
