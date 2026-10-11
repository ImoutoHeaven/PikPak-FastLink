from __future__ import annotations

import json
import socket
import ssl
from dataclasses import dataclass
from enum import Enum
from http.client import HTTPException
from typing import Callable
from urllib.error import URLError
from urllib.parse import urlencode

from pikpak_fastlink.auth import AuthConfig, OFFICIAL_HOST
from pikpak_fastlink.models import SourceRecord, normalize_gcid, parse_size


class DecisionKind(Enum):
    RETRYABLE = "retryable"
    CREDENTIAL_FATAL = "credential_fatal"
    FAILED = "failed"
    NOT_REUSABLE = "not_reusable"
    COMPLETED = "completed"
    DIRECTORY_CREATED = "directory_created"


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    error: str | None = None
    file_id: str | None = None
    payload: dict | None = None


@dataclass(frozen=True)
class HttpResult:
    status: int
    payload: object


Transport = Callable[[str, str, dict[str, str], bytes, float], HttpResult]


def _default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: float,
) -> HttpResult:
    from curl_cffi import requests

    try:
        response = requests.request(
            method, url, headers=headers, data=body or None, timeout=timeout,
            impersonate="chrome", allow_redirects=False,
        )
    except requests.exceptions.RequestException:
        raise ConnectionError("request transport failed") from None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return HttpResult(status=int(response.status_code), payload=payload)


def _transport_failure(exc: Exception) -> Decision:
    if isinstance(exc, (TimeoutError, socket.timeout, ssl.SSLError, URLError,
                        ConnectionError, OSError, HTTPException)):
        return Decision(DecisionKind.RETRYABLE, error="request transport failed")
    return Decision(DecisionKind.FAILED, error="request transport failed")


def _http_failure(result: HttpResult) -> Decision | None:
    payload = result.payload if isinstance(result.payload, dict) else {}
    error = payload.get("error")
    if result.status in {401, 403} or (isinstance(error, str) and error in {
        "captcha_invalid", "unauthenticated", "invalid_token",
    }):
        return Decision(DecisionKind.CREDENTIAL_FATAL, error="credentials expired or rejected")
    if result.status == 429 or 500 <= result.status <= 599:
        return Decision(DecisionKind.RETRYABLE, error=f"HTTP {result.status}")
    if not 200 <= result.status < 300:
        return Decision(DecisionKind.FAILED, error=f"HTTP {result.status}")
    if not isinstance(result.payload, dict):
        return Decision(DecisionKind.RETRYABLE, error="invalid JSON response")
    if "error" in payload:
        return Decision(DecisionKind.FAILED, error="API request rejected")
    return None


def _file_id(file: dict) -> str | None:
    value = file.get("id")
    return value if isinstance(value, str) and value.strip() else None


def _file_payload(payload: dict, file: dict) -> dict:
    # Resumable responses may contain OSS credentials; never pass them to state/errors.
    return {
        "upload_type": payload.get("upload_type"),
        "file": {key: file[key] for key in ("id", "name", "kind", "size", "hash", "phase", "parent_id") if key in file},
    }


class PikPakClient:
    def __init__(
        self, *, authorization: str, device_id: str, captcha_token: str,
        host: str = OFFICIAL_HOST, timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        config = AuthConfig(host, authorization, device_id, captcha_token)
        self.host = config.host
        self.timeout = timeout
        self._transport = transport or _default_transport
        self._headers = {
            "Authorization": config.authorization,
            "X-Device-Id": config.device_id,
            "X-Captcha-Token": config.captcha_token,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload: dict | None = None) -> HttpResult | Decision:
        try:
            result = self._transport(
                method, f"{self.host}{path}", dict(self._headers),
                b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                self.timeout,
            )
        except Exception as exc:
            return _transport_failure(exc)
        return _http_failure(result) or result

    def list_page(self, *, parent_id: str, page_token: str = "", page_size: int = 500) -> Decision:
        query = urlencode({
            "parent_id": parent_id, "page_token": page_token, "limit": page_size,
            "filters": json.dumps({"trashed": {"eq": False}}, separators=(",", ":")),
        })
        result = self._request("GET", f"/drive/v1/files?{query}")
        if isinstance(result, Decision):
            return result
        items = result.payload.get("files")
        token = result.payload.get("next_page_token")
        if not isinstance(items, list) or not isinstance(token, str):
            return Decision(DecisionKind.FAILED, error="invalid list response")
        return Decision(DecisionKind.COMPLETED, payload={"items": items, "next_page_token": token})

    def mkdir(self, *, parent_id: str, name: str) -> Decision:
        result = self._request("POST", "/drive/v1/files", {
            "kind": "drive#folder", "name": name, "parent_id": parent_id, "folder_type": "NORMAL",
        })
        if isinstance(result, Decision):
            return result
        file = result.payload.get("file")
        if (not isinstance(file, dict) or file.get("kind") != "drive#folder"
                or not _file_id(file) or file.get("name") != name
                or file.get("parent_id") != parent_id):
            return Decision(DecisionKind.FAILED, error="invalid directory response")
        return Decision(DecisionKind.DIRECTORY_CREATED, file_id=_file_id(file), payload=_file_payload(result.payload, file))

    def instant_transfer(self, *, record: SourceRecord, parent_id: str) -> Decision:
        result = self._request("POST", "/drive/v1/files", {
            "hash": normalize_gcid(record.gcid), "name": record.file_name,
            "size": str(parse_size(record.size)), "kind": "drive#file", "id": "",
            "parent_id": parent_id, "upload_type": "UPLOAD_TYPE_RESUMABLE",
            "folder_type": "NORMAL", "resumable": {"provider": "PROVIDER_ALIYUN"},
        })
        if isinstance(result, Decision):
            return result
        file = result.payload.get("file")
        if not isinstance(file, dict) or not _file_id(file):
            return Decision(DecisionKind.FAILED, error="invalid file creation response")
        payload = _file_payload(result.payload, file)
        if file.get("phase") == "PHASE_TYPE_COMPLETE":
            try:
                matches = (
                    file.get("kind") == "drive#file"
                    and file.get("name") == record.file_name
                    and file.get("parent_id") == parent_id
                    and normalize_gcid(file.get("hash")) == normalize_gcid(record.gcid)
                    and parse_size(file.get("size")) == parse_size(record.size)
                )
            except ValueError:
                matches = False
            if not matches:
                return Decision(DecisionKind.RETRYABLE, error="invalid completed file response")
            return Decision(DecisionKind.COMPLETED, file_id=_file_id(file), payload=payload)
        return Decision(
            DecisionKind.NOT_REUSABLE, error="strict instant transfer missed; pending file may remain",
            file_id=_file_id(file), payload=payload,
        )
