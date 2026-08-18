#!/usr/bin/env python3
"""A deterministic stand-in for a language model, used by the quickstart.

The library talks to a model through one narrow interface: a shell command
that reads a prompt on stdin and writes a JSON object on stdout. Anything that
honours that contract works -- a vendor CLI, a curl wrapper, a local model, or
this dictionary lookup.

Run the quickstart against a real model by pointing ``--llm-cmd`` at it
instead; nothing else changes.
"""
from __future__ import annotations

import json
import re
import sys

# Surface form -> canonical record. A real model generalises; this table is
# enough to show the promotion, collision and threshold paths.
KNOWN = {
    "山田花子": ("山田花子", "person", ["Hanako Yamada", "花子"], "high"),
    "Hanako Yamada": ("山田花子", "person", ["Hanako Yamada", "花子"], "high"),
    "花子": ("山田花子", "person", ["Hanako Yamada", "花子"], "high"),
    "みなと市民センター": ("みなと市民センター", "place", ["Minato Civic Center"], "high"),
    "Minato Civic Center": ("みなと市民センター", "place", ["Minato Civic Center"], "high"),
    "Pico Router 7": ("Pico Router 7", "thing", [], "high"),
    # A bare family name: the ledger already knows a 山田, so this one cannot
    # be resolved automatically and must go to a human.
    "山田さん": ("山田", "person", [], "high"),
}


def entities_in(text: str) -> list[dict]:
    found: dict[str, dict] = {}
    for surface, (name, etype, aliases, confidence) in KNOWN.items():
        if surface in text and name not in found:
            found[name] = {
                "name": name,
                "type": etype,
                "aliases": aliases,
                "confidence": confidence,
                "context": f"mentioned as {surface}",
            }
    return list(found.values())


def handle_entities(payload: str) -> dict:
    blocks = re.split(r"^EPISODE_ID: ", payload, flags=re.M)[1:]
    episodes = []
    for block in blocks:
        episode_id, _, body = block.partition("\n")
        episodes.append({
            "episode_id": episode_id.strip(),
            "entities": entities_in(body),
        })
    return {"episodes": episodes}


def handle_commitments(payload: str) -> dict:
    commitments = []
    if "予約" in payload and "7月" in payload:
        commitments.append({
            "text": "秋の清掃に向けて、7月1日にみなと市民センターの予約状況を確認する",
            "date_hint": "2031-07-01",
            "key": "秋清掃の予約確認",
        })
    return {"commitments": commitments}


def main() -> int:
    payload = sys.stdin.read()
    result = handle_commitments(payload) if "FILE:" in payload else handle_entities(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
