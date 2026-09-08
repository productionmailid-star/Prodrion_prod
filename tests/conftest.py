"""Shared test setup.

The application imports a few libraries at module load time (``faiss``,
``apscheduler``, ``uvicorn``) that the pure code paths under test do not need,
so we register lightweight stand-ins in ``sys.modules`` before the modules
under test are imported. Real packages, when installed, are left untouched.

The whole test run uses the offline ``fake`` LLM/embedding provider.
"""

from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("PRODRION_LLM_PROVIDER", "fake")


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


# --- feedback_platform.py heavy imports ------------------------------------
# The LLM / embedding stack is exercised through the "fake" provider
# (config PRODRION_LLM_PROVIDER), so only these remain to stub.
_ensure_stub("faiss")
_ensure_stub("apscheduler")
_ensure_stub("apscheduler.schedulers")
_ensure_stub("apscheduler.schedulers.background", BackgroundScheduler=object)
_ensure_stub("uvicorn", run=lambda *a, **k: None)
