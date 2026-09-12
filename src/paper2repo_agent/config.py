from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

from paper2repo_agent.security import redact_mapping


def _first_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip() != "":
            return value.strip().strip('"').strip("'")
    return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None
    openai_base_url: str | None
    model_name: str
    llm_temperature: float
    mineru_base_url: str | None
    mineru_api_token: str | None
    mineru_model_source: str | None
    modelscope_offline: str | None
    modelscope_cache: str | None
    evidence_backend: str = "sqlite"
    mysql_uri: str | None = None
    mysql_required: bool = False

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> "Settings":
        if env_file:
            load_dotenv(dotenv_path=env_file, override=False)
        else:
            load_dotenv(override=False)

        return cls(
            openai_api_key=_first_env("OPENAI_API_KEY"),
            openai_base_url=_first_env("OPENAI_BASE_URL", "OPENAI_API_BASE"),
            model_name=_first_env("MODEL_NAME", "LLM_DEFAULT_MODEL", default="gpt-4.1-mini")
            or "gpt-4.1-mini",
            llm_temperature=_float_env("LLM_DEFAULT_TEMPERATURE", 0.0),
            mineru_base_url=_first_env("MINERU_BASE_URL"),
            mineru_api_token=_first_env("MINERU_API_TOKEN"),
            mineru_model_source=_first_env("MINERU_MODEL_SOURCE"),
            modelscope_offline=_first_env("MODELSCOPE_OFFLINE"),
            modelscope_cache=_first_env("MODELSCOPE_CACHE"),
            evidence_backend=(
                _first_env("PAPER2REPO_EVIDENCE_BACKEND", default="sqlite") or "sqlite"
            ).lower(),
            mysql_uri=_first_env("PAPER2REPO_MYSQL_URI", "MYSQL_URI", "DATABASE_URL"),
            mysql_required=_bool_env("PAPER2REPO_MYSQL_REQUIRED", False),
        )

    def require_llm(self) -> None:
        missing = []
        if not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if not self.openai_base_url:
            missing.append("OPENAI_BASE_URL or OPENAI_API_BASE")
        if missing:
            raise ValueError(f"Missing LLM configuration: {', '.join(missing)}")

    def require_mineru(self) -> None:
        missing = []
        if not self.mineru_base_url:
            missing.append("MINERU_BASE_URL")
        if not self.mineru_api_token or _looks_like_placeholder(self.mineru_api_token):
            missing.append("MINERU_API_TOKEN")
        if missing:
            raise ValueError(f"Missing MinerU configuration: {', '.join(missing)}")

    def safe_dict(self) -> dict[str, object]:
        return redact_mapping(
            {
                "openai_api_key": self.openai_api_key,
                "openai_base_url": self.openai_base_url,
                "model_name": self.model_name,
                "llm_temperature": self.llm_temperature,
                "mineru_base_url": self.mineru_base_url,
                "mineru_api_token": self.mineru_api_token,
                "mineru_model_source": self.mineru_model_source,
                "modelscope_offline": self.modelscope_offline,
                "modelscope_cache": self.modelscope_cache,
                "evidence_backend": self.evidence_backend,
                "mysql_uri": _redact_database_uri(self.mysql_uri),
                "mysql_required": self.mysql_required,
            }
        )


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {
        "your-mineru-token",
        "your-token",
        "your-api-token",
        "your-api-key",
        "token",
        "none",
        "null",
    }


def _redact_database_uri(value: str | None) -> str | None:
    if not value:
        return value
    parts = urlsplit(value)
    userinfo, at, host = parts.netloc.rpartition("@")
    if not at:
        return value
    username, colon, _password = userinfo.partition(":")
    redacted_userinfo = f"{username}:***" if colon else username
    return urlunsplit(
        (parts.scheme, f"{redacted_userinfo}@{host}", parts.path, parts.query, parts.fragment)
    )
