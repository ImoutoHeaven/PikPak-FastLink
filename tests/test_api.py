from __future__ import annotations

import json
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse

import pytest

from pikpak_fastlink.api import DecisionKind, HttpResult, PikPakClient, _default_transport
from pikpak_fastlink.models import SourceRecord


GCID = "AA0A217D4D2AAE5D0E05BCB4C99EA039AE92735A"
RECORD = SourceRecord(1, "folder/a.mkv", "a.mkv", "folder", GCID.lower(), 422649548)


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, json.loads(body) if body else None, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(transport):
    return PikPakClient(
        authorization="Bearer authorization-secret", device_id="device-secret",
        captcha_token="captcha-secret", transport=transport,
    )


def test_list_uses_server_token_even_when_server_exceeds_requested_limit():
    items = [{"id": "file-a", "kind": "drive#file", "name": "a.mkv", "size": "422649548", "hash": GCID}]
    transport = FakeTransport(HttpResult(200, {"files": items, "next_page_token": "next+/="}))
    decision = client(transport).list_page(parent_id="folder", page_token="prev+/=", page_size=1)
    assert decision.kind is DecisionKind.COMPLETED
    assert decision.payload == {"items": items, "next_page_token": "next+/="}
    method, url, headers, body, timeout = transport.calls[0]
    assert method == "GET" and body is None and timeout == 30
    assert urlparse(url).scheme == "https"
    assert urlparse(url).netloc == "api-drive.mypikpak.com"
    assert urlparse(url).path == "/drive/v1/files"
    query = parse_qs(urlparse(url).query)
    assert query["parent_id"] == ["folder"]
    assert query["page_token"] == ["prev+/="]
    assert query["limit"] == ["1"]
    assert json.loads(query["filters"][0]) == {"trashed": {"eq": False}}
    assert headers == {
        "Authorization": "Bearer authorization-secret", "X-Device-Id": "device-secret",
        "X-Captcha-Token": "captcha-secret", "Content-Type": "application/json",
    }


def test_list_empty_terminal_page_and_default_limit():
    transport = FakeTransport(HttpResult(200, {"files": [], "next_page_token": ""}))
    decision = client(transport).list_page(parent_id="")
    assert decision.payload == {"items": [], "next_page_token": ""}
    assert parse_qs(urlparse(transport.calls[0][1]).query)["limit"] == ["500"]


@pytest.mark.parametrize("payload", [{}, {"files": {}, "next_page_token": ""}, {"files": []}, {"files": [], "next_page_token": None}, {"files": [], "next_page_token": 1}])
def test_malformed_list_cannot_silently_truncate_export(payload):
    decision = client(FakeTransport(HttpResult(200, payload))).list_page(parent_id="")
    assert decision.kind is DecisionKind.FAILED


def test_mkdir_uses_pikpak_folder_creation():
    transport = FakeTransport(HttpResult(200, {
        "upload_type": "UPLOAD_TYPE_UNKNOWN",
        "file": {"id": "new-folder", "kind": "drive#folder", "name": "Movies",
                 "parent_id": "parent", "phase": "PHASE_TYPE_COMPLETE", "folder_type": "NORMAL"},
    }))
    decision = client(transport).mkdir(parent_id="parent", name="Movies")
    assert decision.kind is DecisionKind.DIRECTORY_CREATED and decision.file_id == "new-folder"
    assert transport.calls[0][0:2] == ("POST", "https://api-drive.mypikpak.com/drive/v1/files")
    assert transport.calls[0][3] == {"kind": "drive#folder", "name": "Movies", "parent_id": "parent", "folder_type": "NORMAL"}


@pytest.mark.parametrize("patch", [
    {"id": ""}, {"id": True}, {"kind": "drive#file"},
    {"name": "Other"}, {"parent_id": "other-parent"},
])
def test_mkdir_requires_matching_folder_metadata(patch):
    file = {"id": "new-folder", "kind": "drive#folder", "name": "Movies", "parent_id": "", **patch}
    decision = client(FakeTransport(HttpResult(200, {"file": file}))).mkdir(parent_id="", name="Movies")
    assert decision.kind is DecisionKind.FAILED


def test_instant_transfer_matches_captured_create_request_and_complete_phase():
    transport = FakeTransport(HttpResult(200, {
        "upload_type": "UPLOAD_TYPE_UNKNOWN",
        "file": {"kind": "drive#file", "id": "VP3fKhEWKSIpAJWznuUlbKn3o2", "name": "a.mkv", "parent_id": "target", "size": "422649548", "hash": GCID, "phase": "PHASE_TYPE_COMPLETE"},
    }))
    decision = client(transport).instant_transfer(record=RECORD, parent_id="target")
    assert decision.kind is DecisionKind.COMPLETED
    assert decision.file_id == "VP3fKhEWKSIpAJWznuUlbKn3o2"
    assert transport.calls[0][3] == {
        "hash": GCID, "name": "a.mkv", "size": "422649548", "kind": "drive#file",
        "id": "", "parent_id": "target", "upload_type": "UPLOAD_TYPE_RESUMABLE",
        "folder_type": "NORMAL", "resumable": {"provider": "PROVIDER_ALIYUN"},
    }
    assert len(transport.calls) == 1


