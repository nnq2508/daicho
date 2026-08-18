"""The append-only log: validation, ordering, monthly split, projections."""
from __future__ import annotations

import json

import pytest

from daicho import events


def test_writer_is_mandatory(cfg):
    with pytest.raises(events.EventValidationError):
        events.append(cfg.events_dir, {"type": "episode_added", "at": events.now_iso()})
    with pytest.raises(events.EventValidationError):
        events.append(cfg.events_dir, {"type": "x", "at": events.now_iso(), "writer": ""})
    assert list(events.iter_events(cfg.events_dir)) == []


def test_append_assigns_id_and_keeps_caller_dict_intact(cfg):
    original = {"type": "note", "at": "2031-05-04T09:00:00+09:00", "writer": "w"}
    event_id = events.append(cfg.events_dir, original)
    assert event_id.startswith("ev_")
    assert "id" not in original
    assert [e["id"] for e in events.iter_events(cfg.events_dir)] == [event_id]


def test_events_split_by_month_and_iterate_in_order(cfg):
    for at in ("2031-05-31T23:00:00+09:00", "2031-06-01T00:30:00+09:00",
               "2031-06-02T08:00:00+09:00"):
        events.emit(cfg.events_dir, "tick", "w", at=at)
    files = sorted(p.name for p in cfg.events_dir.glob("*.jsonl"))
    assert files == ["2031-05.jsonl", "2031-06.jsonl"]
    ats = [e["at"] for e in events.iter_events(cfg.events_dir)]
    assert ats == sorted(ats)


def test_filters_by_type_and_since(cfg):
    events.emit(cfg.events_dir, "a", "w", at="2031-05-01T00:00:00+09:00")
    events.emit(cfg.events_dir, "b", "w", at="2031-06-01T00:00:00+09:00")
    events.emit(cfg.events_dir, "a", "w", at="2031-07-01T00:00:00+09:00")

    assert len(list(events.iter_events(cfg.events_dir, types=["a"]))) == 2
    since = list(events.iter_events(cfg.events_dir, since="2031-06-01T00:00:00+09:00"))
    assert [e["type"] for e in since] == ["b", "a"]


def test_naive_timestamps_do_not_break_since_filtering(cfg):
    events.emit(cfg.events_dir, "a", "w", at="2031-05-01T00:00:00")
    assert len(list(events.iter_events(cfg.events_dir, since="2031-04-01T00:00:00"))) == 1


def test_corrupt_line_is_skipped_not_fatal(cfg):
    events.emit(cfg.events_dir, "a", "w", at="2031-05-01T00:00:00+09:00")
    path = cfg.events_dir / "2031-05.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{ this is not json\n")
    events.emit(cfg.events_dir, "b", "w", at="2031-05-02T00:00:00+09:00")

    kinds = [e["type"] for e in events.iter_events(cfg.events_dir)]
    assert kinds == ["a", "b"]


def test_session_cursors_take_the_furthest_turn(cfg):
    for turn_range in ([0, 4], [4, 9], [0, 4]):
        events.emit(cfg.events_dir, "episode_added", "w", episode_id=f"ep{turn_range}",
                    source={"session_id": "s1", "turn_range": turn_range})
    events.emit(cfg.events_dir, "episode_added", "w", episode_id="other",
                source={"session_id": "s2", "turn_range": [0, 2]})
    assert events.session_cursors(cfg.events_dir) == {"s1": 9, "s2": 2}


def test_projections_ignore_malformed_records(cfg):
    events.emit(cfg.events_dir, "episode_added", "w", episode_id="ep1")  # no source
    events.emit(cfg.events_dir, "episode_added", "w",
                source={"session_id": "s1"})  # no episode_id, no range
    assert events.episode_ids(cfg.events_dir) == {"ep1"}
    assert events.session_cursors(cfg.events_dir) == {}


def test_scanned_ids_projection(cfg):
    events.emit(cfg.events_dir, "entity_scanned", "w", episode_id="ep1")
    events.emit(cfg.events_dir, "entity_scanned", "w", episode_id="ep1")
    events.emit(cfg.events_dir, "entity_scanned", "w", episode_id="ep2")
    assert events.scanned_ids(cfg.events_dir, "entity_scanned") == {"ep1", "ep2"}


def test_unicode_is_stored_readable_not_escaped(cfg):
    events.emit(cfg.events_dir, "note", "w", at="2031-05-01T00:00:00+09:00", body="日本語")
    raw = (cfg.events_dir / "2031-05.jsonl").read_text(encoding="utf-8")
    assert "日本語" in raw
    assert json.loads(raw.splitlines()[0])["body"] == "日本語"


def test_missing_directory_iterates_empty(tmp_path):
    assert list(events.iter_events(tmp_path / "nope")) == []
