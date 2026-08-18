"""Command line interface.

    daicho init                 create the home directory layout
    daicho ingest               record new session turns as episodes
    daicho extract              run the LLM harvest (entities, commitments)
    daicho review list|show|approve|reject
    daicho search "..."         query the index
    daicho reindex              rebuild the full-text index
    daicho rebuild-registry     regenerate the ledger from the event log

Global options ``--home`` and ``--llm-cmd`` override the environment.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import extract, ingest, registry, review, search
from .config import APP_NAME, ENV_HOME, ENV_LLM_CMD, Config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=f"{APP_NAME}: an append-only memory ledger with staged extraction.",
    )
    parser.add_argument("--home", help=f"home directory (default: ${ENV_HOME})")
    parser.add_argument("--llm-cmd",
                        help=f"shell command: prompt on stdin, JSON on stdout "
                             f"(default: ${ENV_LLM_CMD})")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the home directory layout")

    p_ingest = sub.add_parser("ingest", help="record new session turns as episodes")
    p_ingest.add_argument("--dry-run", action="store_true")
    p_ingest.add_argument("--backfill", action="store_true",
                          help="first bulk import; silences the archive warning")

    p_extract = sub.add_parser("extract", help="run the LLM harvest")
    p_extract.add_argument("--dry-run", action="store_true")
    p_extract.add_argument("--skip-entities", action="store_true")
    p_extract.add_argument("--skip-commitments", action="store_true")

    p_review = sub.add_parser("review", help="inspect and decide staged proposals")
    review_sub = p_review.add_subparsers(dest="review_command", required=True)
    p_list = review_sub.add_parser("list", help="proposals awaiting a decision")
    p_list.add_argument("--kind", choices=["entity", "commitment"])
    p_list.add_argument("--all", action="store_true", help="include resolved proposals")
    p_show = review_sub.add_parser("show", help="full payload of one proposal")
    p_show.add_argument("proposal_id")
    p_approve = review_sub.add_parser("approve", help="accept a proposal")
    p_approve.add_argument("proposal_id")
    p_approve.add_argument("--merge-into", metavar="ENTITY_ID",
                           help="record the name as an alias of an existing entity")
    p_approve.add_argument("--note", default="")
    p_reject = review_sub.add_parser("reject", help="discard a proposal")
    p_reject.add_argument("proposal_id")
    p_reject.add_argument("--note", default="")

    p_search = sub.add_parser("search", help="query the index")
    p_search.add_argument("query")
    p_search.add_argument("--terms", nargs="*", help="explicit search terms")
    p_search.add_argument("-k", "--top-k", type=int, default=5)
    p_search.add_argument("--kinds", nargs="*")
    p_search.add_argument("--min-ratio", type=float, default=0.35)
    p_search.add_argument("--min-coverage", type=float, default=0.0)
    p_search.add_argument("--budget", type=int, help="token budget for the output")
    p_search.add_argument("--recency-days", type=float,
                          help="time constant for a recency bonus")
    p_search.add_argument("--no-alias", action="store_true",
                          help="disable ledger alias expansion")
    p_search.add_argument("--hint", action="store_true",
                          help="locations only, without bodies")

    p_reindex = sub.add_parser("reindex", help="rebuild the full-text index")
    p_reindex.add_argument("--stats", action="store_true",
                           help="print index statistics instead of rebuilding")

    sub.add_parser("rebuild-registry",
                   help="regenerate the entity ledger from the event log")
    return parser


def _config(args) -> Config:
    return Config.load(args.home, llm_cmd=args.llm_cmd)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = _config(args)

    if args.command == "init":
        cfg.ensure_dirs()
        print(f"initialised {cfg.base_dir}")
        return 0

    if args.command == "ingest":
        cfg.ensure_dirs()
        return ingest.run(cfg, dry_run=args.dry_run, backfill=args.backfill)

    if args.command == "extract":
        cfg.ensure_dirs()
        return extract.run(cfg, dry_run=args.dry_run,
                           skip_entities=args.skip_entities,
                           skip_commitments=args.skip_commitments)

    if args.command == "review":
        return _review(cfg, args)

    if args.command == "search":
        return _search(cfg, args)

    if args.command == "reindex":
        if args.stats:
            print(json.dumps(search.index_stats(cfg), ensure_ascii=False, indent=2))
            return 0
        stats = search.build_index(cfg, verbose=not args.json)
        if args.json:
            print(json.dumps(stats, ensure_ascii=False))
        return 0

    if args.command == "rebuild-registry":
        ledger = registry.rebuild(cfg)
        count = sum(1 for _ in ledger.entities())
        print(f"rebuilt {count} entities from the event log into {cfg.registry_dir}")
        return 0

    return 2  # pragma: no cover - argparse rejects unknown commands first


def _review(cfg: Config, args) -> int:
    if args.review_command == "list":
        items = (review.all_proposals(cfg) if args.all
                 else review.pending(cfg, kind=args.kind))
        if args.all and args.kind:
            items = [p for p in items if p.get("kind") == args.kind]
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return 0
        if not items:
            print("no proposals awaiting review")
            return 0
        for proposal in items:
            state = "" if proposal["state"] == "queued" else f"  [{proposal['state']}]"
            print(review.describe(proposal) + state)
        return 0

    if args.review_command == "show":
        proposal = review.get(cfg, args.proposal_id)
        if args.json:
            print(json.dumps(proposal, ensure_ascii=False, indent=2))
            return 0
        print(review.describe(proposal))
        hint = review.source_hint(proposal, cfg.base_dir)
        if hint:
            print(f"  source: {hint}")
        for key in ("entity_type", "aliases", "note", "date_hint", "collides_with", "state"):
            if proposal.get(key):
                print(f"  {key}: {proposal[key]}")
        return 0

    try:
        if args.review_command == "approve":
            result = review.approve(cfg, args.proposal_id,
                                    merge_into=args.merge_into, note=args.note)
        else:
            result = review.reject(cfg, args.proposal_id, note=args.note)
    except review.ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    past = {"approve": "approved", "reject": "rejected"}[args.review_command]
    print(json.dumps(result, ensure_ascii=False) if args.json
          else f"{past} {args.proposal_id}")
    return 0


def _search(cfg: Config, args) -> int:
    try:
        results = search.search(
            cfg, args.query, terms=args.terms, top_k=args.top_k * 4 if args.hint else args.top_k,
            kinds=args.kinds, expand_aliases=not args.no_alias,
            min_ratio=args.min_ratio, min_coverage=args.min_coverage,
            budget_tokens=args.budget, recency_days=args.recency_days,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.hint:
        print(search.format_hints(cfg, results, limit=args.top_k))
    else:
        rendered = search.format_results(cfg, results, args.query)
        print(rendered if rendered else "(no related memory found)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
