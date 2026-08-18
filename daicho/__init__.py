"""An append-only memory ledger for long-lived assistants.

The design rests on four rules:

1. The append-only event log is the source of truth. The entity ledger and the
   full-text index are derived and can be rebuilt from it at any time.
2. Every event names its writer. Nothing enters memory anonymously.
3. Scan cursors are recomputed from the log, never persisted separately.
4. Machine extraction always stops in staging. Only a narrow, explicit rule
   (no collision, seen in N distinct episodes) promotes anything without a
   human, and everything else waits in the review queue.

Typical use::

    from daicho import Config, ingest, extract, search

    cfg = Config.load("/srv/memory").ensure_dirs()
    ingest.run(cfg)
    extract.run(cfg)
    search.build_index(cfg)
    hits = search.search(cfg, "when did we agree on the deadline?")
"""
from __future__ import annotations

from .config import APP_NAME, Config, SourceSpec

__version__ = "0.1.0"
__all__ = [
    "APP_NAME",
    "Config",
    "SourceSpec",
    "events",
    "extract",
    "ingest",
    "registry",
    "review",
    "search",
    "__version__",
]


def __getattr__(name: str):
    """Import submodules lazily so ``import daicho`` stays cheap."""
    if name in __all__:
        import importlib

        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(name)