@pytest.mark.parametrize("phase", ["PHASE_TYPE_PENDING", "PHASE_TYPE_RUNNING", "PHASE_TYPE_ERROR", None])
def test_strict_miss_never_uploads_or_deletes_and_redacts_oss_credentials(phase):
    transport = FakeTransport(HttpResult(200, {
        "upload_type": "UPLOAD_TYPE_RESUMABLE", "resumable": {"access_key_secret": "oss-secret"},
        "file": {"id": "new-pending-file", "phase": phase, "unexpected": "response-secret"},
    }))
    decision = client(transport).instant_transfer(record=RECORD, parent_id="target")
    assert decision.kind is DecisionKind.NOT_REUSABLE
    assert decision.file_id == "new-pending-file"
    assert "pending file may remain" in decision.error
    assert decision.payload == {"upload_type": "UPLOAD_TYPE_RESUMABLE", "file": {"id": "new-pending-file", "phase": phase}}
    assert "secret" not in repr(decision)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("patch", [
    {"kind": "drive#folder"}, {"kind": None}, {"hash": "0" * 40},
    {"hash": None}, {"size": "1"}, {"size": None}, {"size": True},
    {"name": "other.mkv"}, {"parent_id": "other-folder"},
])
def test_complete_phase_with_wrong_or_missing_content_is_uncertain(patch):
    file = {"id": "new-file", "kind": "drive#file", "hash": GCID,
            "name": "a.mkv", "parent_id": "target",
            "size": "422649548", "phase": "PHASE_TYPE_COMPLETE", **patch}
    decision = client(FakeTransport(HttpResult(200, {"file": file}))).instant_transfer(record=RECORD, parent_id="target")
    assert decision.kind is DecisionKind.RETRYABLE
    assert decision.error == "invalid completed file response"


@pytest.mark.parametrize("payload", [{}, {"file": {}}, {"file": {"phase": "PHASE_TYPE_COMPLETE"}}, {"file": {"id": True, "phase": "PHASE_TYPE_COMPLETE"}}])
def test_malformed_creation_is_not_completed(payload):
    decision = client(FakeTransport(HttpResult(200, payload))).instant_transfer(record=RECORD, parent_id="target")
    assert decision.kind is DecisionKind.FAILED


@pytest.mark.parametrize("status,payload,kind", [
    (401, {}, DecisionKind.CREDENTIAL_FATAL), (403, {}, DecisionKind.CREDENTIAL_FATAL),
    (400, {"error": "captcha_invalid"}, DecisionKind.CREDENTIAL_FATAL),
    (200, {"error": "captcha_invalid"}, DecisionKind.CREDENTIAL_FATAL),
    (429, {}, DecisionKind.RETRYABLE), (500, {}, DecisionKind.RETRYABLE),
    (503, {}, DecisionKind.RETRYABLE), (302, {}, DecisionKind.FAILED),
    (400, {"error": "invalid_argument"}, DecisionKind.FAILED),
    (200, {"error": {}, "files": [], "next_page_token": ""}, DecisionKind.FAILED),
    (200, None, DecisionKind.RETRYABLE),
])
def test_http_failures_are_classified_without_exposing_server_credentials(status, payload, kind):
    if isinstance(payload, dict):
        payload = {**payload, "error_description": "authorization-secret captcha-secret"}
    decision = client(FakeTransport(HttpResult(status, payload))).list_page(parent_id="")
    assert decision.kind is kind
    assert decision.payload is None
    assert "secret" not in repr(decision)


@pytest.mark.parametrize("failure,kind", [
    (TimeoutError("captcha-secret"), DecisionKind.RETRYABLE),
    (ConnectionError("authorization-secret"), DecisionKind.RETRYABLE),
    (IncompleteRead(b"authorization-secret"), DecisionKind.RETRYABLE),
    (RuntimeError("authorization-secret"), DecisionKind.FAILED),
])
def test_transport_errors_are_classified_without_exception_text(failure, kind):
    decision = client(FakeTransport(failure)).list_page(parent_id="")
    assert decision.kind is kind
    assert "secret" not in repr(decision)


def test_default_transport_does_not_follow_redirect_with_credentials():
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/credential-trap")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _default_transport("GET", f"http://127.0.0.1:{server.server_port}/start", {"Authorization": "Bearer synthetic-secret"}, b"", 5)
        assert result.status == 302
        assert paths == ["/start"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_default_transport_requests_chrome_impersonation(monkeypatch):
    from curl_cffi import requests
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs)
        class Response:
            status_code = 200
            def json(self):
                return {"files": [], "next_page_token": ""}
        return Response()

    monkeypatch.setattr(requests, "request", request)
    assert client(None).list_page(parent_id="").kind is DecisionKind.COMPLETED
    assert calls[0]["impersonate"] == "chrome"


def test_curl_timeout_is_retryable_without_credential_text(monkeypatch):
    from curl_cffi import requests

    def request(*args, **kwargs):
        raise requests.exceptions.Timeout("authorization-secret")

    monkeypatch.setattr(requests, "request", request)
    decision = client(None).list_page(parent_id="")
    assert decision.kind is DecisionKind.RETRYABLE
    assert "secret" not in repr(decision)
