"""Entity ledger: canonical names, aliases and confusable pairs.

The ledger answers two questions that plain text search cannot:

* *Which surface forms mean the same thing?* -- so that a query for a short
  nickname also searches the rare full name, whose inverse document frequency
  is what actually ranks the right chunk first.
* *Which surface forms are dangerously similar but distinct?* -- two people
  whose names differ by one syllable will be conflated by any fuzzy matcher,
  and once conflated the error spreads to every file written afterwards.
  ``confusable_with`` records the pair explicitly so the mistake is made once
  and then permanently blocked.

The JSON files under ``registry/`` are a **derived artifact**: every accepted
entity is an ``entity_confirmed`` (or ``alias_added``) event first, and
:func:`rebuild` regenerates the files from the log alone.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import events
from .config import Config

#: Entity type -> file name. Extra types can be added here without touching
#: anything else; unknown types coming from an LLM are coerced to ``thing``.
REGISTRY_FILES = {
    "person": "persons.json",
    "place": "places.json",
    "thing": "things.json",
}
ENTITY_TYPES = tuple(REGISTRY_FILES)
DEFAULT_TYPE = "thing"

_WS_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalize(name: str) -> str:
    """Comparison form: whitespace removed, case folded."""
    return _WS_RE.sub("", name).lower()


def coerce_type(value: str | None) -> str:
    return value if value in REGISTRY_FILES else DEFAULT_TYPE


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("_", name.lower()).strip("_")
    if slug:
        return slug
    # Non-latin names slugify to nothing; fall back to a stable digest so the
    # identifier is still deterministic across runs.
    import hashlib

    return "e" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]


@dataclass
class Registry:
    """In-memory view of the ledger."""

    docs: dict[str, dict] = field(default_factory=dict)

    # ------------------------------------------------------------- accessors
    def entities(self, entity_type: str | None = None):
        for etype, doc in self.docs.items():
            if entity_type and etype != entity_type:
                continue
            for entity in doc.get("entities", []):
                yield etype, entity

    def names_of(self, entity: dict) -> list[str]:
        names = [entity.get("canonical_name") or ""] + list(entity.get("aliases") or [])
        return [n for n in names if n]

    @property
    def alias_index(self) -> dict[str, dict]:
        """normalized surface form -> entity record."""
        index: dict[str, dict] = {}
        for _etype, entity in self.entities():
            for name in self.names_of(entity):
                index[normalize(name)] = entity
        return index

    def find(self, name: str) -> dict | None:
        """Exact (normalized) lookup of any surface form."""
        return self.alias_index.get(normalize(name))

    def find_by_id(self, entity_id: str) -> dict | None:
        for _etype, entity in self.entities():
            if entity.get("id") == entity_id:
                return entity
        return None

    def collision(self, name: str) -> dict | None:
        """An existing entity whose name overlaps ``name`` without being equal.

        Substring containment in either direction is the cheap approximation of
        "a human would plausibly mix these up". It is deliberately eager: a
        false positive costs one review item, a false negative costs a merged
        identity that is expensive to unpick later.
        """
        norm = normalize(name)
        if len(norm) < 2:
            return None
        for existing, entity in self.alias_index.items():
            if len(existing) < 2 or existing == norm:
                continue
            if norm in existing or existing in norm:
                return entity
        return None

    # -------------------------------------------------------------- mutation
    def add(
        self,
        entity_type: str,
        canonical_name: str,
        aliases: list[str] | None = None,
        note: str = "",
        sources: list[str] | None = None,
        confusable_with: list[str] | None = None,
        added_by: str = "",
        at: str = "",
        status: str = "confirmed",
    ) -> dict:
        """Insert (or return the existing) record for ``canonical_name``."""
        entity_type = coerce_type(entity_type)
        existing = self.find(canonical_name)
        if existing is not None:
            return existing
        record = {
            "id": f"{entity_type}:{slugify(canonical_name)}",
            "canonical_name": canonical_name,
            "aliases": list(aliases or []),
            "confusable_with": list(confusable_with or []),
            "note": note[:500],
            "sources": list(sources or [])[:5],
            "status": status,
            "added_by": added_by,
            "first_seen": at or events.now_iso(),
        }
        doc = self.docs.setdefault(
            entity_type, {"version": 1, "type": entity_type, "entities": []})
        doc.setdefault("entities", []).append(record)
        for other_id in record["confusable_with"]:
            other = self.find_by_id(other_id)
            if other is not None and record["id"] not in (other.get("confusable_with") or []):
                other.setdefault("confusable_with", []).append(record["id"])
        return record

    def add_alias(self, entity_id: str, alias: str) -> dict | None:
        """Attach a surface form to an existing entity."""
        entity = self.find_by_id(entity_id)
        if entity is None:
            return None
        if normalize(alias) not in {normalize(n) for n in self.names_of(entity)}:
            entity.setdefault("aliases", []).append(alias)
        return entity

    def save(self, registry_dir: Path) -> None:
        registry_dir.mkdir(parents=True, exist_ok=True)
        for entity_type, filename in REGISTRY_FILES.items():
            doc = self.docs.get(entity_type)
            if doc is None:
                continue
            (registry_dir / filename).write_text(
                json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# ------------------------------------------------------------------- loading
def load(registry_dir: Path) -> Registry:
    docs: dict[str, dict] = {}
    for entity_type, filename in REGISTRY_FILES.items():
        path = Path(registry_dir) / filename
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"corrupt registry file: {path} ({exc})") from exc
        else:
            doc = {"version": 1, "type": entity_type, "entities": []}
        docs[entity_type] = doc
    return Registry(docs=docs)


def rebuild(cfg: Config) -> Registry:
    """Regenerate the ledger from the event log and write it out.

    This is the property that makes the log authoritative: the registry files
    can be deleted at any time and reconstructed exactly.
    """
    registry = Registry(docs={
        etype: {"version": 1, "type": etype, "entities": []} for etype in REGISTRY_FILES
    })
    for event in events.iter_events(cfg.events_dir, types=["entity_confirmed", "alias_added"]):
        if event["type"] == "entity_confirmed":
            registry.add(
                entity_type=event.get("entity_type", DEFAULT_TYPE),
                canonical_name=event.get("name", ""),
                aliases=event.get("aliases") or [],
                note=event.get("note", ""),
                sources=event.get("sources") or [],
                confusable_with=event.get("confusable_with") or [],
                added_by=event.get("writer", ""),
                at=event.get("at", ""),
            )
        else:
            registry.add_alias(event.get("entity_id", ""), event.get("alias", ""))
    registry.save(cfg.registry_dir)
    return registry


# ------------------------------------------------------------ search support
def alias_map(cfg: Config) -> dict[str, list[str]]:
    """Surface form -> every surface form of the same entity.

    Used to widen a query: searching a two-character nickname alone drowns in
    common documents, but the rare canonical name added to the same OR clause
    lets the ranking function surface the right chunk on its own. Generic
    relationship words are excluded in both directions -- they match every
    person in the ledger, so expanding them only adds noise.
    """
    generic = {normalize(word) for word in cfg.generic_aliases}
    registry = load(cfg.registry_dir)
    mapping: dict[str, list[str]] = {}
    for _etype, entity in registry.entities():
        names = [n for n in registry.names_of(entity) if normalize(n) not in generic]
        if len(names) < 2:
            continue
        for name in names:
            bucket = mapping.setdefault(name, [])
            for other in names:
                if other not in bucket:
                    bucket.append(other)
    return mapping
