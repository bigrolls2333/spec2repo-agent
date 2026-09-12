import io
import zipfile

import pytest

from paper2repo_agent.config import Settings
from paper2repo_agent.mineru import MineruClient
from paper2repo_agent.parser import parse_local_text_file


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text="", content=b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.content = content

    def json(self):
        return self._payload


class DummySession:
    trust_env = False
    last_headers = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def put(self, *args, **kwargs):
        DummySession.last_headers = kwargs.get("headers")
        return DummyResponse()


def _zip_with_markdown() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("full.md", "# Parsed\n\nContent")
    return buffer.getvalue()


def test_mineru_missing_config_raises():
    settings = Settings(
        openai_api_key=None,
        openai_base_url=None,
        model_name="x",
        llm_temperature=0,
        mineru_base_url=None,
        mineru_api_token=None,
        mineru_model_source=None,
        modelscope_offline=None,
        modelscope_cache=None,
    )

    with pytest.raises(ValueError):
        MineruClient(settings)


def test_mineru_placeholder_token_raises():
    settings = Settings(
        openai_api_key=None,
        openai_base_url=None,
        model_name="x",
        llm_temperature=0,
        mineru_base_url="https://mineru.example/api/v4",
        mineru_api_token="your-mineru-token",
        mineru_model_source=None,
        modelscope_offline=None,
        modelscope_cache=None,
    )

    with pytest.raises(ValueError):
        MineruClient(settings)


def test_mineru_accepts_existing_bearer_prefix():
    settings = Settings(
        openai_api_key=None,
        openai_base_url=None,
        model_name="x",
        llm_temperature=0,
        mineru_base_url="https://mineru.example/api/v4",
        mineru_api_token="Bearer abc",
        mineru_model_source=None,
        modelscope_offline=None,
        modelscope_cache=None,
    )

    client = MineruClient(settings)

    assert client.headers["Authorization"] == "Bearer abc"


def test_markdown_fallback_does_not_need_mineru(tmp_path):
    paper = tmp_path / "paper.md"
    paper.write_text("# Title\n\nBody", encoding="utf-8")

    parsed = parse_local_text_file(paper, "markdown", "paper_1")

    assert parsed.title == "paper"
    assert parsed.sections


def test_mineru_happy_path(monkeypatch, tmp_path):
    settings = Settings(
        openai_api_key=None,
        openai_base_url=None,
        model_name="x",
        llm_temperature=0,
        mineru_base_url="https://mineru.example/api/v4",
        mineru_api_token="secret-token",
        mineru_model_source=None,
        modelscope_offline=None,
        modelscope_cache=None,
    )
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF")

    def fake_post(*args, **kwargs):
        return DummyResponse(
            payload={
                "code": 0,
                "data": {"file_urls": ["https://upload.example"], "batch_id": "batch_1"},
            }
        )

    def fake_get(url, *args, **kwargs):
        if "extract-results" in url:
            return DummyResponse(
                payload={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {"state": "done", "full_zip_url": "https://download.example/out.zip"}
                        ]
                    },
                }
            )
        return DummyResponse(content=_zip_with_markdown())

    monkeypatch.setattr("paper2repo_agent.mineru.requests.post", fake_post)
    monkeypatch.setattr("paper2repo_agent.mineru.requests.get", fake_get)
    monkeypatch.setattr("paper2repo_agent.mineru.requests.Session", DummySession)

    result = MineruClient(settings).parse_pdf_to_markdown(pdf, tmp_path / "out")

    assert result.exists()
    assert "Parsed" in result.read_text(encoding="utf-8")
    assert DummySession.last_headers is None
