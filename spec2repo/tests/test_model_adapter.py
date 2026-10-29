import io
import json
import urllib.error

import pytest

from spec2repo.harness.model import ChatModel, ModelError


class Response(io.BytesIO):
    def read1(self, size=-1):
        return self.read(size)


def test_output_limit_is_classified_from_provider_finish_reason(monkeypatch):
    class Opener:
        def open(self, *_args, **_kwargs):
            return Response(json.dumps({"choices": [{"finish_reason": "length",
                "message": {"role": "assistant", "tool_calls": []}}]}).encode())
    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    with pytest.raises(ModelError) as exc:
        ChatModel("https://model.example/v1", "key", "model").complete([], [])
    assert exc.value.category == "output_limit"


def test_chat_adapter_sends_protocol_preserves_reasoning_and_reports_unknown_usage(monkeypatch):
    seen = {}

    class Opener:
        def open(self, request, timeout):
            seen["body"] = json.loads(request.data)
            seen["url"], seen["timeout"] = request.full_url, timeout
            seen["auth"] = request.headers["Authorization"]
            return Response(
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "reasoning_content": "provider reasoning",
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "list_files",
                                                "arguments": "{}",
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                ).encode()
            )

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    model = ChatModel("https://model.example/v1", "synthetic-key", "configured-model")
    message, usage = model.complete([{"role": "user", "content": "synthetic spec"}], [], timeout=17)
    assert usage is None
    assert message["reasoning_content"] == "provider reasoning"
    assert seen["url"] == "https://model.example/v1/chat/completions"
    assert seen["body"]["parallel_tool_calls"] is True
    assert seen["body"]["model"] == "configured-model"
    assert seen["auth"] == "Bearer synthetic-key" and seen["timeout"] == 17


@pytest.mark.parametrize(
    "response",
    [
        {"choices": []},
        {"choices": ["invalid"]},
        {"choices": [{"message": "not an object"}]},
        {"choices": [{"message": {"role": "assistant", "tool_calls": [{}, {}]}}]},
    ],
)
def test_invalid_provider_responses_fail_closed(monkeypatch, response):
    class Opener:
        def open(self, *_args, **_kwargs):
            return Response(json.dumps(response).encode())

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    with pytest.raises(ModelError):
        ChatModel("https://model.example/v1", "key", "model").complete([], [])


def test_transport_error_never_exposes_credentials_or_provider_body(monkeypatch):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.URLError("Authorization: Bearer synthetic-secret")

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    with pytest.raises(ModelError) as error:
        ChatModel("https://model.example/v1", "synthetic-secret", "model").complete([], [])
    assert "synthetic-secret" not in str(error.value)


def test_redirect_handler_refuses_forwarding_credentials(monkeypatch):
    seen = {}

    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.URLError("redirect")

    def build(handler):
        seen["handler"] = handler()
        return Opener()

    monkeypatch.setattr("urllib.request.build_opener", build)
    with pytest.raises(ModelError):
        ChatModel("https://model.example/v1", "key", "model").complete([], [])
    assert seen["handler"].redirect_request(None, None, 302, None, None, "https://other") is None


def test_opencode_uses_own_identity_and_stable_run_session(monkeypatch):
    seen = []

    class Opener:
        def open(self, request, **_kwargs):
            seen.append(dict(request.header_items()))
            return Response(b'{"choices":[{"message":{"role":"assistant","content":"test"}}]}')

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    model = ChatModel("https://opencode.ai/inference/go/openai/v1", "synthetic-key", "model")
    for _ in range(2):
        model.complete([], [], session_id="synthetic-run")
    assert all(h["User-agent"] == "spec2repo/0.5" for h in seen)
    assert all(h["X-opencode-session"] == "synthetic-run" for h in seen)


def test_http_status_is_observable_without_exposing_provider_body(monkeypatch):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://model.example", 401, "synthetic-secret", {}, io.BytesIO(b"private body")
            )

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    with pytest.raises(ModelError, match="401") as error:
        ChatModel("https://model.example/v1", "synthetic-key", "model").complete([], [])
    assert "synthetic-secret" not in str(error.value) and "private body" not in str(error.value)
