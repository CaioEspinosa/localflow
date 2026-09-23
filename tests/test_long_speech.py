"""Long dictations must survive intact.

Two failures seen in localflow.log, both only on long speech:
  1. the cleanup LLM silently drops the last sentence
  2. auto-stop ends the recording during a normal thinking pause

Everything here is offline: the Ollama call is stubbed, no audio, no
keystrokes.
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the project
import app

fails = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}"
          + (f"\n        {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------- splitting
# made-up dictation with the shape Whisper returns for long speech: a few
# punctuated sentences, then a run-on of capitalised fragments
RAW_LONG = (
    "Hoje eu quero organizar a viagem de fim de semana para a praia. "
    "Primeiro a gente precisa ver o horário do ônibus, porque o último sai "
    "cedo. Depois eu vou separar as roupas, o protetor solar e o carregador "
    "do celular. Também preciso lembrar de comprar pão e frutas antes de "
    "sair de casa. E no sábado de manhã A gente sai bem cedo Para pegar a "
    "estrada sem trânsito Então quando chegar lá Primeiro deixa as malas na "
    "pousada Depois almoça em algum lugar perto da praia E à tarde dá para "
    "caminhar na areia Ver o pôr do sol E voltar para jantar com calma No "
    "domingo a gente acorda sem pressa Toma café da manhã na pousada Arruma "
    "as coisas E pega o ônibus de volta no fim da tarde Assim dá tempo de "
    "descansar antes da semana começar")

chunks = app.split_for_cleanup(RAW_LONG, 600)
check("long text is split into several requests", len(chunks) > 1,
      f"got {len(chunks)} chunk(s)")
check("no chunk exceeds the limit", all(len(c) <= 600 for c in chunks),
      f"sizes {[len(c) for c in chunks]}")
check("splitting loses no words",
      " ".join(chunks).split() == RAW_LONG.split())
check("short text is left as one request",
      app.split_for_cleanup("Oi, tudo bem?", 600) == ["Oi, tudo bem?"])
# a run-on with no punctuation at all must still be cut
runon = " ".join(["palavra"] * 300)
check("run-on speech with no punctuation still gets cut",
      all(len(c) <= 600 for c in app.split_for_cleanup(runon, 600)),
      f"sizes {[len(c) for c in app.split_for_cleanup(runon, 600)]}")

# ------------------------------------------------------------ loss detector
worst, tail = app.dropped_words(
    "depois almoça em algum lugar perto da praia e à tarde dá para caminhar "
    "na areia e ver o pôr do sol",
    "Depois almoça em algum lugar perto da praia. E à tarde dá para "
    "caminhar na areia.")
check("a dropped final sentence is detected", tail >= 3,
      f"worst={worst} tail={tail}")

worst, tail = app.dropped_words(
    "é tipo eu queria saber se se você pode me mandar o relatório até sexta né",
    "Eu queria saber se você pode me mandar o relatório até sexta.")
check("normal filler removal is NOT flagged as loss", worst < 6 and tail < 3,
      f"worst={worst} tail={tail}")

# --------------------------------------------------------- cleanup guarding
class FakeSession:
    """Stands in for the requests.Session the cleaner keeps open."""
    def __init__(self, handler): self.post = handler


class FakeResponse:
    def __init__(self, text): self._t = text
    def raise_for_status(self): pass
    def json(self): return {"message": {"content": self._t}}


calls = []


def fake_post(url, json=None, timeout=None, **kw):
    """Mimics gemma3 dropping the tail of whatever it is given."""
    sent = json["messages"][-1]["content"]
    calls.append(sent)
    kept = sent.rsplit(" ", 8)[0] if len(sent.split()) > 20 else sent
    return FakeResponse(kept)


cfg = dict(app.DEFAULTS["ollama"])
cleaner = app.OllamaCleaner.__new__(app.OllamaCleaner)   # skip the probe
cleaner.cfg = cfg
cleaner.available = True
cleaner._next_probe = 0.0
cleaner.url = "http://127.0.0.1:11434"
cleaner.http = FakeSession(fake_post)

out = cleaner.clean(RAW_LONG, lang="pt")

check("a long transcript is sent as several requests", len(calls) > 1,
      f"{len(calls)} request(s)")
check("words the model dropped are restored from the raw transcript",
      out.split()[-8:] == RAW_LONG.split()[-8:],
      f"ends with {' '.join(out.split()[-8:])!r}")

# a well-behaved model must still get its cleanup through
calls.clear()


def polite_post(url, json=None, timeout=None, **kw):
    sent = json["messages"][-1]["content"]
    calls.append(sent)
    return FakeResponse(sent.replace(" tipo ", " ").capitalize())


cleaner.http = FakeSession(polite_post)
out = cleaner.clean("é tipo eu queria saber se você pode", lang="pt")
check("a faithful cleanup is kept", "tipo" not in out, f"got {out!r}")

# ------------------------------------------------------------- auto-stop
auto = dict(app.DEFAULTS["auto_stop"])
short = app.silence_window(auto, elapsed=2.0)
long_ = app.silence_window(auto, elapsed=25.0)
check("a quick dictation still stops fast",
      abs(short - auto["silence_ms"] / 1000) < 0.01, f"{short}s")
check("a long dictation tolerates a longer thinking pause",
      long_ > short + 0.5, f"short={short}s long={long_}s")
check("the pause never grows without bound", long_ <= 6.0, f"{long_}s")

print()
if fails:
    sys.exit(f"{len(fails)} FAILED: {', '.join(fails)}")
print("ALL LONG-SPEECH TESTS PASSED")
