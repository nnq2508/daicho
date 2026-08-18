<!-- Japanese version: README.ja.md -->
# daicho

An append-only memory ledger for long-lived assistants.

Give it your conversation logs and notes. It records them as immutable
episodes, harvests entities and dated commitments with a language model of your
choosing, holds everything uncertain in a review queue until a human decides,
and answers questions over the whole corpus with a full-text index that works
for Japanese as well as English.

No dependencies. Python 3.10+ and the standard library: `json` for the log,
`sqlite3` (FTS5) for the index, `subprocess` for the model boundary.

```bash
pip install daicho
daicho init
daicho ingest
daicho --llm-cmd "your-model-cli --quiet" extract
daicho review list
daicho reindex
daicho search "when did we agree on the venue?"
```

Or as a library:

```python
from daicho import Config, ingest, extract, search

cfg = Config.load("/srv/memory").ensure_dirs()
ingest.run(cfg)                      # sessions -> episodes
extract.run(cfg)                     # episodes -> entities + commitments (staged)
search.build_index(cfg)
hits = search.search(cfg, "venue for the autumn cleanup")
```

Run `python examples/quickstart.py` to see the whole cycle on fictional data
with a mock model, without a network connection.

## Why this shape

Most assistant memory is a pile of files that several processes write
concurrently, plus a vector store nobody can audit. Four failures follow
predictably, and each rule below exists to remove one of them.

### 1. The event log is the source of truth

Everything is an append-only JSON line under `events/YYYY-MM.jsonl`. The entity
registry and the search index are **derived**: delete them and rebuild.

```bash
rm -rf registry/ index/
daicho rebuild-registry && daicho reindex
```

If a fact exists only in a derived store, a corrupted database becomes lost
memory. Provenance, rollback and recovery all come from the same property.

### 2. Every event names its writer

`type`, `at` and `writer` are mandatory; an event without them is rejected at
the API boundary rather than accepted as an unattributable record. When several
sessions, a nightly worker and a subagent all write into one store, "who
claimed this, and from which episode?" is the question you will need answered
later, and it cannot be reconstructed after the fact.

### 3. Cursors are recomputed, never stored

"Which turns have I already ingested?" is derived from the log itself on every
run (`events.session_cursors`). A cursor file is a second source of truth: when
it rots, idempotency rots with it, and the symptom is either silent data loss
or an infinite re-ingest loop. Episode identity is likewise content-derived
(`session id + turn range + content hash`), so rotating a log file into a
gzipped archive does not re-ingest it.

### 4. Extraction stops in staging

