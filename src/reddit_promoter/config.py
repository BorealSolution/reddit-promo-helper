"""Loading of the project paths, .env secrets, and per-app app.yaml files."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPS_DIR = PROJECT_ROOT / "apps"
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "promoter.db"
BACKUP_DIR = DATA_DIR / "backups"
BACKUPS_TO_KEEP = 10

VALID_POOLS = ("weekly", "lifetime")
VALID_STORES = ("play_store", "app_store")


class ConfigError(Exception):
    """Raised when an app.yaml or the environment is malformed."""


@dataclass(frozen=True)
class CodeFile:
    path: Path
    pool: str
    priority: int
    column: str


@dataclass(frozen=True)
class Template:
    kind: str
    body: str
    subject: str | None = None

    def render(self, **values: str) -> tuple[str | None, str]:
        """Return (subject, body) with placeholders filled in.

        Unknown placeholders raise rather than silently producing a message
        with a literal `{code}` in it.
        """
        try:
            subject = self.subject.format(**values) if self.subject else None
            body = self.body.format(**values)
        except KeyError as exc:
            raise ConfigError(f"template references unknown placeholder {exc}") from exc
        return subject, body.strip()


@dataclass(frozen=True)
class AppConfig:
    app_id: str
    name: str
    store: str
    subreddits: list[str]
    code_files: list[CodeFile]
    low_stock_threshold: dict[str, int]
    templates: dict[str, Template]
    config_path: Path
    app_dir: Path
    gemini_model: str = "gemini-2.5-flash"

    def template(self, key: str) -> Template:
        if key not in self.templates:
            raise ConfigError(f"app '{self.app_id}' has no template '{key}'")
        return self.templates[key]

    def threshold(self, pool: str) -> int:
        return int(self.low_stock_threshold.get(pool, 0))


def _require(data: dict, key: str, path: Path):
    if key not in data:
        raise ConfigError(f"{path}: missing required key '{key}'")
    return data[key]


def load_app_config(app_id: str, apps_dir: Path | None = None) -> AppConfig:
    """Read apps/<app_id>/app.yaml into an AppConfig."""
    apps_dir = apps_dir or APPS_DIR
    app_dir = apps_dir / app_id
    config_path = app_dir / "app.yaml"
    if not config_path.exists():
        raise ConfigError(f"no app.yaml found at {config_path}")

    with config_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    declared_id = _require(data, "app_id", config_path)
    if declared_id != app_id:
        raise ConfigError(
            f"{config_path}: app_id '{declared_id}' does not match folder name '{app_id}'"
        )

    store = _require(data, "store", config_path)
    if store not in VALID_STORES:
        raise ConfigError(f"{config_path}: store must be one of {VALID_STORES}, got '{store}'")

    code_files = []
    for entry in _require(data, "code_files", config_path):
        pool = entry.get("pool")
        if pool not in VALID_POOLS:
            raise ConfigError(f"{config_path}: pool must be one of {VALID_POOLS}, got '{pool}'")
        code_files.append(
            CodeFile(
                path=(app_dir / entry["path"]).resolve(),
                pool=pool,
                priority=int(entry.get("priority", 1)),
                column=entry.get("column", "Promotion code"),
            )
        )

    templates = {}
    for key, entry in (data.get("templates") or {}).items():
        templates[key] = Template(
            kind=entry.get("kind", "comment_reply"),
            body=entry["body"],
            subject=entry.get("subject"),
        )

    return AppConfig(
        app_id=app_id,
        name=_require(data, "name", config_path),
        store=store,
        subreddits=list(data.get("subreddits") or []),
        code_files=code_files,
        low_stock_threshold=dict(data.get("low_stock_threshold") or {}),
        templates=templates,
        config_path=config_path,
        app_dir=app_dir,
        gemini_model=data.get("gemini_model", "gemini-2.5-flash"),
    )


def discover_app_ids(apps_dir: Path | None = None) -> list[str]:
    """Every folder under apps/ that contains an app.yaml."""
    apps_dir = apps_dir or APPS_DIR
    if not apps_dir.exists():
        return []
    return sorted(p.name for p in apps_dir.iterdir() if (p / "app.yaml").exists())


@dataclass(frozen=True)
class Secrets:
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_username: str = ""
    reddit_password: str = ""
    reddit_user_agent: str = ""
    gemini_api_key: str = ""

    def missing_reddit(self) -> list[str]:
        names = {
            "REDDIT_CLIENT_ID": self.reddit_client_id,
            "REDDIT_CLIENT_SECRET": self.reddit_client_secret,
            "REDDIT_USERNAME": self.reddit_username,
            "REDDIT_PASSWORD": self.reddit_password,
            "REDDIT_USER_AGENT": self.reddit_user_agent,
        }
        return [k for k, v in names.items() if not v]


def load_secrets() -> Secrets:
    load_dotenv(PROJECT_ROOT / ".env")
    return Secrets(
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
        reddit_username=os.getenv("REDDIT_USERNAME", ""),
        reddit_password=os.getenv("REDDIT_PASSWORD", ""),
        reddit_user_agent=os.getenv("REDDIT_USER_AGENT", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
    )
