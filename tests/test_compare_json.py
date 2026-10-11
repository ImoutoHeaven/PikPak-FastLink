from __future__ import annotations

import json

import pytest

from pikpak_fastlink import app
from pikpak_fastlink.api import Decision, DecisionKind


SHARED = "A" * 40
LOCAL_ONLY = "B" * 40
REMOTE_ONLY = "C" * 40


class TreeClient:
    def __init__(self):
        self.items = {
            "": [
                {"id": "dir", "name": "deep", "kind": "drive#folder"},
                {
                    "id": "shared",
                    "name": "renamed (1).bin",
                    "kind": "drive#file",
                    "hash": SHARED,
                    "size": "99", "phase": "PHASE_TYPE_COMPLETE", "trashed": False},
                {
                    "id": "collision",
                    "name": "collision.bin",
                    "kind": "drive#file",
                    "hash": REMOTE_ONLY,
                    "size": "7", "phase": "PHASE_TYPE_COMPLETE", "trashed": False},
            ],
            "dir": [
                {
                    "id": "extra",
                    "name": "extra.bin",
                    "kind": "drive#file",
                    "hash": REMOTE_ONLY.lower(),
                    "size": "7", "phase": "PHASE_TYPE_COMPLETE", "trashed": False},
            ],
        }
        self.calls = []

    def list_page(self, *, parent_id, page_token='', page_size=500):
        self.calls.append((parent_id, page_token))
        items = self.items[parent_id]
        index = int(page_token or 0)
        # Opaque tokens deliberately accompany short pages.
        return Decision(
            DecisionKind.COMPLETED,
            payload={"items": items[index : index + 1],
                     "next_page_token": str(index + 1) if index + 1 < len(items) else ""},
        )


def test_json_compare_uses_gcid_and_keeps_output_side_paths(tmp_path, monkeypatch):
    source = tmp_path / "local.json"
    source.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": "/collection/original.bin",
                        "gcid": SHARED.lower(),
                        "size": "1",
                    },
                    {"path": "/another/copy.bin", "gcid": SHARED, "size": "1"},
                    {"path": "/nested/missing.bin", "gcid": LOCAL_ONLY, "size": "7"},
                    {
                        "path": "/collision.bin",
                        "gcid": LOCAL_ONLY,
                        "size": "7",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "missing.json"
    argv = [
        "compare-folder",
        "--local-json",
        str(source),
        "--remote-folder-id",
        "root",
        "--output-file",
        str(output),
    ]
    client = TreeClient()
    monkeypatch.setattr(app, "build_client", lambda: client)

    for direction in ([], ["--local-only"]):
        assert app.run_cli([*argv, *direction]) == 0
        result = json.loads(output.read_text(encoding="utf-8"))
        assert [r["path"] for r in result["files"]] == [
            "/collision.bin",
            "/nested/missing.bin",
        ]
        assert result["totalFilesCount"] == 2
        assert result["totalSize"] == 14
        assert result["sourceTag"] == "local"

    assert app.run_cli([*argv, "--remote-only"]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["files"] == [
        {
            "path": "/collision.bin",
            "gcid": REMOTE_ONLY,
            "size": "7",
            "fileId": "collision",
        },
        {
            "path": "/deep/extra.bin",
            "gcid": REMOTE_ONLY,
            "size": "7",
            "fileId": "extra",
            "parentId": "dir",
        },
    ]
    assert result["sourceTag"] == "pikpak"
    assert ("", "2") in client.calls and ("dir", "") in client.calls

    # A new remote copy covers all local paths with that GCID on the next run.
    client.items[""].append(
        {
            "id": "arrived",
            "name": "flat.bin",
            "kind": "drive#file",
            "hash": LOCAL_ONLY,
            "size": "7", "phase": "PHASE_TYPE_COMPLETE", "trashed": False}
    )
    assert app.run_cli(argv) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["files"] == []
    assert result["totalFilesCount"] == result["totalSize"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"files": {}},
        {
            "files": [
                {"path": "/bad.bin", "size": "1", "gcid": "bad"},
            ]
        },
    ],
)
def test_invalid_local_json_preserves_previous_output(tmp_path, monkeypatch, payload):
    source, output = tmp_path / "local.json", tmp_path / "out.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    output.write_text("previous output", encoding="utf-8")
    client = TreeClient()
    monkeypatch.setattr(app, "build_client", lambda: client)
    assert (
        app.run_cli(
            [
                "compare-folder",
                "--local-json",
                str(source),
                "--remote-folder-id",
                "root",
                "--output-file",
                str(output),
            ]
        )
        == 1
    )
    assert output.read_text(encoding="utf-8") == "previous output"
    assert client.calls == []