A language model reading conversation logs is a guesser, and the cost of a
wrong guess is asymmetric: a merged identity ("which of the two people with the
same family name is this?") silently poisons everything written afterwards.

Exactly one rule promotes an entity without a human:

> no collision with any existing name **and** seen in at least *N* distinct
> episodes (default 3, `auto_confirm_min_episodes`).

Everything else — a name overlapping an existing one, a low-confidence
extraction, a dated commitment with no matching reminder — becomes a proposal:

```
$ daicho review list
prop_1762…_01b776de  entity 山田 (person) -- collides with the existing entity '山田花子'
prop_1762…_3d07c83e  commitment renew the permit before September -- dated statement with no matching reminder

$ daicho review approve prop_1762…_01b776de       # a different person
$ daicho review approve prop_1762…_01b776de --merge-into person:yamada_hanako
$ daicho review reject  prop_1762…_3d07c83e --note "already on the calendar"
```

Approving as a separate entity records the pair in `confusable_with`, in both
directions, so the same near-miss is decided once and never re-litigated.

The queue itself is a projection of the log (`proposal_opened` /
`proposal_deferred` / `proposal_resolved`), so a decision is auditable and the
whole queue survives losing every derived file.

## Failure discipline

A memory worker that fails quietly is worse than one that stops, because the
system keeps looking alive while it stops learning.

* An LLM call is retried `llm_retries` times (default 2). A batch that still
  fails is left **unscanned** — no skip marker is written for work that was not
  done — and the next run retries it.
* After `max_consecutive_failures` failed batches (default 5) the worker calls
  the configured notification command and **exits non-zero**. Unbounded silent
  retrying is not resilience.
* `ingest` verifies on every run that no already-ingested session has
  disappeared, and that the monthly archive has no gaps. Either one is a
  non-zero exit, never a warning nobody reads.
* Proposals are capped per run (`max_proposals_per_run`, default 10 per
  stream). The overflow is deferred and drained at the start of the next run,
  so a noisy night neither buries the reviewer nor loses anything.

## Search

`daicho search` is intended to be the *only* read path into memory. Multiple
retrieval routes with slightly different logic diverge, and then answers depend
on which route happened to run.

**Bigrams, not trigrams.** FTS5 ships a `trigram` tokenizer that is commonly
recommended for languages without spaces. A query shorter than three characters
produces no trigram at all, so it structurally returns zero rows — and in
Japanese an enormous share of the words people actually search for (family
names, place names, everyday nouns) are exactly two characters. This library
therefore splits CJK runs into overlapping bigrams at index time and applies the
same transformation to the query, using the standard `unicode61` tokenizer.
ASCII words are kept whole and lower-cased. `tests/test_search.py` demonstrates
the contrast directly rather than asking you to take it on faith.

Other properties worth knowing:

* **It can return nothing.** Hits below `min_ratio` of the best hit are
  dropped. Padding results to `top_k` is how irrelevant text ends up quoted as
  evidence.
* **Aliases widen a query.** A two-character nickname alone drowns in common
  documents; the ledger adds the rare canonical spelling to the same `OR`
  clause and the ranking sorts it out. Explicit `terms` are trusted as given
  and never widened.
* **Curated notes outrank raw logs**, per-kind and configurable
  (`kind_weights`). Logs are numerous and would otherwise dominate.
* **Mid-session hits carry the session's last turn.** Snippet search
  structurally hides self-correction: a confident wrong statement matches, the
  correction twenty minutes later does not. The tail is attached so a reader
  can see the conclusion may have moved.
* **Results are wrapped in `<retrieved_memory>`** and labelled as reference
  material. Stored memory contains old model output and text copied from the
  web; when it is fed back to a model, none of it should be read as an
  instruction.

## Data formats

Sessions are a JSON array of turns:

```json
[{"role": "user", "content": "...", "timestamp": "2031-05-04T09:12:00+09:00"}]
```

Only `role` and `content` are required. Anything else — a vendor export, a
database, a chat backend's API — is handled by an adapter that writes this
shape into `sessions/`; nothing downstream knows the original format.

Notes are plain Markdown under `notes/`. Extra corpora are added through
`extra_sources` without touching the code.

## Layout

```
$DAICHO_HOME/
  events/YYYY-MM.jsonl      append-only log        <- source of truth
  sessions/*.json           conversation logs (+ archive/YYYY-MM/*.json.gz)
  notes/*.md                markdown notes
  registry/*.json           entity ledger          <- derived
  index/fts.db              full-text index        <- derived
  config.json               optional overrides
```

The home directory comes from `Config.load(path)`, `$DAICHO_HOME`, or
`~/.daicho`, in that order. Nothing is hardcoded to a machine or a user.

## Configuration

`config.json` in the home directory, or keyword arguments to `Config.load`:

| key | default | meaning |
|---|---|---|
| `llm_cmd` | `$DAICHO_LLM_CMD` | shell command: prompt on stdin, JSON on stdout |
| `notify_cmd` | `$DAICHO_NOTIFY_CMD` | called with the message on stdin when a run gives up |
| `auto_confirm_min_episodes` | 3 | distinct episodes required for automatic promotion |
| `max_proposals_per_run` | 10 | review-queue budget per stream; the rest is deferred |
| `max_consecutive_failures` | 5 | failed batches before notifying and exiting non-zero |
| `llm_retries` / `llm_timeout_sec` | 2 / 180 | per-call retry and timeout |
| `max_episodes_per_run` / `episodes_per_batch` | 60 / 10 | work size per run and per prompt |
| `reminders_file` | — | JSON file of reminders the host already scheduled |
| `extra_sources` | — | `[{"kind": "profile", "path": "people", "glob": "**/*.md"}]` |
| `kind_weights` | see `config.py` | relevance multiplier per source kind |
| `generic_aliases` | see `config.py` | words never used for alias expansion |

## Bringing your own model

The model boundary is one shell command: **prompt on stdin, JSON on stdout**.
Anything honouring that works, and no vendor SDK is imported anywhere.

```bash
daicho --llm-cmd "claude -p --model sonnet" extract
daicho --llm-cmd "llm -m gpt-4o-mini" extract
daicho --llm-cmd "ollama run qwen2.5" extract
daicho --llm-cmd "python my_wrapper.py" extract
export DAICHO_LLM_CMD="curl -s -XPOST … | jq -r .output"
```

Only the first JSON object in stdout is read, so wrappers that print progress
lines are fine. See `examples/mock_llm.py` for a dependency-free stub.

## Scheduling

`ingest` is cheap, deterministic and safe to run often (a few minutes apart).
`extract` costs model calls; nightly is a reasonable default. Keep them in
separate units: a failing extractor must never stop plain recording.

```cron
*/15 * * * *  daicho ingest              >> /var/log/daicho.log 2>&1
35 3    * * *  daicho extract && daicho reindex
```

## Tests

```bash
pip install -e ".[test]"
pytest
```

The suite runs entirely in temporary directories with a mock model; nothing
touches a real home directory, and no test needs a network.

## License

Apache-2.0.
