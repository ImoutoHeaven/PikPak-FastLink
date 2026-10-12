from __future__ import annotations

import json
from types import SimpleNamespace

from pikpak_fastlink.api import Decision, DecisionKind
import pytest

from pikpak_fastlink.batch import discover_jobs, run_batch_import


GCID_A = "58A3F526EE3C569FEECCDFD66DAC9631614E7578"
GCID_B = "8A154A225F16FC3A8C6D554164BC29D3B0053490"


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
        file_id = f"folder-{name}"
        self.children[parent_id][name] = {
            "id": file_id,
            "name": name,
            "kind": "drive#folder",
        }
        self.children.setdefault(file_id, {})
        return Decision(DecisionKind.DIRECTORY_CREATED, file_id=file_id)

    def instant_transfer(self, *, record, parent_id):
        self.transfer_calls += 1
        self.children[parent_id][record.file_name] = {
            "id": f"file-{self.transfer_calls}",
            "name": record.file_name,
            "size": str(record.size),
            "hash": record.gcid,
            "kind": "drive#file", "phase": "PHASE_TYPE_COMPLETE", "trashed": False}
        return Decision(DecisionKind.COMPLETED)


def make_config(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    return SimpleNamespace(
        input_dir=input_dir,
        target_parent_id="",
        target_path=None,
        state_dir=tmp_path / "state",
        workers=2,
        json_parallelism=2,
        max_retries=0,
        flush_every=1,
        retry_failed=False,
        dry_run=False,
    )


def write_json(path, *, relative_path, gcid):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"files": [{"path": f"/{relative_path}", "gcid": gcid, "size": "1"}]}
        ),
        encoding="utf-8",
    )


def test_batch_import_runs_non_colliding_jobs(tmp_path):
    config = make_config(tmp_path)
    write_json(config.input_dir / "one.json", relative_path="one.bin", gcid=GCID_A)
    write_json(
        config.input_dir / "nested/two.json", relative_path="two.bin", gcid=GCID_B
    )
    client = FakeClient()
    assert run_batch_import(config=config, client=client) == 0
    assert client.transfer_calls == 2


def test_batch_import_rejects_collision_before_remote_mutation(tmp_path):
    config = make_config(tmp_path)
    write_json(config.input_dir / "one.json", relative_path="same.bin", gcid=GCID_A)
    write_json(config.input_dir / "two.json", relative_path="same.bin", gcid=GCID_B)
    client = FakeClient()
    assert run_batch_import(config=config, client=client) == 1
    assert client.transfer_calls == 0


def test_malformed_job_does_not_block_valid_job(tmp_path):
    config = make_config(tmp_path)
    (config.input_dir / "bad.json").write_text("{", encoding="utf-8")
    write_json(config.input_dir / "good.json", relative_path="good.bin", gcid=GCID_A)
    client = FakeClient()
    assert run_batch_import(config=config, client=client) == 1
    assert client.transfer_calls == 1


def test_batch_roots_must_be_disjoint_in_both_directions(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    write_json(input_dir / "one.json", relative_path="one.bin", gcid=GCID_A)
    with pytest.raises(ValueError, match="disjoint"):
        discover_jobs(
            input_dir=input_dir,
            state_dir=tmp_path,
            suffix=".import-state.sqlite3",
        )
