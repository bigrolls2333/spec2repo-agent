from __future__ import annotations

import shutil
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from paper2repo_agent.config import Settings
from paper2repo_agent.security import redact_text
from paper2repo_agent.utils import ensure_dir


class MineruClient:
    def __init__(
        self,
        settings: Settings,
        *,
        timeout_seconds: int = 600,
        poll_interval_seconds: int = 3,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        settings.require_mineru()
        self.base_url = (settings.mineru_base_url or "").rstrip("/")
        self.authorization = _as_bearer_header(settings.mineru_api_token or "")
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.progress = progress

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": self.authorization,
            "Content-Type": "application/json",
        }

    def parse_pdf_to_markdown(self, pdf_path: Path, output_dir: Path) -> Path:
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")
        if pdf_path.suffix.lower() != ".pdf":
            raise ValueError(f"Expected a PDF input, got: {pdf_path}")
        ensure_dir(output_dir)

        self._log("MinerU: requesting upload URL")
        signed_url, batch_id = self._get_upload_url(pdf_path)
        self._log("MinerU: uploading PDF to presigned OSS URL")
        self._upload_pdf(pdf_path, signed_url)
        self._log(f"MinerU: polling extraction batch {batch_id}")
        zip_url = self._poll_zip_url(batch_id)
        self._log("MinerU: downloading and extracting Markdown result")
        return self._download_and_extract(zip_url, output_dir, pdf_path.stem)

    def _get_upload_url(self, pdf_path: Path) -> tuple[str, str]:
        url = f"{self.base_url}/file-urls/batch"
        request_body = {"files": [{"name": pdf_path.name}], "model_version": "vlm"}
        response = requests.post(url, json=request_body, headers=self.headers, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(_format_mineru_error("upload-url request failed", response.text))
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                _format_mineru_error(
                    "upload-url response failed",
                    f"code={payload.get('code')}, msg={payload.get('msg')}",
                )
            )
        data = payload.get("data") or {}
        signed_url = (data.get("file_urls") or [None])[0]
        batch_id = data.get("batch_id")
        if not signed_url or not batch_id:
            raise RuntimeError("MinerU did not return upload URL or batch ID")
        return signed_url, batch_id

    def _upload_pdf(self, pdf_path: Path, signed_url: str) -> None:
        with requests.Session() as session:
            session.trust_env = False
            response = session.put(
                signed_url,
                data=pdf_path.read_bytes(),
                timeout=120,
            )
        if response.status_code not in {200, 201, 204}:
            raise RuntimeError(_format_mineru_error("PDF upload failed", response.text))

    def _poll_zip_url(self, batch_id: str) -> str:
        poll_url = f"{self.base_url}/extract-results/batch/{batch_id}"
        started = time.time()
        last_error: str | None = None
        while time.time() - started < self.timeout_seconds:
            try:
                response = requests.get(poll_url, headers=self.headers, timeout=10)
                if response.status_code >= 500:
                    last_error = response.text
                    time.sleep(self.poll_interval_seconds)
                    continue
                if response.status_code != 200:
                    raise RuntimeError(_format_mineru_error("polling failed", response.text))
                payload: dict[str, Any] = response.json()
                if payload.get("code") != 0:
                    raise RuntimeError(
                        _format_mineru_error(
                            "polling response failed",
                            f"code={payload.get('code')}, msg={payload.get('msg')}",
                        )
                    )
                results = payload.get("data", {}).get("extract_result") or []
                if not results:
                    time.sleep(self.poll_interval_seconds)
                    continue
                state = results[0].get("state")
                if state == "done":
                    zip_url = results[0].get("full_zip_url")
                    if not zip_url:
                        raise RuntimeError("MinerU finished without a ZIP URL")
                    self._log("MinerU: extraction finished")
                    return zip_url
                if state == "failed":
                    raise RuntimeError("MinerU extraction task failed")
                self._log(f"MinerU: current state={state}, waiting")
                time.sleep(self.poll_interval_seconds)
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(self.poll_interval_seconds)

        suffix = f": {redact_text(last_error)}" if last_error else ""
        raise TimeoutError(f"MinerU extraction timed out{suffix}")

    def _download_and_extract(self, zip_url: str, output_dir: Path, stem: str) -> Path:
        response = requests.get(zip_url, timeout=120)
        if response.status_code != 200:
            raise RuntimeError(redact_text(f"MinerU ZIP download failed: {response.text}"))

        zip_path = output_dir / f"{stem}_result.zip"
        zip_path.write_bytes(response.content)

        # Use a fixed-length short directory name for extraction so very long
        # paper names do not blow past Windows MAX_PATH on the nested UUID-named
        # files MinerU writes inside result_list.json. Only the markdown itself
        # (saved below to ``{stem}.md``) keeps the paper stem for display.
        extract_dir = output_dir / "_x"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        ensure_dir(extract_dir)

        with zipfile.ZipFile(zip_path, "r") as archive:
            archive.extractall(extract_dir)

        candidates = list(extract_dir.rglob("*.md"))
        if not candidates:
            raise RuntimeError("MinerU ZIP did not contain a Markdown file")

        preferred = next((path for path in candidates if path.stem == stem), None)
        preferred = preferred or next((path for path in candidates if path.name == "full.md"), None)
        preferred = preferred or candidates[0]

        final_path = output_dir / f"{stem}.md"
        if preferred.resolve() != final_path.resolve():
            markdown = preferred.read_text(encoding="utf-8", errors="replace")
            final_path.write_text(markdown, encoding="utf-8")
        self._log(f"MinerU: Markdown saved to {final_path}")
        return final_path

    def _log(self, message: str) -> None:
        if self.progress:
            self.progress(message)


def _as_bearer_header(token: str) -> str:
    normalized = token.strip().strip('"').strip("'")
    if normalized.lower().startswith("bearer "):
        return normalized
    return f"Bearer {normalized}"


def _format_mineru_error(action: str, detail: str) -> str:
    safe_detail = redact_text(detail)
    if "authenticate failed" in safe_detail.lower() or "a0202" in safe_detail.lower():
        return (
            f"MinerU {action}: authentication failed. Check MINERU_API_TOKEN in .env. "
            "Use a current token value; both bare tokens and 'Bearer ...' values are supported. "
            f"Server response: {safe_detail}"
        )
    if "signaturedoesnotmatch" in safe_detail.lower():
        return (
            f"MinerU {action}: OSS presigned upload signature mismatch. "
            "The upload request must not add extra signed headers such as Content-Type. "
            f"Server response: {safe_detail[:800]}"
        )
    return redact_text(f"MinerU {action}: {safe_detail}")
