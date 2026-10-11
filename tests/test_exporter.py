from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pikpak_fastlink.api import Decision, DecisionKind
from pikpak_fastlink.exporter import run_export


GCID_A = "58A3F526EE3C569FEECCDFD66DAC9631614E7578"
GCID_B = "8A154A225F16FC3A8C6D554164BC29D3B0053490"


class FakeListClient:
    def __init__(self):
        self.items = {
            "": [
                {"id": "folder", "name": "dir", "kind": "drive#folder"},
                {
                    "id": "a",
                    "name": "a.rar",
                    "size": "10",
                    "hash": GCID_A,
                    "parent_id": "",
                    "kind": "drive#file", "phase": "PHASE_TYPE_COMPLETE", "trashed": False},
            ],
            "folder": [
                {
                    "id": "b",
                    "name": "b.rar",
                    "size": "20",
                    "hash": GCID_B,
                    "parent_id": "folder",
                    "kind": "drive#file", "phase": "PHASE_TYPE_COMPLETE", "trashed": False}
            ],
        }

    def list_page(self, *, parent_id, page_token='', page_size=500):
        items = self.items.get(parent_id, []) if not page_token else []
        return Decision(
            DecisionKind.COMPLETED,
            payload={"items": items, "next_page_token": ""},
        )


def config(tmp_path, source=""):
    return SimpleNamespace(
        source_parent_id=source,
        output_file=tmp_path / "out.json",
        state_file=tmp_path / "export.state.json",
        workers=2,
        max_retries=0,
    )


def test_recursive_export_writes_userscript_format_and_cleans_state(tmp_path):
    cfg = config(tmp_path)
    assert run_export(client=FakeListClient(), config=cfg) == 0

    payload = json.loads(cfg.output_file.read_text(encoding="utf-8"))
    assert payload["sourceTag"] == "pikpak"
    assert payload["totalFilesCount"] == 2
    assert payload["totalSize"] == 30
    assert payload["files"] == [
        {
            "size": "10",
            "path": "/a.rar",
            "gcid": GCID_A,
            "fileId": "a",
        },
        {
            "size": "20",
            "path": "/dir/b.rar",
            "gcid": GCID_B,
            "fileId": "b",
            "parentId": "folder",
        },
    ]
    assert not cfg.state_file.exists()
    assert not cfg.state_file.with_suffix(".records.jsonl").exists()


def test_export_rejects_empty_source(tmp_path):
    cfg = config(tmp_path)
    client = FakeListClient()
    client.items = {"": []}
    with pytest.raises(RuntimeError, match="zero files"):
        run_export(client=client, config=cfg)


def test_output_can_use_a_different_directory_from_state(tmp_path):
    cfg = config(tmp_path)
    cfg.output_file = tmp_path / "output" / "out.json"
    cfg.state_file = tmp_path / "state" / "export.state.json"
    assert run_export(client=FakeListClient(), config=cfg) == 0
    assert cfg.output_file.exists()
    assert not (cfg.output_file.parent / f".{cfg.output_file.name}.tmp").exists()


def test_export_resumes_validated_directories_and_discards_partial_sidecar_tail(tmp_path):
    cfg = config(tmp_path)
    client = FakeListClient()
    original_list = client.list_page
    client.list_page = lambda **kw: (
        Decision(DecisionKind.RETRYABLE, error="connection lost")
        if kw["parent_id"] == "folder" else original_list(**kw)
    )
    with pytest.raises(RuntimeError, match="connection lost"):
        run_export(client=client, config=cfg)
    sidecar = cfg.state_file.with_suffix(".records.jsonl")
    with sidecar.open("ab") as handle:
        handle.write(b'{"partial"')
    client.list_page = original_list
    assert run_export(client=client, config=cfg) == 0
    payload = json.loads(cfg.output_file.read_text(encoding="utf-8"))
    assert [r["path"] for r in payload["files"]] == ["/a.rar", "/dir/b.rar"]
    assert payload["totalFilesCount"] == 2
    assert not sidecar.exists()


@pytest.mark.parametrize("field,value", [
    ("id", ""), ("phase", "PHASE_TYPE_PENDING"), ("hash", "invalid"),
    ("size", "-1"), ("kind", "unknown"),
])
def test_export_rejects_invalid_remote_files_without_replacing_output(tmp_path, field, value):
    cfg = config(tmp_path)
    cfg.output_file.write_text("previous output", encoding="utf-8")
    client = FakeListClient()
    entry = dict(client.items[""][1], **{field: value})
    client.items = {"": [entry]}
    with pytest.raises((RuntimeError, ValueError)):
        run_export(client=client, config=cfg)
    assert cfg.output_file.read_text(encoding="utf-8") == "previous output"


def test_export_rejects_unvalidated_checkpoint_without_changing_artifacts(tmp_path):
    cfg = config(tmp_path)
    client = FakeListClient()
    client.items = {"": []}
    with pytest.raises(RuntimeError, match="zero files"):
        run_export(client=client, config=cfg)
    payload = json.loads(cfg.state_file.read_text(encoding="utf-8"))
    payload.pop("listing_version", None)
    cfg.state_file.write_text(json.dumps(payload), encoding="utf-8")
    cfg.output_file.write_text("previous output", encoding="utf-8")
    state_bytes = cfg.state_file.read_bytes()
    with pytest.raises(ValueError, match="pagination validation"):
        run_export(client=FakeListClient(), config=cfg)
    assert cfg.state_file.read_bytes() == state_bytes
    assert cfg.output_file.read_text(encoding="utf-8") == "previous output"