def test_empty_json_and_remote_failure_preserve_correct_output(tmp_path, monkeypatch):
    source, output = tmp_path / "local.json", tmp_path / "out.json"
    source.write_text('{"files": []}', encoding="utf-8")
    client = TreeClient()
    monkeypatch.setattr(app, "build_client", lambda: client)
    argv = [
        "compare-folder",
        "--local-json",
        str(source),
        "--remote-folder-id",
        "root",
        "--output-file",
        str(output),
    ]
    assert app.run_cli(argv) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["files"] == []
    assert app.run_cli([*argv, "--remote-only"]) == 0
    previous = output.read_bytes()
    assert json.loads(previous)["totalFilesCount"] == 3

    client.items["dir"][0]["hash"] = "invalid"
    assert app.run_cli(argv) == 1
    assert output.read_bytes() == previous

    monkeypatch.setattr(
        client,
        "list_page",
        lambda **kw: Decision(
            DecisionKind.COMPLETED, payload={"items": [], "next_page_token": None}
        ),
    )
    assert app.run_cli(argv) == 1
    assert output.read_bytes() == previous

    monkeypatch.setattr(
        client,
        "list_page",
        lambda **kw: Decision(DecisionKind.CREDENTIAL_FATAL, error="invalid token"),
    )
    assert app.run_cli(argv) == 1
    assert output.read_bytes() == previous

    # A repeated nonterminal token must never publish a partial snapshot.
    monkeypatch.setattr(
        client,
        "list_page",
        lambda **kw: Decision(
            DecisionKind.COMPLETED,
            payload={
                "items": client.items[""][1:2] if not kw["page_token"] else [],
                "next_page_token": "again",
            },
        ),
    )
    assert app.run_cli(argv) == 1
    assert output.read_bytes() == previous


def test_duplicate_remote_names_preserve_previous_manifest(tmp_path, monkeypatch):
    source, output = tmp_path / "local.json", tmp_path / "out.json"
    source.write_text('{"files": []}', encoding="utf-8")
    output.write_text("previous output", encoding="utf-8")
    client = TreeClient()
    client.items[""].append(dict(client.items[""][1], id="duplicate-id"))
    monkeypatch.setattr(app, "build_client", lambda: client)
    assert app.run_cli([
        "compare-folder", "--local-json", str(source), "--remote-folder-id", "root",
        "--output-file", str(output), "--remote-only",
    ]) == 1
    assert output.read_text(encoding="utf-8") == "previous output"


def test_empty_remote_returns_all_local_records(tmp_path, monkeypatch):
    source, output = tmp_path / "local.json", tmp_path / "out.json"
    records = [{"path": "/a.bin", "size": "1", "gcid": SHARED}]
    source.write_text(json.dumps({"files": records}), encoding="utf-8")
    client = TreeClient()
    client.items = {"": []}
    monkeypatch.setattr(app, "build_client", lambda: client)
    argv = [
        "compare-folder",
        "--local-json",
        str(source),
        "--remote-folder-id",
        "root",
        "--output-file",
        str(output),
    ]
    assert app.run_cli(argv) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["files"] == records
    assert app.run_cli([*argv, "--remote-only"]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["files"] == []
