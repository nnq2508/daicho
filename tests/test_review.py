"""Review queue: staging, decisions, and rebuilding the ledger from the log."""
from __future__ import annotations

import pytest
from conftest import seed_registry

from memledger import events, registry, review


def entity_proposal(cfg, name="山田", collides_with=None, defer=False) -> str:
    return review.raise_proposal(cfg, {
        "kind": "entity",
        "name": name,
        "entity_type": "person",
        "aliases": [],
        "note": "context line",
        "reason": "collides with an existing entity",
        "collides_with": collides_with,
        "sources": ["episode:ep1"],
        "source_episode_id": "ep1",
    }, writer="test", defer=defer)


def commitment_proposal(cfg, key="permit renewal") -> str:
    return review.raise_proposal(cfg, {
        "kind": "commitment",
        "key": key,
        "text": "Renew the permit",
        "date_hint": "2031-09",
        "source_file": "notes/plan.md",
        "reason": "dated statement with no matching reminder",
    }, writer="test")


def test_pending_lists_only_undecided_proposals(cfg):
    first = entity_proposal(cfg, "山田")
    commitment_proposal(cfg)
    assert {p["proposal_id"] for p in review.pending(cfg)} == {
        first, review.pending(cfg, kind="commitment")[0]["proposal_id"]}

    review.reject(cfg, first, writer="tester")
    remaining = review.pending(cfg)
    assert len(remaining) == 1 and remaining[0]["kind"] == "commitment"
    assert review.get(cfg, first)["state"] == "rejected"


def test_deferred_proposals_are_not_in_the_queue_until_drained(cfg):
    proposal_id = entity_proposal(cfg, "山田", defer=True)
    assert review.pending(cfg) == []
    assert review.all_proposals(cfg)[0]["state"] == "deferred"

    assert review.drain_deferred(cfg, writer="test", limit=5) == 1
    assert [p["proposal_id"] for p in review.pending(cfg)] == [proposal_id]

    # Draining again must not duplicate it.
    assert review.drain_deferred(cfg, writer="test", limit=5) == 0
    assert len(review.pending(cfg)) == 1


def test_drain_respects_its_limit(cfg):
    for i in range(4):
        entity_proposal(cfg, f"name{i}", defer=True)
    assert review.drain_deferred(cfg, writer="test", limit=2) == 2
    assert len(review.pending(cfg)) == 2
    assert review.drain_deferred(cfg, writer="test", limit=2) == 2
    assert len(review.pending(cfg)) == 4


def test_approving_creates_the_entity_and_records_the_confusable_pair(cfg):
    seed_registry(cfg, "山田花子", ["Hanako Yamada"])
    other = registry.load(cfg.registry_dir).find("山田花子")
    proposal_id = entity_proposal(cfg, "山田", collides_with=other["id"])

    result = review.approve(cfg, proposal_id, writer="operator")
    ledger = registry.load(cfg.registry_dir)
    record = ledger.find("山田")
    assert record is not None and record["id"] == result["entity_id"]
    assert record["confusable_with"] == [other["id"]]
    # The link is symmetric: neither direction may be confused later.
    assert record["id"] in ledger.find("山田花子")["confusable_with"]
    assert review.get(cfg, proposal_id)["state"] == "approved"


def test_approving_with_merge_adds_an_alias_instead(cfg):
    seed_registry(cfg, "山田花子", ["Hanako Yamada"])
    target = registry.load(cfg.registry_dir).find("山田花子")
    proposal_id = entity_proposal(cfg, "花子", collides_with=target["id"])

    review.approve(cfg, proposal_id, writer="operator", merge_into=target["id"])
    ledger = registry.load(cfg.registry_dir)
    assert len(list(ledger.entities())) == 1
    assert "花子" in ledger.find("山田花子")["aliases"]


def test_merging_into_an_unknown_entity_is_refused(cfg):
    proposal_id = entity_proposal(cfg, "花子")
    with pytest.raises(review.ReviewError):
        review.approve(cfg, proposal_id, merge_into="person:nope")
    assert review.get(cfg, proposal_id)["state"] == "queued"


def test_a_decision_cannot_be_taken_twice(cfg):
    proposal_id = entity_proposal(cfg, "山田")
    review.approve(cfg, proposal_id, writer="operator")
    with pytest.raises(review.ReviewError, match="already approved"):
        review.approve(cfg, proposal_id, writer="operator")
    with pytest.raises(review.ReviewError):
        review.reject(cfg, proposal_id, writer="operator")
    assert len(list(registry.load(cfg.registry_dir).entities())) == 1


def test_unknown_proposal_id_is_an_error(cfg):
    with pytest.raises(review.ReviewError, match="no such proposal"):
        review.get(cfg, "prop_does_not_exist")


def test_rejecting_leaves_the_ledger_untouched(cfg):
    proposal_id = entity_proposal(cfg, "山田")
    review.reject(cfg, proposal_id, writer="operator", note="not a real person")
    assert list(registry.load(cfg.registry_dir).entities()) == []
    resolved = list(events.iter_events(cfg.events_dir, types=[review.RESOLVED]))
    assert resolved[0]["decision"] == "rejected"
    assert resolved[0]["note"] == "not a real person"


def test_approving_a_commitment_records_it_for_the_host_application(cfg):
    proposal_id = commitment_proposal(cfg)
    review.approve(cfg, proposal_id, writer="operator")
    accepted = list(events.iter_events(cfg.events_dir, types=["commitment_accepted"]))
    assert len(accepted) == 1
    assert accepted[0]["key"] == "permit renewal"
    assert accepted[0]["date_hint"] == "2031-09"


def test_every_decision_is_attributed(cfg):
    review.reject(cfg, entity_proposal(cfg, "A"), writer="alice")
    review.approve(cfg, entity_proposal(cfg, "B"), writer="bob")
    writers = {e["writer"] for e in events.iter_events(cfg.events_dir,
                                                       types=[review.RESOLVED])}
    assert writers == {"alice", "bob"}


def test_the_ledger_can_be_rebuilt_from_the_event_log_alone(cfg):
    seed_registry(cfg, "山田花子", ["Hanako Yamada"])
    # Seeding wrote a file without an event, so make the log the full story.
    events.emit(cfg.events_dir, "entity_confirmed", "test", name="山田花子",
                entity_type="person", aliases=["Hanako Yamada"], sources=[],
                confusable_with=[])
    review.approve(cfg, entity_proposal(cfg, "山田"), writer="operator")
    review.approve(cfg, entity_proposal(cfg, "花子"), writer="operator",
                   merge_into=registry.load(cfg.registry_dir).find("山田花子")["id"])

    before = registry.load(cfg.registry_dir)
    import shutil
    shutil.rmtree(cfg.registry_dir)

    after = registry.rebuild(cfg)
    assert ({e["canonical_name"] for _t, e in after.entities()}
            == {e["canonical_name"] for _t, e in before.entities()})
    assert "花子" in after.find("山田花子")["aliases"]
    assert after.find("山田") is not None


def test_describe_is_readable(cfg):
    proposal = review.get(cfg, entity_proposal(cfg, "山田"))
    line = review.describe(proposal)
    assert "山田" in line and "entity" in line
    assert review.source_hint(proposal) == "episode ep1"
