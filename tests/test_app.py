from __future__ import annotations

import json

import pytest

from pikpak_fastlink import app
from pikpak_fastlink.api import Decision, DecisionKind


GCID = "58A3F526EE3C569FEECCDFD66DAC9631614E7578"


class FakeClient:
    def __init__(self):
        self.children = {"": {}}
        self.transfer_calls = 0

    def list_page(self, *, parent_id, page_token='', page_size=500):
        items = (
            list(self.children.setdefault(parent_id, {}).values()) if not page_token else []
        )
        return Decision(
            DecisionKind.COMPLETED,
            payload={"items": items, "next_page_token": ""},
        )

    def mkdir(self, *, parent_id, name):
        raise AssertionError("mkdir should not be called")

    def instant_transfer(self, *, record, parent_id):
        self.transfer_calls += 1
        self.children[parent_id][record.file_name] = {
            "id": "file-id",
            "name": record.file_name,
            "size": str(record.size),
            "hash": record.gcid,
            "kind": "drive#file", "phase": "PHASE_TYPE_COMPLETE", "trashed": False}
        return Decision(DecisionKind.COMPLETED)


def test_cli_import_runs_end_to_end_and_resumes(tmp_path, monkeypatch):
    source = tmp_path / "input.json"
    source.write_text(
        json.dumps({"files": [{"path": "/a.rar", "gcid": GCID, "size": "123"}]}),
        encoding="utf-8",
    )
    state = tmp_path / "state.sqlite3"
    client = FakeClient()
    monkeypatch.setattr(app, "build_client", lambda: client)
    argv = [
        "import-json",
        "--file",
        str(source),
        "--target-parent-id",
        "root",
        "--state-file",
        str(state),
        "--flush-every",
        "1",
    ]

    assert app.run_cli(argv) == 0
    assert app.run_cli(argv) == 0
    assert client.transfer_calls == 1


@pytest.mark.parametrize("command", ["import-json", "batch-import-json"])
def test_strict_miss_retry_failed_and_resume(tmp_path, monkeypatch, capsys, command):
    source = tmp_path / "input" / "one.json"
    source.parent.mkdir()
    source.write_text(json.dumps({"files": [
        {"path": "/a.rar", "gcid": GCID, "size": "123"}
    ]}), encoding="utf-8")

    class MissClient(FakeClient):
        reusable = False

        def instant_transfer(self, **kwargs):
            if self.reusable:
                return super().instant_transfer(**kwargs)
            self.transfer_calls += 1
            return Decision(DecisionKind.NOT_REUSABLE, error="instant reuse unavailable")

    client = MissClient()
    monkeypatch.setattr(app, "build_client", lambda: client)
    if command == "import-json":
        state = tmp_path / "state.sqlite3"
        argv = [command, "--file", str(source), "--state-file", str(state)]
        retry = state.with_suffix(".retry.export.json")
    else:
        state_dir = tmp_path / "state"
        argv = [command, "--input-dir", str(source.parent), "--state-dir", str(state_dir)]
        retry = state_dir / "one.import-state.retry.export.json"
    assert app.run_cli([*argv, "--target-parent-id", "root"]) == 1
    assert client.transfer_calls == 1
    assert "NOT_REUSABLE: a.rar" in capsys.readouterr().out
    assert json.loads(retry.read_text(encoding="utf-8"))["files"] == [
        {"path": "/a.rar", "gcid": GCID, "size": "123"}
    ]
    client.reusable = True
    assert app.run_cli([*argv, "--target-parent-id", "root", "--retry-failed"]) == 0
    assert client.transfer_calls == 2
    assert not retry.exists()
    assert app.run_cli([*argv, "--target-parent-id", "root"]) == 0
    assert client.transfer_calls == 2


