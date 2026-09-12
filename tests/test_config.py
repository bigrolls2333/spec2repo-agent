from paper2repo_agent.config import Settings
from paper2repo_agent.security import redact_text


def test_model_name_precedence(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "preferred-model")
    monkeypatch.setenv("LLM_DEFAULT_MODEL", "fallback-model")

    settings = Settings.load()

    assert settings.model_name == "preferred-model"


def test_base_url_precedence(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://preferred.example/v1")
    monkeypatch.setenv("OPENAI_API_BASE", "https://fallback.example/v1")

    settings = Settings.load()

    assert settings.openai_base_url == "https://preferred.example/v1"


def test_mysql_settings_and_safe_dict_redact_password(monkeypatch):
    monkeypatch.setenv("PAPER2REPO_EVIDENCE_BACKEND", "mysql")
    monkeypatch.setenv(
        "PAPER2REPO_MYSQL_URI",
        "mysql+pymysql://paper2repo:example-password@127.0.0.1:3306/paper2repo?charset=utf8mb4",
    )
    monkeypatch.setenv("PAPER2REPO_MYSQL_REQUIRED", "true")

    settings = Settings.load()
    safe = settings.safe_dict()

    assert settings.evidence_backend == "mysql"
    assert settings.mysql_required is True
    assert settings.mysql_uri is not None and "example-password" in settings.mysql_uri
    assert (
        safe["mysql_uri"]
        == "mysql+pymysql://paper2repo:***@127.0.0.1:3306/paper2repo?charset=utf8mb4"
    )
    assert "example-password" not in str(safe)


def test_redact_text_masks_secret_shapes():
    fake_key = "sk-" + "testsecret123456"
    key_name = "OPENAI_" + "API_KEY"
    redacted = redact_text(f"{key_name}={fake_key} Authorization: Bearer abcdef")

    assert fake_key not in redacted
    assert "Bearer abcdef" not in redacted
