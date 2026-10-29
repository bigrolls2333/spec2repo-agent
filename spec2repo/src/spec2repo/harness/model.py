"""Small OpenAI-compatible Chat Completions adapter, without implicit retries."""

import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import uuid4


class ModelError(RuntimeError):
    def __init__(self, message, *, category="unknown"):
        super().__init__(message)
        self.category = category


class ChatModel:
    MAX_OUTPUT = 4096
    MAX_RESPONSE = 1024 * 1024

    def __init__(self, base_url, api_key, model):
        url = urlsplit(base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.query or url.fragment:
            raise ValueError("Model endpoint must be a configured HTTPS URL")
        if not api_key or not model:
            raise ValueError("Model credentials and name are required")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.key, self.model = api_key, model
        self.provider_host, self.session_id = url.hostname, uuid4().hex

    @classmethod
    def from_env(cls):
        return cls(
            os.environ["SPEC2REPO_MODEL_BASE_URL"],
            os.environ["SPEC2REPO_MODEL_API_KEY"],
            os.environ["SPEC2REPO_MODEL"],
        )

    def complete(self, messages, tools, *, timeout=60, session_id=None):
        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "max_tokens": self.MAX_OUTPUT,
        }
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "User-Agent": "spec2repo/0.5",
        }
        if self.provider_host == "opencode.ai":
            headers["x-opencode-session"] = session_id or self.session_id
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
        deadline = time.monotonic() + timeout
        try:
            # Refuse redirects so credentials cannot be forwarded to another endpoint.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *_args, **_kwargs):
                    return None

            opener = urllib.request.build_opener(NoRedirect)
            with opener.open(request, timeout=timeout) as response:
                data = bytearray()
                while True:
                    chunk = response.read1(65536)
                    if time.monotonic() > deadline:
                        raise ModelError("Model deadline exceeded", category="deadline")
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > self.MAX_RESPONSE:
                        raise ModelError("Model response too large", category="response_size")
            payload = json.loads(data)
            choice = payload["choices"][0]
            if not isinstance(choice, dict):
                raise ModelError("Invalid model choice", category="response_protocol")
            if choice.get("finish_reason") == "length":
                raise ModelError("Model output limit reached", category="output_limit")
            message = payload["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ModelError("Invalid model message", category="response_protocol")
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > 8:
                raise ModelError("At most eight tool calls per turn are allowed", category="tool_envelope")
            ids = set()
            for call in calls:
                if (not isinstance(call, dict) or not isinstance(call.get("id"), str)
                        or not call["id"] or call["id"] in ids
                        or not isinstance(call.get("function"), dict)
                        or not isinstance(call["function"].get("name"), str)
                        or not isinstance(call["function"].get("arguments"), str)):
                    raise ModelError("Invalid tool call envelope", category="tool_envelope")
                ids.add(call["id"])
            # Preserve provider reasoning_content when required for tool continuation.
            clean = {
                k: message[k]
                for k in ["role", "content", "tool_calls", "reasoning_content"]
                if k in message
            }
            if clean.get("role") != "assistant":
                raise ModelError("Invalid assistant response", category="response_protocol")
            usage = payload.get("usage")
            if isinstance(usage, dict):
                usage = {
                    k: v
                    for k, v in usage.items()
                    if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                    and isinstance(v, int)
                    and not isinstance(v, bool)
                    and v >= 0
                }
            else:
                usage = None
            return clean, usage or None
        except urllib.error.HTTPError as exc:
            raise ModelError(f"Model HTTP status {exc.code}", category="http_status") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ModelError("Model transport error", category="transport") from None
        except (
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ):
            raise ModelError("Model protocol error", category="response_protocol") from None
