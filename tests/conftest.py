"""Shared test setup.

The prototype imports a heavy dependency stack at module load time
(``sentence_transformers``, ``faiss``, ``apscheduler``, ``uvicorn``,
``ollama``). None of that is needed to exercise the pure functions these
characterization tests cover, so we register lightweight stand-ins in
``sys.modules`` before the modules under test are imported. Real packages,
when installed, are left untouched.
"""

from __future__ import annotations

import sys
import types


def _ensure_stub(name: str, **attrs: object) -> types.ModuleType:
    """Register a stub module (and its parent packages) if not already importable."""
    if name in sys.modules:
        mod = sys.modules[name]
    else:
        try:  # prefer the real package when it is installed
            return __import__(name)
        except Exception:
            mod = types.ModuleType(name)
            sys.modules[name] = mod
    for key, value in attrs.items():
        setattr(mod, key, value)
    # make sure parent packages exist so ``from a.b.c import x`` resolves
    parts = name.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        sys.modules.setdefault(parent, types.ModuleType(parent))
    return mod


class _FakeSentenceTransformer:
    def __init__(self, *_args, **_kwargs):
        pass

    def encode(self, texts, **_kwargs):  # pragma: no cover - not called in unit tests
        raise RuntimeError("embedding is stubbed out in tests")


# --- feedback_platform.py heavy imports ------------------------------------
_ensure_stub("sentence_transformers", SentenceTransformer=_FakeSentenceTransformer)
_ensure_stub("faiss")
_ensure_stub("apscheduler")
_ensure_stub("apscheduler.schedulers")
_ensure_stub("apscheduler.schedulers.background", BackgroundScheduler=object)
_ensure_stub("uvicorn", run=lambda *a, **k: None)

# --- repo-agent/agents/llm.py heavy import --------------------------------
_ensure_stub("ollama", chat=lambda *a, **k: {"message": {"content": ""}})
