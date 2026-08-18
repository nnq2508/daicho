"""Ingest: idempotency, incremental turns, archives, durability canaries."""
from __future__ import annotations

import gzip
import io
import json

from conftest import write_session

from daicho import events, ingest


def test_records_one_episode_per_session(cfg):
    write_session(cfg, "s1", ["hello", "world"])
    write_session(cfg, "s2", ["another"])

    assert ingest.run(cfg, out=io.StringIO()) == 0
    recorded = list(events.iter_events(cfg.events_dir, types=["episode_added"]))
    assert len(recorded) == 2
    assert {e["source"]["session_id"] for e in recorded} == {"s1", "s2"}
    assert all(e["writer"].endswith("ingest") for e in recorded)


def test_second_run_is_a_no_op(cfg):
    write_session(cfg, "s1", ["hello"])
    ingest.run(cfg, out=io.StringIO())
    ingest.run(cfg, out=io.StringIO())
    assert len(list(events.iter_events(cfg.events_dir, types=["episode_added"]))) == 1


def test_only_new_turns_are_ingested(cfg):
    write_session(cfg, "s1", ["one", "two"])
    ingest.run(cfg, out=io.StringIO())
    write_session(cfg, "s1", ["one", "two", "three"])
    ingest.run(cfg, out=io.StringIO())

    ranges = [e["source"]["turn_range"]
              for e in events.iter_events(cfg.events_dir, types=["episode_added"])]
    assert ranges == [[0, 2], [2, 3]]


def test_dry_run_writes_nothing(cfg):
    write_session(cfg, "s1", ["hello"])
    out = io.StringIO()
    assert ingest.run(cfg, dry_run=True, out=out) == 0
    assert "dry-run" in out.getvalue()
    assert list(events.iter_events(cfg.events_dir)) == []


def test_episode_id_survives_archiving(cfg):
    """The identity of an episode must not depend on where the file lives."""
    turns = write_session(cfg, "s1", ["hello"])
    ingest.run(cfg, out=io.StringIO())
    before = next(iter(events.episode_ids(cfg.events_dir)))

    archive = cfg.sessions_dir / "archive" / "2031-05"
    archive.mkdir(parents=True)
    with gzip.open(archive / "s1.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(turns, handle, ensure_ascii=False)
    (cfg.sessions_dir / "s1.json").unlink()

    assert ingest.run(cfg, out=io.StringIO()) == 0
    assert events.episode_ids(cfg.events_dir) == {before}


def test_live_copy_wins_over_archive(cfg):
    turns = write_session(cfg, "s1", ["live version", "extra turn"])
    archive = cfg.sessions_dir / "archive" / "2031-05"
    archive.mkdir(parents=True)
    with gzip.open(archive / "s1.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(turns[:1], handle, ensure_ascii=False)

    ingest.run(cfg, out=io.StringIO())
    ranges = [e["source"]["turn_range"]
              for e in events.iter_events(cfg.events_dir, types=["episode_added"])]
    assert ranges == [[0, 2]]


def test_canary_detects_a_vanished_source(cfg):
    write_session(cfg, "s1", ["hello"])
    ingest.run(cfg, out=io.StringIO())
    (cfg.sessions_dir / "s1.json").unlink()

    assert ingest.run(cfg, out=io.StringIO()) == 1
    assert len(canary_problems(cfg)) == 1


def canary_problems(cfg):
    return ingest.canary_missing_sources(cfg.events_dir, cfg.sessions_dir)


def test_canary_detects_a_gap_in_the_archive(cfg):
    for month in ("2031-01", "2031-03"):
        (cfg.sessions_dir / "archive" / month).mkdir(parents=True)
    problems = ingest.canary_archive_gaps(cfg.sessions_dir)
    assert len(problems) == 1 and "2031-02" in problems[0]

    (cfg.sessions_dir / "archive" / "2031-02").mkdir()
    assert ingest.canary_archive_gaps(cfg.sessions_dir) == []


def test_unreadable_session_does_not_stop_the_run(cfg):
    (cfg.sessions_dir / "broken.json").write_text("{not json", encoding="utf-8")
    write_session(cfg, "s1", ["hello"])
    assert ingest.run(cfg, out=io.StringIO()) == 0
    assert len(list(events.iter_events(cfg.events_dir, types=["episode_added"]))) == 1


def test_edited_history_produces_a_distinct_episode_id(cfg):
    """Rewriting past turns changes the content hash, so the edit is visible."""
    turns_a = write_session(cfg, "s1", ["original"])
    first = ingest.compute_episode_id("s1", 0, 1, turns_a)
    turns_b = write_session(cfg, "s1", ["tampered"])
    second = ingest.compute_episode_id("s1", 0, 1, turns_b)
    assert first != second
