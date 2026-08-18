"""Append-only event log: the single source of truth.

Contract
--------
* **The log is authoritative.** The entity registry and the full-text index are
  derived artifacts; deleting them and rebuilding from the log must be a safe,
  lossless operation. Never store a fact that exists only in a derived store.
* **One event per line of JSON**, in monthly files (``events/YYYY-MM.jsonl``).
* **Appends take an exclusive lock**, blocking. Appending is cheap enough that
  waiting is harmless, whereas a non-blocking attempt that gives up would
  create a new failure mode: an event silently lost because it lost a race.
* **``type``, ``at`` and ``writer`` are mandatory.** An event without a writer
  is a memory nobody is accountable for, which is exactly how several
  independent writers end up quietly corrupting a shared store. ``append``
  rejects it rather than accepting an unattributable record.
* A corrupt line is skipped with a warning instead of aborting iteration, so a
  single bad byte cannot make the entire history unreadable.

Standard library only.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO, Iterable, Iterator

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]

REQUIRED_FIELDS = ("type", "at", "writer")

LOCK_RETRY_SEC = 0.05
LOCK_TIMEOUT_SEC = 30.0


class EventValidationError(ValueError):
    """The input cannot be written as an event (missing required fields)."""


# --------------------------------------------------------------------- helpers
def now_iso() -> str:
    """Current local time with offset, seconds precision."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_at(at: str) -> datetime:
    """Parse an ISO timestamp into an aware datetime.

    A trailing ``Z`` is rewritten because ``fromisoformat`` rejects it before
    Python 3.11, and naive timestamps are assumed to be local time so that
    comparisons never raise on mixed awareness.
    """
    dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _month_key(at: str) -> str:
    return parse_at(at).strftime("%Y-%m")


def new_id(prefix: str) -> str:
    """Collision-resistant identifier. Sortability is not required."""
    return f"{prefix}_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


def _validate(event: dict) -> None:
    for name in REQUIRED_FIELDS:
        value = event.get(name)
        if not value or not isinstance(value, str):
            raise EventValidationError(
                f"event is missing the required field {name!r} "
                f"(empty strings and non-strings are rejected): {event!r}"
            )


@contextmanager
def _exclusive(handle: IO[str], path: Path) -> Iterator[None]:
    """Exclusive lock around an append, with a portable fallback."""
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.time() + LOCK_TIMEOUT_SEC
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                raise TimeoutError(f"could not acquire {lock_path}")
            time.sleep(LOCK_RETRY_SEC)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------- append
def append(events_dir: Path, event: dict) -> str:
    """Append one event to the monthly log and return its id.

    ``id`` is assigned here when the caller did not provide one. The caller's
    dictionary is never mutated.
    """
    _validate(event)
    events_dir = Path(events_dir)
    events_dir.mkdir(parents=True, exist_ok=True)

    event = dict(event)
    event_id = event.get("id") or new_id("ev")
    event["id"] = event_id

    path = events_dir / f"{_month_key(event['at'])}.jsonl"
    line = json.dumps(event, ensure_ascii=False, sort_keys=False) + "\n"

    with open(path, "a", encoding="utf-8") as handle:
        with _exclusive(handle, path):
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    return event_id


def emit(events_dir: Path, type: str, writer: str, at: str | None = None, **payload) -> str:
    """Convenience wrapper: ``emit(dir, "episode_added", writer, episode_id=...)``."""
    return append(events_dir, {"type": type, "at": at or now_iso(), "writer": writer, **payload})


# ------------------------------------------------------------------- iteration
def iter_events(
    events_dir: Path,
    types: Iterable[str] | None = None,
    since: str | None = None,
) -> Iterator[dict]:
    """Yield events oldest-first (monthly files in name order, appends in order).

    ``types`` filters on the event type, ``since`` on the ISO timestamp.
    """
    events_dir = Path(events_dir)
    if not events_dir.exists():
        return
    wanted = set(types) if types is not None else None
    since_dt = parse_at(since) if since else None
    since_month = _month_key(since) if since else None

    for path in sorted(events_dir.glob("*.jsonl")):
        if since_month and path.stem < since_month:
            continue
        with open(path, encoding="utf-8") as handle:
            for lineno, raw in enumerate(handle, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"[events] skipping corrupt line: {path}:{lineno}", file=sys.stderr)
                    continue
                if not isinstance(event, dict):
                    continue
                if wanted is not None and event.get("type") not in wanted:
                    continue
                if since_dt is not None:
                    try:
                        if parse_at(event.get("at", "")) < since_dt:
                            continue
                    except ValueError:
                        continue
                yield event


# ------------------------------------------------------------------ projections
def episode_ids(events_dir: Path) -> set[str]:
    """Every episode already recorded. The only authority on idempotency."""
    return {
        event["episode_id"]
        for event in iter_events(events_dir, types=["episode_added"])
        if event.get("episode_id")
    }


def session_cursors(events_dir: Path) -> dict[str, int]:
    """``session_id`` -> number of turns already ingested (= next start index).

    Recomputed from the log on every call rather than cached in a file. A
    cursor file is a second source of truth: when it rots, idempotency rots
    with it. If recomputation ever becomes the bottleneck, cache the *result*
    of this function, not the position it derives.
    """
    cursors: dict[str, int] = {}
    for event in iter_events(events_dir, types=["episode_added"]):
        source = event.get("source") or {}
        turn_range = source.get("turn_range")
        session_id = source.get("session_id")
        if not session_id or not turn_range or len(turn_range) != 2:
            continue
        end = turn_range[1]
        if isinstance(end, int) and end > cursors.get(session_id, 0):
            cursors[session_id] = end
    return cursors


def scanned_ids(events_dir: Path, event_type: str, key: str = "episode_id") -> set[str]:
    """Ids already marked as processed by a worker (its scan cursor)."""
    return {
        event[key]
        for event in iter_events(events_dir, types=[event_type])
        if event.get(key)
    }
