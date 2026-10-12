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
        self.listed = []

    def list_page(self, *, parent_id, page_token='', page_size=500):
        self.listed.append(parent_id)
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


OTHER_GCID = "0" * 40
PATH_COMMANDS = ["import-json", "batch-import-json", "batch-check-json", "export-json", "compare-folder"]


def remote_file(file_id, name, gcid):
    return {"id": file_id, "name": name, "size": "123", "hash": gcid,
            "kind": "drive#file", "phase": "PHASE_TYPE_COMPLETE", "trashed": False}


def media_client(media_id="media-id", files=True):
    client = FakeClient()
    client.children = {
        "": {"root.rar": remote_file("root-file", "root.rar", OTHER_GCID),
             "Media": {"id": media_id, "name": "Media", "kind": "drive#folder", "trashed": False}},
        media_id: {"a.rar": remote_file("a-file", "a.rar", GCID)} if files else {},
    }
    return client


def path_argv(command, tmp_path, path):
    source = tmp_path / "input" / "one.json"
    source.parent.mkdir(exist_ok=True)
    source.write_text(json.dumps({"files": [{"path": "/a.rar", "gcid": GCID, "size": "123"}]}), encoding="utf-8")
    state = tmp_path / "state"
    if command == "import-json":
        return [command, "--file", str(source), "--state-file", str(state / "one.sqlite3"), "--target-path", path]
    if command == "export-json":
        return [command, "--output-file", str(tmp_path / "out.json"), "--state-file", str(state / "export.json"), "--source-path", path]
    if command == "compare-folder":
        return [command, "--local-json", str(source), "--output-file", str(tmp_path / "out.json"), "--remote-only", "--remote-path", path]
    argv = [command, "--input-dir", str(source.parent), "--state-dir", str(state), "--target-path", path]
    return argv + (["--output-dir", str(tmp_path / "delta")] if command == "batch-check-json" else [])


@pytest.mark.parametrize("command", PATH_COMMANDS)
def test_remote_path_scopes_command_to_resolved_folder_id(command, tmp_path, monkeypatch, capsys):
    client = media_client()
    monkeypatch.setattr(app, "build_client", lambda: client)
    argv = path_argv(command, tmp_path, "/Media/")

    assert app.run_cli(argv) == 0
    # Media already holds a.rar and the root holds another file, so any root-scoped run differs.
    assert client.transfer_calls == 0
    assert not (tmp_path / "delta" / "one.delta.export.json").exists()
    if command in {"export-json", "compare-folder"}:
        files = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["files"]
        assert [f["path"] for f in files] == (["/a.rar"] if command == "export-json" else [])
        return
    # Recreating the same path gives a new folder ID, which the saved ID-bound state rejects.
    recreated = media_client("media-2")
    monkeypatch.setattr(app, "build_client", lambda: recreated)
    capsys.readouterr()
    assert app.run_cli(argv) == 1
    assert "mismatch" in capsys.readouterr().out
    assert "media-2" not in recreated.listed and recreated.transfer_calls == 0


@pytest.mark.parametrize("command", PATH_COMMANDS)
@pytest.mark.parametrize("path", ["/Missing", "/root.rar"])
def test_unresolvable_remote_path_fails_before_writing_state(command, path, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(app, "build_client", media_client)
    argv = path_argv(command, tmp_path, path)

    assert app.run_cli(argv) == 1
    assert "Error:" in capsys.readouterr().out
    assert [p.name for p in tmp_path.iterdir()] == ["input"]


@pytest.mark.parametrize("command", ["batch-import-json", "batch-check-json"])
def test_batch_remote_path_checks_local_headers_before_lookup(command, tmp_path, monkeypatch, capsys):
    argv = path_argv(command, tmp_path, "/Media")
    (tmp_path / "input" / "bad.json").write_text("[]", encoding="utf-8")
    client = media_client()
    monkeypatch.setattr(app, "build_client", lambda: client)

    assert app.run_cli(argv) == 1
    assert "bad.json" in capsys.readouterr().out
    assert client.listed == []


@pytest.mark.parametrize("command", ["import-json", "batch-import-json"])
@pytest.mark.parametrize("selector", [["--target-parent-id", "root"], ["--target-path", "/Media"]])
def test_dry_run_reads_only_and_id_mode_stays_offline(command, selector, tmp_path, monkeypatch, capsys):
    argv = path_argv(command, tmp_path, "/")[:-2] + selector + ["--dry-run"]
    client = media_client(files=False)
    built = []
    monkeypatch.setattr(app, "build_client", lambda: built.append(1) or client)

    assert app.run_cli(argv) == 0
    assert "Dry run: no remote mutations performed" in capsys.readouterr().out
    assert built == ([] if selector[0] == "--target-parent-id" else [1])
    assert client.transfer_calls == 0 and client.children["media-id"] == {}


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
