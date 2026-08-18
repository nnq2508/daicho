"""Configuration and on-disk layout.

Everything the library writes lives under a single ``base_dir`` (the "home").
Nothing is hardcoded to a particular machine or user: the home is taken from
(in order) an explicit argument, the ``DAICHO_HOME`` environment variable,
or ``~/.daicho``.

The tool name appears exactly once here (:data:`APP_NAME`); the environment
variable names, the default home directory and the default writer identifiers
are all derived from it, so renaming the project is a one-line change (plus the
package directory and the console-script entry in ``pyproject.toml``).

Layout under ``base_dir``::

    events/YYYY-MM.jsonl     append-only event log  <- the source of truth
    sessions/*.json          raw conversation logs (adapters may write here)
    sessions/archive/YYYY-MM/*.json.gz
    notes/*.md               free-form markdown notes (commitment harvesting)
    registry/*.json          entity ledger           <- derived, rebuildable
    index/fts.db             full-text index         <- derived, rebuildable
    config.json              optional overrides for the fields below
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

APP_NAME = "daicho"

ENV_HOME = f"{APP_NAME.upper()}_HOME"
ENV_LLM_CMD = f"{APP_NAME.upper()}_LLM_CMD"
ENV_NOTIFY_CMD = f"{APP_NAME.upper()}_NOTIFY_CMD"

CONFIG_FILENAME = "config.json"

#: Relevance multipliers per source kind. Curated notes carry more information
#: per character than raw conversation logs, which are numerous and would
#: otherwise dominate the ranking.
DEFAULT_KIND_WEIGHTS = {
    "note": 1.25,
    "profile": 1.20,
    "state": 1.15,
    "session": 1.00,
}

#: Words that describe a relationship or a role rather than a specific person.
#: They are never used as a key for alias expansion, and never expanded into,
#: because they match every person in the ledger.
DEFAULT_GENERIC_ALIASES = [
    "wife", "husband", "spouse", "mother", "father", "parent", "daughter",
    "son", "boss", "manager", "colleague", "friend", "teacher", "doctor",
    "客", "妻", "夫", "母", "父", "母親", "父親", "娘", "息子",
    "上司", "部下", "同僚", "友人", "先生", "先輩", "後輩",
]


@dataclass(frozen=True)
class SourceSpec:
    """One indexable corpus: a kind label, a root directory and a glob."""

    kind: str
    path: Path
    glob: str

    @classmethod
    def from_obj(cls, obj: dict, base_dir: Path) -> "SourceSpec":
        path = Path(obj["path"])
        if not path.is_absolute():
            path = base_dir / path
        return cls(kind=obj.get("kind", "note"), path=path, glob=obj.get("glob", "**/*.md"))


@dataclass
class Config:
    """Runtime configuration. Construct with :meth:`load`."""

    base_dir: Path

    # --- LLM plumbing ------------------------------------------------------
    #: Shell command that reads a prompt on stdin and writes JSON on stdout.
    llm_cmd: str | None = None
    #: Shell command invoked when the worker gives up (message on stdin).
    #: When unset, the message only goes to stderr.
    notify_cmd: str | None = None
    llm_timeout_sec: int = 180
    #: Retries *after* the first attempt, so the default means 3 attempts.
    llm_retries: int = 2
    max_consecutive_failures: int = 5

    # --- extraction policy -------------------------------------------------
    #: Distinct episodes an entity must appear in before it can be confirmed
    #: without a human. Collisions bypass this and always go to review.
    auto_confirm_min_episodes: int = 3
    #: Upper bound on proposals pushed into the review queue per run, per
    #: stream (entities / commitments). The overflow is deferred and drained
    #: at the start of the next run.
    max_proposals_per_run: int = 10
    max_episodes_per_run: int = 60
    episodes_per_batch: int = 10
    turn_chars_cap: int = 1200

    # --- corpora -----------------------------------------------------------
    #: Optional JSON file listing reminders the host application already
    #: scheduled. Harvested commitments matching one of these are dropped.
    reminders_file: Path | None = None
    extra_sources: list[SourceSpec] = field(default_factory=list)
    kind_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_KIND_WEIGHTS))
    generic_aliases: list[str] = field(default_factory=lambda: list(DEFAULT_GENERIC_ALIASES))

    # ------------------------------------------------------------------ dirs
    @property
    def events_dir(self) -> Path:
        return self.base_dir / "events"

    @property
    def sessions_dir(self) -> Path:
        return self.base_dir / "sessions"

    @property
    def notes_dir(self) -> Path:
        return self.base_dir / "notes"

    @property
    def registry_dir(self) -> Path:
        return self.base_dir / "registry"

    @property
    def index_path(self) -> Path:
        return self.base_dir / "index" / "fts.db"

    def ensure_dirs(self) -> "Config":
        for path in (self.events_dir, self.sessions_dir, self.notes_dir,
                     self.registry_dir, self.index_path.parent):
            path.mkdir(parents=True, exist_ok=True)
        return self

    # ---------------------------------------------------------------- loading
    @staticmethod
    def default_base_dir() -> Path:
        env = os.environ.get(ENV_HOME)
        if env:
            return Path(env).expanduser()
        return Path.home() / f".{APP_NAME}"

    @classmethod
    def load(cls, base_dir: Path | str | None = None, **overrides) -> "Config":
        """Build a config from ``base_dir`` / env vars / ``config.json`` / kwargs.

        Later sources win: file overrides defaults, keyword arguments override
        the file. Unknown keys in ``config.json`` are ignored so that a config
        written by a newer version does not break an older one.
        """
        base = Path(base_dir).expanduser() if base_dir else cls.default_base_dir()
        base = base.resolve()
        cfg = cls(base_dir=base)

        file_data: dict = {}
        config_file = base / CONFIG_FILENAME
        if config_file.exists():
            try:
                file_data = json.loads(config_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"unreadable config file: {config_file} ({exc})") from exc

        known = {f.name for f in fields(cls)} - {"base_dir"}
        for key, value in list(file_data.items()) + list(overrides.items()):
            if key not in known or value is None:
                continue
            if key in ("reminders_file",):
                value = Path(value)
                if not value.is_absolute():
                    value = base / value
            elif key == "extra_sources":
                value = [s if isinstance(s, SourceSpec) else SourceSpec.from_obj(s, base)
                         for s in value]
            setattr(cfg, key, value)

        if cfg.llm_cmd is None:
            cfg.llm_cmd = os.environ.get(ENV_LLM_CMD)
        if cfg.notify_cmd is None:
            cfg.notify_cmd = os.environ.get(ENV_NOTIFY_CMD)
        return cfg

    # ---------------------------------------------------------------- writers
    def writer(self, role: str) -> str:
        """Stable writer identifier for an internal component.

        Every event carries the identity of whoever wrote it; deriving the
        string from :data:`APP_NAME` keeps the rename cost at one line.
        """
        return f"{APP_NAME}.{role}"
