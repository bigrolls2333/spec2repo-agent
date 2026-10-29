import pytest

from spec2repo.config import Settings


def test_requires_explicit_configuration(monkeypatch):
    monkeypatch.delenv("SPEC2REPO_DATABASE_URL", raising=False)
    monkeypatch.delenv("SPEC2REPO_API_KEYS", raising=False)
    with pytest.raises(ValueError, match="Set SPEC2REPO"):
        Settings.from_env()


def test_secrets_not_in_repr():
    settings = Settings("mysql+pymysql://user:secret@localhost/db", {"alice": "a" * 32})
    assert "secret" not in repr(settings)
    assert "a" * 32 not in repr(settings)


@pytest.mark.parametrize("keys", [{}, {"a": "short"}, {"a": "x" * 24, "b": "x" * 24}])
def test_rejects_bad_identity_configuration(keys):
    with pytest.raises(ValueError):
        Settings("mysql+pymysql://user@localhost/db", keys)


def test_no_silent_sqlite_production_fallback():
    with pytest.raises(ValueError, match="mysql"):
        Settings("sqlite:///local.db", {"alice": "a" * 32})
