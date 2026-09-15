from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse


APP_NAME = "MO54CallsAgent"


@dataclass(frozen=True)
class AgentPaths:
    root: Path
    audio: Path
    staging: Path
    transcripts: Path
    models: Path
    browser_profile: Path
    database: Path
    config: Path
    log: Path
    lock: Path
    manager_embedding: Path

    @classmethod
    def default(cls) -> "AgentPaths":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
        return cls(
            root=root,
            audio=root / "audio",
            staging=root / "staging",
            transcripts=root / "transcripts",
            models=root / "models",
            browser_profile=root / "edge-profile",
            database=root / "agent.sqlite3",
            config=root / "config.json",
            log=root / "agent.log",
            lock=root / "agent.lock",
            manager_embedding=root / "manager_embedding.dpapi",
        )

    def ensure(self) -> None:
        for path in (self.root, self.audio, self.staging, self.transcripts, self.models, self.browser_profile):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Selectors:
    call_rows: str = "[data-call-session-id]"
    session_id_attribute: str = "data-call-session-id"
    session_id_selector: str = ""
    started_at_attribute: str = "data-call-started-at"
    duration_attribute: str = "data-call-duration-sec"
    started_at_selector: str = ""
    duration_selector: str = ""
    download_button: str = "[data-action='download-recording']"


@dataclass(frozen=True)
class AgentConfig:
    crm_url: str = ""
    novofon_calls_url: str = ""
    timezone: str = "Asia/Novosibirsk"
    daily_download_limit: int = 50
    retention_days: int = 21
    minimum_free_gib: int = 10
    minimum_free_percent: int = 15
    manager_match_threshold: float = 0.72
    selectors: Selectors = field(default_factory=Selectors)

    def validate(self) -> None:
        if self.crm_url:
            parsed = urlparse(self.crm_url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
                raise ValueError("crm_url must be an HTTPS origin without a path")
        if self.novofon_calls_url:
            parsed = urlparse(self.novofon_calls_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("novofon_calls_url must be an HTTPS URL")
        if not 1 <= self.daily_download_limit <= 500:
            raise ValueError("daily_download_limit must be between 1 and 500")
        if not 1 <= self.retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        if not 0 < self.minimum_free_gib <= 10_000 or not 0 < self.minimum_free_percent < 100:
            raise ValueError("disk thresholds are invalid")
        if not 0.0 < self.manager_match_threshold <= 1.0:
            raise ValueError("manager_match_threshold must be within (0, 1]")
        required = self.selectors
        if not required.call_rows or not (required.session_id_attribute or required.session_id_selector) or not required.download_button:
            raise ValueError("Novofon row, session-id and download selectors are required")
        if not required.started_at_attribute and not required.started_at_selector:
            raise ValueError("Novofon started-at attribute or selector is required")

    @classmethod
    def load(cls, paths: AgentPaths) -> "AgentConfig":
        paths.ensure()
        if not paths.config.exists():
            config = cls()
            paths.config.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return config
        # Windows PowerShell 5 writes a BOM for Set-Content -Encoding utf8.
        # Accept it so setup.ps1-created configuration works on home PCs.
        raw = json.loads(paths.config.read_text(encoding="utf-8-sig"))
        selectors = Selectors(**raw.pop("selectors", {}))
        config = cls(selectors=selectors, **raw)
        config.validate()
        return config

    def save(self, paths: AgentPaths) -> None:
        self.validate()
        paths.ensure()
        temporary = paths.config.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(paths.config)

    def to_dict(self) -> dict:
        return asdict(self)
