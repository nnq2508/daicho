#!/usr/bin/env python3
"""End-to-end walk-through on fictional data, using a mock language model.

Run it from anywhere::

    python examples/quickstart.py            # uses a throwaway temp directory
    python examples/quickstart.py /tmp/demo  # keeps the home directory around

It exercises every stage: ingest, harvest, automatic promotion, the collision
path into the review queue, a human decision, index build, and search -- with
no network access and no dependencies.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memledger import Config, extract, ingest, registry, review, search  # noqa: E402

HERE = Path(__file__).resolve().parent
SAMPLES = HERE / "sample_sessions"
MOCK_LLM = HERE / "mock_llm.py"

NOTE = """# Community cleanup

## Status
The May cleanup is done. Attendance was 24 people.

## Next
みなと市民センターの予約は3ヶ月前から。10月にもう一度やるなら、
7月1日に予約状況を確認する必要がある。
"""

LATER_SESSION = [
    {"role": "user",
     "content": "自治会の山田さんから連絡があって、秋の清掃も手伝えるとのこと。"
                "あと Pico Router 7 はやっぱり受付で使うことにした。",
     "timestamp": "2031-05-20T21:10:00+09:00"},
    {"role": "assistant",
     "content": "自治会の山田さんを秋の清掃の協力者として控えておきます。"
                "Pico Router 7 は持ち物リストに戻しました。",
     "timestamp": "2031-05-20T21:11:00+09:00"},
]


def banner(text: str) -> None:
    print(f"\n=== {text} " + "=" * max(0, 60 - len(text)))


def main() -> int:
    keep = len(sys.argv) > 1
    home = Path(sys.argv[1]) if keep else Path(tempfile.mkdtemp(prefix="memledger-demo-"))
    cfg = Config.load(home, llm_cmd=f"{sys.executable} {MOCK_LLM}").ensure_dirs()
    print(f"home: {cfg.base_dir}")

    for path in sorted(SAMPLES.glob("*.json")):
        shutil.copy(path, cfg.sessions_dir / path.name)
    (cfg.notes_dir / "community-cleanup.md").write_text(NOTE, encoding="utf-8")

    banner("1. ingest: session turns become episodes")
    if ingest.run(cfg) != 0:
        return 1

    banner("2. extract: harvest entities and commitments")
    if extract.run(cfg) != 0:
        return 1
    ledger = registry.load(cfg.registry_dir)
    confirmed = [e["canonical_name"] for _t, e in ledger.entities()]
    print(f"auto-confirmed entities: {confirmed}")
    print("(Pico Router 7 is still a candidate: seen in 2 episodes, "
          "the threshold is 3)")

    banner("3. a fourth session arrives, naming someone easily confused")
    (cfg.sessions_dir / "sess-2031-05-20.json").write_text(
        json.dumps(LATER_SESSION, ensure_ascii=False, indent=2), encoding="utf-8")
    if ingest.run(cfg) != 0:
        return 1
    if extract.run(cfg) != 0:
        return 1
    ledger = registry.load(cfg.registry_dir)
    print(f"entities now: {[e['canonical_name'] for _t, e in ledger.entities()]}")
    print("(Pico Router 7 reached three episodes and was promoted automatically)")

    banner("4. review queue: what the machine refused to decide")
    for proposal in review.pending(cfg):
        print("  " + review.describe(proposal))

    entity_proposals = review.pending(cfg, kind="entity")
    if entity_proposals:
        target = entity_proposals[0]
        result = review.approve(cfg, target["proposal_id"], writer="demo-operator")
        print(f"\napproved {target['name']} as a separate entity -> {result}")
        ledger = registry.load(cfg.registry_dir)
        record = ledger.find(target["name"])
        print(f"confusable_with recorded: {record['confusable_with']}")
        other = ledger.find_by_id(record["confusable_with"][0]) if record["confusable_with"] else None
        if other:
            print(f"...and the link is symmetric on {other['canonical_name']}: "
                  f"{other['confusable_with']}")

    for proposal in review.pending(cfg, kind="commitment"):
        review.reject(cfg, proposal["proposal_id"], writer="demo-operator",
                      note="already on the household calendar")
        print(f"rejected commitment proposal {proposal['proposal_id']}")

    banner("5. index and search")
    stats = search.build_index(cfg)
    print(f"indexed {stats['files']} files / {stats['chunks']} chunks")

    for query in ["清掃の集合場所はどこだっけ", "花子", "Minato Civic Center"]:
        hits = search.search(cfg, query, top_k=2)
        print(f"\nquery: {query!r} -> {len(hits)} hit(s)")
        for hit in hits:
            location = Path(hit["path"]).name
            snippet = " ".join(hit["body"].split())[:80]
            note = ""
            if hit.get("alias_expanded"):
                note = f"   [alias expansion: {hit['alias_expanded']}]"
            print(f"  {location} (score {hit['score']}, coverage {hit['coverage']}){note}")
            print(f"    {snippet}…")

    banner("6. the registry is derived: delete it and rebuild from the log")
    shutil.rmtree(cfg.registry_dir)
    rebuilt = registry.rebuild(cfg)
    print(f"rebuilt from events: {[e['canonical_name'] for _t, e in rebuilt.entities()]}")

    if not keep:
        print(f"\n(removing {cfg.base_dir}; pass a path as argv[1] to keep it)")
        shutil.rmtree(cfg.base_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
