from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pikpak_fastlink import app, runner
from pikpak_fastlink.api import Decision, DecisionKind
from pikpak_fastlink.runner import RemoteTreeCoordinator, list_remote_children


def item(index, **overrides):
    return {
        "id": str(index), "name": f"{index:03d}.bin", "kind": "drive#file",
        "size": "1", "hash": f"{index:040X}",
        "phase": "PHASE_TYPE_COMPLETE", "trashed": False, **overrides,
    }


def page(indices, next_token=""):
    return Decision(DecisionKind.COMPLETED, payload={
        "items": [item(i) for i in indices], "next_page_token": next_token,
    })


class PagesClient:
    def __init__(self, pages, retry_token=None):
        self.pages = pages
        self.retry_token = retry_token
        self.calls = []

    def list_page(self, *, parent_id, page_token="", page_size=500):
        self.calls.append(page_token)
        if self.retry_token == page_token:
            self.retry_token = None
            return Decision(DecisionKind.RETRYABLE, error="timeout")
        return self.pages[page_token]

    def mkdir(self, **kwargs):
        raise AssertionError("unexpected mutation")

    def instant_transfer(self, **kwargs):
        raise AssertionError("unexpected mutation")


@pytest.mark.parametrize("tail", [
    Decision(DecisionKind.COMPLETED, payload={}),
    Decision(DecisionKind.COMPLETED, payload={"items": [], "next_page_token": None}),
    Decision(DecisionKind.COMPLETED, payload={"items": {}, "next_page_token": ""}),
    page([], "next"),
    page([0]),
    Decision(DecisionKind.COMPLETED, payload={"items": [item(0, name="renamed.bin")], "next_page_token": ""}),
    Decision(DecisionKind.COMPLETED, payload={"items": [item(1, name="../outside.bin")], "next_page_token": ""}),
    Decision(DecisionKind.COMPLETED, payload={"items": [item(1, parent_id="other-folder")], "next_page_token": ""}),
    Decision(DecisionKind.COMPLETED, payload={"items": [item(1, id=1)], "next_page_token": ""}),
])
def test_malformed_or_replayed_pages_fail_without_caching(tail):
    client = PagesClient({"": page([0], "next"), "next": tail})
    coordinator = RemoteTreeCoordinator(client)
    with pytest.raises(RuntimeError):
        coordinator.get_child(parent_id="", name="119.bin", max_retries=0)
    client.pages = {"": page([119])}
    assert coordinator.get_child(parent_id="", name="119.bin", max_retries=0)["id"] == "119"
    assert client.calls == ["", "next", ""]


@pytest.mark.parametrize("count", [0, 1, 500, 501])
def test_only_empty_token_terminates_listing_and_retry_keeps_token(count, monkeypatch):
    # Empty nonterminal pages and short pages are legal; length never ends a scan.
    client = PagesClient({"": page([], "first"), "first": page(range(count), "last"), "last": page([])}, retry_token="first")
    monkeypatch.setattr(runner, "compute_backoff", lambda attempt: 0)
    result = list_remote_children(client=client, parent_id="", max_retries=1)
    assert [entry["id"] for entry in result] == [str(i) for i in range(count)]
    assert client.calls == ["", "first", "first", "last"]


@pytest.mark.parametrize("overrides", [
    {"phase": "PHASE_TYPE_PENDING"}, {"size": True}, {"hash": "invalid"},
])
def test_trashed_file_does_not_block_import_but_unverified_file_does(overrides):
    client = PagesClient({"": Decision(DecisionKind.COMPLETED, payload={
        "items": [item(0, trashed=True), item(1, **overrides)],
        "next_page_token": "",
    })})
    coordinator = RemoteTreeCoordinator(client)
    for index, expected in [(0, "missing"), (1, "collision")]:
        assert coordinator.classify_file(parent_id="", record=SimpleNamespace(
            file_name=f"{index:03d}.bin", gcid=f"{index:040X}", size=1,
        ), max_retries=0) == expected


@pytest.mark.parametrize("command", ["import-json", "batch-import-json", "batch-check-json", "export-json", "compare-json", "compare-path"])
def test_commands_preserve_artifacts_on_invalid_listing_then_resume(command, tmp_path, monkeypatch, capsys):
    source = tmp_path / "input" / "source.json"
    source.parent.mkdir()
    source.write_text(json.dumps({"files": [{"path": "/119.bin", "size": "1", "gcid": item(119)["hash"]}]}), encoding="utf-8")
    output = tmp_path / "out.json"
    output.write_text("previous output", encoding="utf-8")
    state = tmp_path / "state"
    if command == "import-json":
        argv = [command, "--file", str(source), "--target-parent-id", "root", "--state-file", str(state / "import.sqlite3")]
    elif command in {"batch-import-json", "batch-check-json"}:
        argv = [command, "--input-dir", str(source.parent), "--target-parent-id", "root", "--state-dir", str(state)]
        if command == "batch-check-json":
            output = tmp_path / "delta" / "source.delta.export.json"
            output.parent.mkdir()
            output.write_text("previous output", encoding="utf-8")
            argv += ["--output-dir", str(output.parent)]
    elif command == "export-json":
        argv = [command, "--source-parent-id", "root", "--output-file", str(output), "--state-file", str(state / "export.state.json")]
    else:
        argv = ["compare-folder", "--remote-folder-id", "root"]
        if command == "compare-json":
            argv += ["--local-json", str(source), "--output-file", str(output)]
        else:
            local = tmp_path / "local"
            local.mkdir()
            (local / "119.bin").touch()
            argv += ["--local-folder", str(local)]
    argv += ["--max-retries", "0"]
    client = PagesClient({"": page(range(50), "next"), "next": page([], "next")})
    monkeypatch.setattr(app, "build_client", lambda: client)
    assert app.run_cli(argv) == 1
    assert output.read_text(encoding="utf-8") == "previous output"
    capsys.readouterr()
    client.pages = {"": page(range(50), "next"), "next": page(range(50, 120))}
    assert app.run_cli(argv) == 0
    assert client.calls[-2:] == ["", "next"]
    if command == "batch-check-json":
        assert not output.exists()
    elif command == "compare-json":
        assert json.loads(output.read_text(encoding="utf-8"))["files"] == []
    elif command == "export-json":
        assert json.loads(output.read_text(encoding="utf-8"))["totalFilesCount"] == 120
    elif command == "compare-path":
        assert capsys.readouterr().out == ""
