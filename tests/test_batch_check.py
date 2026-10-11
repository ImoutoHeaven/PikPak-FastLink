from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pikpak_fastlink.api import Decision, DecisionKind
from pikpak_fastlink.batch import run_batch_check


GCID = "58A3F526EE3C569FEECCDFD66DAC9631614E7578"


class FakeClient:
    def __init__(self, *, matching: bool, phase="PHASE_TYPE_COMPLETE"):
        self.matching = matching
        self.phase = phase
        self.list_calls = 0

    def list_page(self, *, parent_id, page_token='', page_size=500):
        self.list_calls += 1
        if page_token:
            items = []
        elif parent_id == "":
            items = [{"id": "dir-id", "name": "dir", "kind": "drive#folder"}]
        else:
            items = [
                {
                    "id": "file-id",
                    "name": "a.rar",
                    "size": str(123 if self.matching else 999),
                    "hash": GCID,
                    "kind": "drive#file", "phase": self.phase, "trashed": False}
            ]
        return Decision(
            DecisionKind.COMPLETED,
            payload={"items": items, "next_page_token": ""},
        )


def config(tmp_path, *, mode="with_checksum"):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "one.json").write_text(
        json.dumps({"files": [{"path": "/dir/a.rar", "gcid": GCID, "size": "123"}]}),
        encoding="utf-8",
    )
    return SimpleNamespace(
        input_dir=input_dir,
        target_parent_id="",
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "output",
        workers=2,
        json_parallelism=1,
        max_retries=0,
        compare_mode=mode,
    )


@pytest.mark.parametrize("matching,phase,mode,delta", [
    (True, "PHASE_TYPE_COMPLETE", "with_checksum", False),
    (False, "PHASE_TYPE_COMPLETE", "with_checksum", True),
    (False, "PHASE_TYPE_COMPLETE", "exist_only", False),
    (True, "PHASE_TYPE_PENDING", "exist_only", True),
    (True, "PHASE_TYPE_PENDING", "with_checksum", True),
])
def test_batch_check_only_aligns_complete_files(tmp_path, matching, phase, mode, delta):
    cfg = config(tmp_path, mode=mode)
    assert run_batch_check(config=cfg, client=FakeClient(matching=matching, phase=phase)) == 0
    output = cfg.output_dir / "one.delta.export.json"
    assert output.exists() == delta
    if delta:
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["files"] == [{"path": "/dir/a.rar", "size": "123", "gcid": GCID}]


def test_switching_compare_mode_reuses_state_and_removes_stale_delta(tmp_path):
    cfg = config(tmp_path)
    client = FakeClient(matching=False)
    assert run_batch_check(config=cfg, client=client) == 0
    output = cfg.output_dir / "one.delta.export.json"
    assert output.exists()
    calls_after_scan = client.list_calls

    cfg.compare_mode = "exist_only"
    assert run_batch_check(config=cfg, client=client) == 0
    assert not output.exists()
    assert client.list_calls == calls_after_scan
