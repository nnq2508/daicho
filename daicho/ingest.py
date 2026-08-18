"""Session logs -> ``episode_added`` events.

Deterministic and mechanical: **this module never calls an LLM**. Judgement and
extraction live in :mod:`daicho.extract`, in a separate process, so that a
failing extractor cannot stop plain recording.

Session format
--------------
The canonical on-disk format is a JSON array of turns::

    [{"role": "user", "content": "...", "timestamp": "2020-01-01T09:00:00+09:00"}, ...]

Only ``role`` and ``content`` are required. Anything else (a vendor export, a
database, a chat backend's API) is handled by writing an adapter that emits
this shape into ``sessions/``; nothing downstream is aware of the original
format.

Idempotency
-----------
``episode_id`` is derived from the **session id, the turn range and a hash of
the turn contents** -- never from a path or an mtime. Old sessions get rotated
from ``sessions/x.json`` to ``sessions/archive/YYYY-MM/x.json.gz``, and a
path-based identity would re-ingest everything the day that happens.

The incremental cursor is recomputed from the event log itself on every run
(see :func:`daicho.events.session_cursors`); there is no cursor file.

Durability canaries (checked every run; a hit is a non-zero exit, never a
silent pass):

a. gaps in the monthly ``archive/YYYY-MM/`` directories;
b. an ingested episode whose source file has disappeared from both the live
   directory and the archive.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import events
from .config import Config

ROLE_WRITER = "ingest"


# ----------------------------------------------------------------- discovery
def discover_sessions(sessions_dir: Path) -> dict[str, dict]:
    """``session_id`` -> ``{"path": Path, "archived": bool}``.

    When a session exists both live and archived (the instant during rotation)
    the live copy wins, which is the safe direction.
    """
    sessions: dict[str, dict] = {}
    if not sessions_dir.exists():
        return sessions
    for path in sorted(sessions_dir.glob("*.json")):
        sessions[path.stem] = {"path": path, "archived": False}
    archive = sessions_dir / "archive"
    if archive.exists():
        for path in sorted(archive.glob("*/*.json.gz")):
            session_id = path.name[: -len(".json.gz")]
            sessions.setdefault(session_id, {"path": path, "archived": True})
    return sessions


def load_turns(path: Path) -> list[dict]:
    """Read a session file (plain or gzipped) as a list of turns."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"unexpected format (expected a list of turns): {path}")
    return data


def _content_hash(turns: list[dict]) -> str:
    """Hash of role+content only, so re-serialisation noise does not shift ids."""
    parts = [f"{t.get('role', '')}\x00{t.get('content') or ''}" for t in turns]
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()[:16]


def compute_episode_id(session_id: str, start: int, end: int, turns: list[dict]) -> str:
    return f"ep_{session_id}_{start}-{end}_{_content_hash(turns)}"


# ------------------------------------------------------------------- canaries
def _month_range(start: str, end: str) -> list[str]:
    cur = datetime.strptime(start, "%Y-%m")
    last = datetime.strptime(end, "%Y-%m")
    out = []
    while cur <= last:
        out.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def canary_archive_gaps(sessions_dir: Path) -> list[str]:
    """Missing months between the oldest and newest archive directory.

    No archive at all is normal (nothing has rotated yet).
    """
    archive = sessions_dir / "archive"
    if not archive.exists():
        return []
    months = sorted(
        p.name for p in archive.iterdir()
        if p.is_dir() and len(p.name) == 7 and p.name[:4].isdigit() and p.name[4] == "-"
    )
    if len(months) < 2:
        return []
    existing = set(months)
    return [
        f"archive/{m}/ is missing (gap between {months[0]} and {months[-1]})"
        for m in _month_range(months[0], months[-1]) if m not in existing
    ]


def canary_missing_sources(events_dir: Path, sessions_dir: Path) -> list[str]:
    """Ingested episodes whose original session file no longer exists.

    Live -> archive is a normal transition, so an archived copy counts. Neither
    present means the durability claim has been broken and something deleted
    history behind our back.
    """
    session_ids = {
        (event.get("source") or {}).get("session_id")
        for event in events.iter_events(events_dir, types=["episode_added"])
    }
    problems = []
    for session_id in sorted(sid for sid in session_ids if sid):
        if (sessions_dir / f"{session_id}.json").exists():
            continue
        if list(sessions_dir.glob(f"archive/*/{session_id}.json.gz")):
            continue
        problems.append(
            f"session {session_id} was ingested but its source is gone "
            f"(neither live nor archived)"
        )
    return problems


# ----------------------------------------------------------------------- main
def _relpath(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def resolve_source_path(cfg: Config, stored: str) -> Path:
    """Turn the ``source.path`` of an event back into a real path."""
    path = Path(stored)
    return path if path.is_absolute() else cfg.base_dir / path


def run(cfg: Config, dry_run: bool = False, backfill: bool = False,
        writer: str | None = None, out=sys.stdout) -> int:
    """Ingest every unseen turn. Returns a process exit code."""
    writer = writer or cfg.writer(ROLE_WRITER)
    now = events.now_iso()

    problems = canary_archive_gaps(cfg.sessions_dir)
    problems += canary_missing_sources(cfg.events_dir, cfg.sessions_dir)

    cursors = events.session_cursors(cfg.events_dir)
    sessions = discover_sessions(cfg.sessions_dir)

    planned: list[dict[str, Any]] = []
    late_archives: list[str] = []

    for session_id in sorted(sessions):
        info = sessions[session_id]
        path: Path = info["path"]
        try:
            turns = load_turns(path)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            print(f"[ingest] unreadable, skipping: {path} ({exc})", file=sys.stderr)
            continue
        total = len(turns)
        start = cursors.get(session_id, 0)
        if start >= total:
            continue
        new_turns = turns[start:total]
        if info["archived"] and not backfill:
            late_archives.append(session_id)
        planned.append({
            "session_id": session_id,
            "path": path,
            "start": start,
            "end": total,
            "episode_id": compute_episode_id(session_id, start, total, new_turns),
            "archived": info["archived"],
        })

    if late_archives:
        sample = ", ".join(late_archives[:10]) + ("..." if len(late_archives) > 10 else "")
        print(
            f"[ingest] warning: {len(late_archives)} session(s) are being ingested for the "
            f"first time from the archive. In normal operation ingestion happens long "
            f"before rotation, so this points at a worker that was down or at a missed "
            f"backfill. Pass --backfill if it is intentional: {sample}",
            file=sys.stderr,
        )

    if dry_run:
        print(f"[ingest] dry-run: {len(planned)} session(s) with new turns", file=out)
        for item in planned:
            tag = " [from archive]" if item["archived"] else ""
            print(f"  {item['session_id']}: turns[{item['start']}:{item['end']}] "
                  f"-> {item['episode_id']}{tag}", file=out)
    else:
        known = events.episode_ids(cfg.events_dir)
        ingested = 0
        for item in planned:
            if item["episode_id"] in known:
                continue  # identical content already recorded: true idempotency
            events.append(cfg.events_dir, {
                "type": "episode_added",
                "episode_id": item["episode_id"],
                "at": now,
                "writer": writer,
                "source": {
                    "kind": "session",
                    "session_id": item["session_id"],
                    "path": _relpath(item["path"], cfg.base_dir),
                    "turn_range": [item["start"], item["end"]],
                },
            })
            ingested += 1
        print(f"[ingest] recorded {ingested} episode(s) out of {len(planned)} candidate(s)",
              file=out)

    if problems:
        print("[ingest] durability canary tripped (exiting non-zero):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0
