"""Human-in-the-loop review queue.

Nothing an LLM extracts reaches the ledger by itself unless it clears the
automatic bar (no collision, seen in *N* distinct episodes). Everything else --
a name that collides with an existing entity, a low-confidence guess, a
commitment with no matching reminder -- lands here and waits for a person.

The queue is a **projection of the event log**, not a directory of files:

``proposal_deferred``
    Raised but over the per-run budget. Waiting to enter the queue.
``proposal_opened``
    In the queue, awaiting a decision.
``proposal_resolved``
    Decided. ``decision`` is ``approved`` or ``rejected``.

Approving an entity proposal appends ``entity_confirmed`` (or ``alias_added``
when merging into an existing entity) and then updates the registry files, in
that order -- the log leads, the derived store follows.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from . import events, registry
from .config import Config

ROLE_WRITER = "review"

OPENED = "proposal_opened"
DEFERRED = "proposal_deferred"
RESOLVED = "proposal_resolved"


class ReviewError(RuntimeError):
    """The requested review action cannot be applied."""


# ------------------------------------------------------------------ raising
def raise_proposal(cfg: Config, payload: dict, writer: str, defer: bool = False) -> str:
    """Append a proposal event and return its id.

    ``defer=True`` parks it outside the queue; :func:`drain_deferred` promotes
    it on a later run. The distinction exists so a single noisy run cannot bury
    a person under a hundred questions, while nothing is silently dropped.
    """
    proposal_id = payload.get("proposal_id") or events.new_id("prop")
    body = dict(payload)
    body["proposal_id"] = proposal_id
    events.emit(cfg.events_dir, DEFERRED if defer else OPENED, writer, **body)
    return proposal_id


def drain_deferred(cfg: Config, writer: str, limit: int) -> int:
    """Promote up to ``limit`` deferred proposals into the queue.

    Deferral is a waiting room, not a landfill: every run empties as much of it
    as the budget allows before raising anything new.
    """
    promoted = set()
    for event in events.iter_events(cfg.events_dir, types=[OPENED]):
        if event.get("proposal_id"):
            promoted.add(event["proposal_id"])
    count = 0
    for event in events.iter_events(cfg.events_dir, types=[DEFERRED]):
        if count >= limit:
            break
        pid = event.get("proposal_id")
        if not pid or pid in promoted:
            continue
        payload = {k: v for k, v in event.items()
                   if k not in ("type", "at", "writer", "id")}
        events.emit(cfg.events_dir, OPENED, writer, **payload)
        promoted.add(pid)
        count += 1
    return count


# --------------------------------------------------------------- projections
def _decisions(cfg: Config) -> dict[str, dict]:
    return {
        event["proposal_id"]: event
        for event in events.iter_events(cfg.events_dir, types=[RESOLVED])
        if event.get("proposal_id")
    }


def all_proposals(cfg: Config, states: Iterable[str] = (OPENED, DEFERRED)) -> list[dict]:
    """Every proposal ever raised, newest last, with its current state."""
    seen: dict[str, dict] = {}
    for event in events.iter_events(cfg.events_dir, types=list(states)):
        pid = event.get("proposal_id")
        if not pid:
            continue
        record = dict(event)
        record["state"] = "queued" if event["type"] == OPENED else "deferred"
        if pid in seen and seen[pid]["state"] == "queued":
            continue  # an opened proposal outranks its earlier deferred copy
        seen[pid] = record
    decisions = _decisions(cfg)
    for pid, record in seen.items():
        decision = decisions.get(pid)
        if decision:
            record["state"] = decision.get("decision", "resolved")
            record["resolved_at"] = decision.get("at")
            record["resolution_note"] = decision.get("note", "")
    return list(seen.values())


def pending(cfg: Config, kind: str | None = None) -> list[dict]:
    """Proposals waiting for a decision, oldest first."""
    out = [p for p in all_proposals(cfg) if p["state"] == "queued"]
    if kind:
        out = [p for p in out if p.get("kind") == kind]
    return sorted(out, key=lambda p: p.get("at", ""))


def proposed_entity_names(cfg: Config) -> set[str]:
    """Normalized entity names already raised (in any state).

    Re-asking about the same name every night is how a review queue teaches
    people to ignore it.
    """
    return {
        registry.normalize(p["name"])
        for p in all_proposals(cfg) if p.get("kind") == "entity" and p.get("name")
    }


def proposed_commitment_keys(cfg: Config) -> set[str]:
    return {
        p["key"] for p in all_proposals(cfg)
        if p.get("kind") == "commitment" and p.get("key")
    }


def get(cfg: Config, proposal_id: str) -> dict:
    for proposal in all_proposals(cfg):
        if proposal["proposal_id"] == proposal_id:
            return proposal
    raise ReviewError(f"no such proposal: {proposal_id}")


# ---------------------------------------------------------------- decisions
def approve(cfg: Config, proposal_id: str, writer: str | None = None,
            merge_into: str | None = None, note: str = "") -> dict:
    """Accept a proposal and apply its effect.

    For an entity proposal: ``merge_into`` records the name as an alias of an
    existing entity, otherwise a new entity is created. When the proposal was
    raised because of a collision, the two entities are cross-linked through
    ``confusable_with`` so the same near-miss is never re-litigated.
    """
    writer = writer or cfg.writer(ROLE_WRITER)
    proposal = get(cfg, proposal_id)
    if proposal["state"] not in ("queued", "deferred"):
        raise ReviewError(f"proposal {proposal_id} is already {proposal['state']}")

    result: dict = {"proposal_id": proposal_id, "kind": proposal.get("kind")}
    if proposal.get("kind") == "entity":
        ledger = registry.load(cfg.registry_dir)
        if merge_into:
            target = ledger.find_by_id(merge_into)
            if target is None:
                raise ReviewError(f"no such entity id: {merge_into}")
            events.emit(cfg.events_dir, "alias_added", writer,
                        entity_id=merge_into, alias=proposal["name"],
                        proposal_id=proposal_id)
            ledger.add_alias(merge_into, proposal["name"])
            result["merged_into"] = merge_into
        else:
            confusable = [proposal["collides_with"]] if proposal.get("collides_with") else []
            payload = {
                "name": proposal["name"],
                "entity_type": registry.coerce_type(proposal.get("entity_type")),
                "aliases": proposal.get("aliases") or [],
                "note": proposal.get("note", ""),
                "sources": proposal.get("sources") or [],
                "confusable_with": confusable,
                "confirm_rule": "human review",
                "proposal_id": proposal_id,
            }
            events.emit(cfg.events_dir, "entity_confirmed", writer, **payload)
            record = ledger.add(
                entity_type=payload["entity_type"], canonical_name=payload["name"],
                aliases=payload["aliases"], note=payload["note"],
                sources=payload["sources"], confusable_with=confusable,
                added_by=writer)
            result["entity_id"] = record["id"]
        ledger.save(cfg.registry_dir)
    elif proposal.get("kind") == "commitment":
        # The library does not own a scheduler. Accepting a commitment records
        # the decision and lets the host application act on the event.
        events.emit(cfg.events_dir, "commitment_accepted", writer,
                    proposal_id=proposal_id, key=proposal.get("key", ""),
                    text=proposal.get("text", ""),
                    date_hint=proposal.get("date_hint", ""),
                    source_file=proposal.get("source_file", ""))

    events.emit(cfg.events_dir, RESOLVED, writer,
                proposal_id=proposal_id, decision="approved", note=note)
    return result


def reject(cfg: Config, proposal_id: str, writer: str | None = None, note: str = "") -> dict:
    writer = writer or cfg.writer(ROLE_WRITER)
    proposal = get(cfg, proposal_id)
    if proposal["state"] not in ("queued", "deferred"):
        raise ReviewError(f"proposal {proposal_id} is already {proposal['state']}")
    events.emit(cfg.events_dir, RESOLVED, writer,
                proposal_id=proposal_id, decision="rejected", note=note)
    return {"proposal_id": proposal_id, "kind": proposal.get("kind")}


# ------------------------------------------------------------------ display
def describe(proposal: dict) -> str:
    """One-line rendering for a terminal listing."""
    pid = proposal.get("proposal_id", "?")
    if proposal.get("kind") == "entity":
        body = f"entity {proposal.get('name', '?')} ({proposal.get('entity_type', '?')})"
    else:
        body = f"commitment {proposal.get('text', proposal.get('key', '?'))[:60]}"
    reason = proposal.get("reason", "")
    tail = f" -- {reason}" if reason else ""
    return f"{pid}  {body}{tail}"


def source_hint(proposal: dict, base_dir: Path | None = None) -> str:
    """Where the proposal came from, for the ``show`` view."""
    if proposal.get("source_episode_id"):
        return f"episode {proposal['source_episode_id']}"
    if proposal.get("source_file"):
        path = Path(proposal["source_file"])
        if base_dir:
            try:
                path = path.relative_to(base_dir)
            except ValueError:
                pass
        return f"file {path}"
    return ""