def test_rejected_credentials_explain_recopy_and_leave_work_resumable(tmp_path, monkeypatch, capsys):
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"files": [{"path": "/a.mkv", "gcid": GCID, "size": "123"}]}), encoding="utf-8")
    client = FakeClient()
    transfer = client.instant_transfer

    def reject(**kwargs):
        client.transfer_calls += 1
        return Decision(DecisionKind.CREDENTIAL_FATAL, error="do-not-print-token")

    monkeypatch.setattr(client, "instant_transfer", reject)
    monkeypatch.setattr(app, "build_client", lambda: client)
    argv = ["import-json", "--file", str(source), "--target-parent-id", "root",
            "--state-file", str(tmp_path / "state.sqlite3"), "--workers", "1"]
    assert app.run_cli(argv) == 1
    output = capsys.readouterr().out
    assert "Credentials rejected" in output and ".env" in output
    assert "do-not-print-token" not in output
    monkeypatch.setattr(client, "instant_transfer", transfer)
    assert app.run_cli(argv) == 0
    assert client.transfer_calls == 2


def test_dry_run_does_not_load_credentials_or_touch_retry_output(tmp_path, monkeypatch):
    source = tmp_path / "input.json"
    source.write_text(
        json.dumps({"files": [{"path": "/a.rar", "gcid": GCID, "size": "123"}]}),
        encoding="utf-8",
    )
    state = tmp_path / "state.sqlite3"
    retry = state.with_suffix(".retry.export.json")
    retry.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        app,
        "build_client",
        lambda: (_ for _ in ()).throw(AssertionError("credentials loaded")),
    )

    assert (
        app.run_cli(
            [
                "import-json",
                "--file",
                str(source),
                "--target-parent-id",
                "root",
                "--state-file",
                str(state),
                "--dry-run",
            ]
        )
        == 0
    )
    assert retry.read_text(encoding="utf-8") == "keep"


def test_generate_json_cli_needs_no_credentials(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"a")
    output = tmp_path / "out.json"
    monkeypatch.setattr(
        app,
        "build_client",
        lambda: (_ for _ in ()).throw(AssertionError("credentials loaded")),
    )

    assert (
        app.run_cli(
            [
                "generate-json",
                "--source-dir",
                str(source),
                "--output-file",
                str(output),
                "--workers",
                "2",
            ]
        )
        == 0
    )
    assert json.loads(output.read_text(encoding="utf-8"))["sourceTag"] == "local"


def test_compare_folder_prints_only_local_files_missing_remotely(
    tmp_path, monkeypatch, capsys
):
    local = tmp_path / "local"
    (local / "foldert").mkdir(parents=True)
    (local / "matching.txt").write_text("local", encoding="utf-8")
    (local / "missing_file1.txt").write_text("missing", encoding="utf-8")
    (local / "foldert" / "present.txt").write_text("local", encoding="utf-8")
    (local / "foldert" / "m2.txt").write_text("missing", encoding="utf-8")

    class CompareClient:
        def list_page(self, *, parent_id, page_token='', page_size=500):
            items = {
                "remote-root": [
                    {"id": "matching", "name": "matching.txt", "kind": "drive#file",
                     "size": "5", "hash": GCID, "phase": "PHASE_TYPE_COMPLETE", "trashed": False},
                    {"id": "foldert", "name": "foldert", "kind": "drive#folder"},
                    {"id": "remote-only", "name": "remote-only.txt", "kind": "drive#file",
                     "size": "5", "hash": GCID, "phase": "PHASE_TYPE_COMPLETE", "trashed": False},
                ],
                "foldert": [
                    {"id": "present", "name": "present.txt", "kind": "drive#file",
                     "size": "5", "hash": GCID, "phase": "PHASE_TYPE_COMPLETE", "trashed": False},
                ],
            }.get(parent_id, [])
            page_items = items if not page_token else []
            return Decision(
                DecisionKind.COMPLETED,
                payload={"items": page_items, "next_page_token": ""},
            )

    monkeypatch.setattr(app, "build_client", CompareClient)

    argv = [
        "compare-folder",
        "--local-folder",
        str(local),
        "--remote-folder-id",
        "remote-root",
    ]
    for direction in ([], ["--local-only"]):
        assert app.run_cli([*argv, *direction]) == 0
        assert capsys.readouterr().out.splitlines() == [
            "/foldert/m2.txt",
            "/missing_file1.txt",
        ]

    assert app.run_cli([*argv, "--remote-only"]) == 0
    assert capsys.readouterr().out.splitlines() == ["/remote-only.txt"]
