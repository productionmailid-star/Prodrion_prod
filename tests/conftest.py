"""Shared test setup.

The application imports a few libraries at module load time (``faiss``,
``uvicorn``) that the pure code paths under test do not need, so we register
lightweight stand-ins in ``sys.modules`` before the modules under test are
imported. Real packages, when installed, are left untouched.

The whole test run uses the offline ``fake`` LLM/embedding provider.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

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
_ensure_stub("uvicorn", run=lambda *a, **k: None)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point every module's Settings at a fresh throwaway database and init it.

    Returns the Settings instance. Modules bind ``settings`` at import time and
    security.py reads ``get_settings()``, so we patch both forms wherever they
    appear.
    """
    import config as _config
    import credits as _credits
    import db as _db
    import feedback_platform as _fp
    import github_app as _gh
    import jobs as _jobs
    import security as _sec
    import supabase_sync as _sb
    import worker as _worker

    s = _config.Settings(
        _env_file=None,
        environment="development",
        llm_provider="fake",
        db_path=str(tmp_path / "test.db"),
        github_clone_dir=str(tmp_path / "gh-clones"),
    )
    for mod in (_config, _credits, _db, _jobs, _fp, _gh, _sec, _sb, _worker):
        if hasattr(mod, "settings"):
            monkeypatch.setattr(mod, "settings", s, raising=False)
    monkeypatch.setattr(_sec, "get_settings", lambda: s)

    _fp.EmbeddingEngine._instance = None
    _sec.clear_workspace_cache()
    _db.init_db()
    return s
