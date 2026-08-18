"""Shared fixtures. Every test writes inside ``tmp_path`` and nowhere else."""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memledger import Config  # noqa: E402
from memledger.ingest import compute_episode_id  # noqa: E402
from memledger import events  # noqa: E402


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """An isolated home directory with no LLM configured."""
    return Config.load(tmp_path / "home").ensure_dirs()


def write_session(cfg: Config, session_id: str, texts: list[str],
                  role: str = "user") -> list[dict]:
    turns = [{"role": role, "content": text, "timestamp": "2031-05-04T09:00:00+09:00"}
             for text in texts]
    (cfg.sessions_dir / f"{session_id}.json").write_text(
        json.dumps(turns, ensure_ascii=False), encoding="utf-8")
    return turns


def add_episode(cfg: Config, session_id: str, turns: list[dict],
                writer: str = "test") -> str:
    """Record an episode directly, bypassing the ingest worker."""
    episode_id = compute_episode_id(session_id, 0, len(turns), turns)
    events.append(cfg.events_dir, {
        "type": "episode_added",
        "episode_id": episode_id,
        "at": "2031-05-04T09:00:00+09:00",
        "writer": writer,
        "source": {
            "kind": "session",
            "session_id": session_id,
            "path": f"sessions/{session_id}.json",
            "turn_range": [0, len(turns)],
        },
    })
    return episode_id


def make_executable(path: Path) -> Path:
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def mock_llm(tmp_path: Path, entities: list[dict] | None = None,
             commitments: list[dict] | None = None, name: str = "mock_llm.py") -> str:
    """Write a stub model and return the shell command that runs it.

    It echoes the same entity list for every episode it is shown, and a fixed
    commitment list for the note prompt, which is enough to drive the promotion
    and staging paths deterministically.
    """
    script = tmp_path / name
    script.write_text(
        "import json, re, sys\n"
        "data = sys.stdin.read()\n"
        f"entities = {json.dumps(entities or [], ensure_ascii=False)}\n"
        f"commitments = {json.dumps(commitments or [], ensure_ascii=False)}\n"
        "if 'FILE:' in data:\n"
        "    print(json.dumps({'commitments': commitments}, ensure_ascii=False))\n"
        "else:\n"
        "    ids = re.findall(r'EPISODE_ID: (\\S+)', data)\n"
        "    print(json.dumps({'episodes': [\n"
        "        {'episode_id': i, 'entities': entities} for i in ids]},\n"
        "        ensure_ascii=False))\n",
        encoding="utf-8")
    make_executable(script)
    return f"{sys.executable} {script}"


def broken_llm(tmp_path: Path, name: str = "broken_llm.py") -> str:
    """A model command that always returns unparsable output."""
    script = tmp_path / name
    script.write_text("print('not json at all')\n", encoding="utf-8")
    make_executable(script)
    return f"{sys.executable} {script}"


def seed_registry(cfg: Config, canonical: str, aliases: list[str],
                  entity_type: str = "person") -> None:
    from memledger import registry

    ledger = registry.load(cfg.registry_dir)
    ledger.add(entity_type=entity_type, canonical_name=canonical, aliases=aliases,
               added_by="test")
    ledger.save(cfg.registry_dir)
