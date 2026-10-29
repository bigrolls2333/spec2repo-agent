"""Explicit configuration: no built-in credentials or silent database fallback."""

import json
import os
import re
from dataclasses import dataclass, field

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    api_keys: dict[str, str] = field(repr=False)

    def __post_init__(self):
        try:
            url = make_url(self.database_url)
        except (ArgumentError, ValueError, TypeError):
            raise ValueError("Invalid SPEC2REPO_DATABASE_URL") from None
        if url.drivername != "mysql+pymysql" or not url.database:
            raise ValueError("SPEC2REPO_DATABASE_URL must use mysql+pymysql and name a database")
        if not isinstance(self.api_keys, dict) or not self.api_keys:
            raise ValueError("Configure at least one API identity")
        for owner, key in self.api_keys.items():
            if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", owner):
                raise ValueError("Invalid API identity name")
            if not isinstance(key, str) or len(key) < 24 or not key.isascii() or key.isspace():
                raise ValueError(
                    "API keys must be distinct ASCII strings of at least 24 characters"
                )
        if len(set(self.api_keys.values())) != len(self.api_keys):
            raise ValueError("API keys must be distinct across identities")

    @classmethod
    def from_env(cls):
        try:
            database_url = os.environ["SPEC2REPO_DATABASE_URL"]
            api_keys = json.loads(os.environ["SPEC2REPO_API_KEYS"])
        except (KeyError, json.JSONDecodeError):
            raise ValueError(
                "Set SPEC2REPO_DATABASE_URL and SPEC2REPO_API_KEYS (JSON object)"
            ) from None
        return cls(database_url, api_keys)
