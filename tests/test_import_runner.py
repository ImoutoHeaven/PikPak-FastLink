from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pikpak_fastlink.api import Decision, DecisionKind
from pikpak_fastlink.import_state import MISMATCH_ERROR, open_or_plan_import_state
from pikpak_fastlink.models import inspect_export_scope
from pikpak_fastlink.runner import (
    RemoteTreeCoordinator,
    create_remote_directories,
    run_import_files,
)


GCID = "58A3F526EE3C569FEECCDFD66DAC9631614E7578"


def write_export(path: Path):
    path.write_text(
        json.dumps({"files": [{"path": "/dir/a.rar", "gcid": GCID, "size": "123"}]}),
        encoding="utf-8",
    )


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
        self.children.setdefault(parent_id, {})[name] = {
            "name": name,
            "id": file_id,
            "kind": "drive#folder",
        }
        self.children.setdefault(file_id, {})
        return Decision(DecisionKind.DIRECTORY_CREATED, file_id=file_id)

    def instant_transfer(self, *, record, parent_id):
        self.transfer_calls += 1
        self.children.setdefault(parent_id, {})[record.file_name] = {
            "name": record.file_name,
            "id": "file-a",
            "size": str(record.size),
            "hash": record.gcid,
            "kind": "drive#file", "phase": "PHASE_TYPE_COMPLETE", "trashed": False}
        return Decision(DecisionKind.COMPLETED)


class UncertainSuccessClient(FakeClient):
    def instant_transfer(self, *, record, parent_id):
        self.transfer_calls += 1
        self.children.setdefault(parent_id, {})[record.file_name] = {
            "name": record.file_name,
            "id": "file-a",
            "size": str(record.size),
            "hash": record.gcid,
            "kind": "drive#file", "phase": "PHASE_TYPE_COMPLETE", "trashed": False}
        return Decision(DecisionKind.RETRYABLE, error="response lost")


def test_import_plans_runs_and_resumes_without_duplicate(tmp_path):
    export = tmp_path / "source.json"
    state_path = tmp_path / "state.sqlite3"
    write_export(export)
    scope = inspect_export_scope(export)
    state = open_or_plan_import_state(
        state_path=state_path,
        source_path=export,
        source_sha256=scope.source_sha256,
        target_parent_id="",
    )
    state.workers = 2
    client = FakeClient()
    coordinator = RemoteTreeCoordinator(client)

    create_remote_directories(state=state, coordinator=coordinator, max_retries=0)
    summary = run_import_files(
        state=state,
        client=client,
        coordinator=coordinator,
        max_retries=0,
        flush_every=1,
    )
    assert summary == {"processed": 1, "credential_fatal": False}
    assert state.stats == {"total": 1, "completed": 1, "not_reusable": 0, "failed": 0}
    assert client.transfer_calls == 1
    state.close()

    resumed = open_or_plan_import_state(
        state_path=state_path,
        source_path=export,
        source_sha256=scope.source_sha256,
        target_parent_id="",
    )
    resumed.workers = 2
    create_remote_directories(state=resumed, coordinator=coordinator, max_retries=0)
    second = run_import_files(
        state=resumed,
        client=client,
        coordinator=coordinator,
        max_retries=0,
        flush_every=1,
    )
    assert second["processed"] == 0
    assert client.transfer_calls == 1
    resumed.close()


def test_existing_same_file_is_completed_without_transfer(tmp_path):
    export = tmp_path / "source.json"
    write_export(export)
    scope = inspect_export_scope(export)
    state = open_or_plan_import_state(
        state_path=tmp_path / "state.sqlite3",
        source_path=export,
        source_sha256=scope.source_sha256,
        target_parent_id="",
    )
    client = FakeClient()
    client.children[""]["dir"] = {
        "name": "dir",
        "id": "folder-dir",
        "kind": "drive#folder",
    }
    client.children["folder-dir"] = {
        "a.rar": {
            "name": "a.rar",
            "id": "file-a",
            "size": "123",
            "hash": GCID,
            "kind": "drive#file", "phase": "PHASE_TYPE_COMPLETE", "trashed": False}
    }
    coordinator = RemoteTreeCoordinator(client)
    create_remote_directories(state=state, coordinator=coordinator, max_retries=0)
    run_import_files(
        state=state,
        client=client,
        coordinator=coordinator,
        max_retries=0,
        flush_every=10,
    )
    assert client.transfer_calls == 0
    assert state.stats["completed"] == 1
    state.close()


def test_existing_different_file_is_collision(tmp_path):
    export = tmp_path / "source.json"
    write_export(export)
    scope = inspect_export_scope(export)
    state = open_or_plan_import_state(
        state_path=tmp_path / "state.sqlite3",
        source_path=export,
        source_sha256=scope.source_sha256,
        target_parent_id="",
    )
    client = FakeClient()
    client.children[""]["dir"] = {
        "name": "dir",
        "id": "folder-dir",
        "kind": "drive#folder",
    }
    client.children["folder-dir"] = {
        "a.rar": {
            "name": "a.rar",
            "id": "other",
            "size": "999",
            "hash": GCID,
            "kind": "drive#file", "phase": "PHASE_TYPE_COMPLETE", "trashed": False}
    }
    coordinator = RemoteTreeCoordinator(client)
    create_remote_directories(state=state, coordinator=coordinator, max_retries=0)
    run_import_files(
        state=state,
        client=client,
        coordinator=coordinator,
        max_retries=0,
        flush_every=10,
    )
    assert client.transfer_calls == 0
    assert state.stats["failed"] == 1
    state.close()


def test_uncertain_directory_creation_reconciles_before_retry(tmp_path, monkeypatch):
    export = tmp_path / "source.json"
    write_export(export)
    scope = inspect_export_scope(export)
    state = open_or_plan_import_state(
        state_path=tmp_path / "state.sqlite3", source_path=export,
        source_sha256=scope.source_sha256, target_parent_id="",
    )

    class UncertainDirectoryClient(FakeClient):
        mkdir_calls = 0

        def mkdir(self, *, parent_id, name):
            self.mkdir_calls += 1
            super().mkdir(parent_id=parent_id, name=name)
            return Decision(DecisionKind.RETRYABLE, error="folder response lost")

    client = UncertainDirectoryClient()
    coordinator = RemoteTreeCoordinator(client)
    monkeypatch.setattr("pikpak_fastlink.runner.compute_backoff", lambda attempt: 0)
    create_remote_directories(state=state, coordinator=coordinator, max_retries=1)
    run_import_files(state=state, client=client, coordinator=coordinator,
                     max_retries=1, flush_every=1)
    assert client.mkdir_calls == 1
    assert state.folder_map()["dir"] == "folder-dir"
    assert client.children["folder-dir"]["a.rar"]["hash"] == GCID
    assert state.stats["completed"] == 1
    state.close()


def test_state_scope_mismatch_is_rejected(tmp_path):
    export = tmp_path / "source.json"
    write_export(export)
    scope = inspect_export_scope(export)
    state_path = tmp_path / "state.sqlite3"
    state = open_or_plan_import_state(
        state_path=state_path,
        source_path=export,
        source_sha256=scope.source_sha256,
        target_parent_id="",
    )
    state.close()
    with pytest.raises(ValueError, match=MISMATCH_ERROR):
        open_or_plan_import_state(
            state_path=state_path,
            source_path=export,
            source_sha256=hashlib.sha256(b"other").hexdigest(),
            target_parent_id="",
        )


def test_uncertain_post_is_reconciled_before_retry(tmp_path):
    export = tmp_path / "source.json"
    export.write_text(
        json.dumps({"files": [{"path": "/a.rar", "gcid": GCID, "size": "123"}]}),
        encoding="utf-8",
    )
    scope = inspect_export_scope(export)
    state = open_or_plan_import_state(
        state_path=tmp_path / "state.sqlite3",
        source_path=export,
        source_sha256=scope.source_sha256,
        target_parent_id="",
    )
    client = UncertainSuccessClient()
    coordinator = RemoteTreeCoordinator(client)
    run_import_files(
        state=state,
        client=client,
        coordinator=coordinator,
        max_retries=2,
        flush_every=10,
    )
    assert client.transfer_calls == 1
    assert state.stats["completed"] == 1
    state.close()


def test_malformed_remote_item_fails_closed():
    class MalformedClient(FakeClient):
        def list_page(self, *, parent_id, page_token='', page_size=500):
            return Decision(
                DecisionKind.COMPLETED,
                payload={"items": [{"name": "a.rar", "kind": "drive#file", "phase": "PHASE_TYPE_COMPLETE", "trashed": False}], "next_page_token": ""},
            )

    coordinator = RemoteTreeCoordinator(MalformedClient())
    with pytest.raises(RuntimeError, match="invalid remote"):
        coordinator.get_child(parent_id="", name="a.rar", max_retries=0)


def test_empty_remote_id_fails_closed():
    class EmptyIdClient(FakeClient):
        def list_page(self, *, parent_id, page_token='', page_size=500):
            return Decision(
                DecisionKind.COMPLETED,
                payload={
                    "items": [{"name": "dir", "id": "", "kind": "drive#folder"}],
                    "next_page_token": "",
                },
            )

    coordinator = RemoteTreeCoordinator(EmptyIdClient())
    with pytest.raises(RuntimeError, match="invalid remote"):
        coordinator.find_directory(parent_id="", name="dir", max_retries=0)


def test_parallel_jobs_create_a_shared_directory_once():
    entered = threading.Event()
    duplicate = threading.Event()
    release = threading.Event()

    class BlockingDirectoryClient(FakeClient):
        mkdir_calls = 0
        counter_lock = threading.Lock()

        def mkdir(self, *, parent_id, name):
            with self.counter_lock:
                self.mkdir_calls += 1
                first = self.mkdir_calls == 1
            if first:
                entered.set()
                if not release.wait(timeout=2):
                    raise AssertionError("test did not release directory creation")
            else:
                duplicate.set()
            return super().mkdir(parent_id=parent_id, name=name)

    client = BlockingDirectoryClient()
    coordinator = RemoteTreeCoordinator(client)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(coordinator.ensure_directory,
                                parent_id="", name="shared", max_retries=0)
        assert entered.wait(timeout=2)
        second = executor.submit(coordinator.ensure_directory,
                                 parent_id="", name="shared", max_retries=0)
        duplicate.wait(timeout=0.2)
        release.set()
        assert first.result() == second.result() == "folder-shared"
    assert client.mkdir_calls == 1


def test_different_parent_list_requests_can_run_concurrently():
    barrier = threading.Barrier(2)

    class ConcurrentClient(FakeClient):
        def list_page(self, *, parent_id, page_token='', page_size=500):
            barrier.wait(timeout=2)
            return Decision(DecisionKind.COMPLETED, payload={"items": [], "next_page_token": ""})

    coordinator = RemoteTreeCoordinator(ConcurrentClient())
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                coordinator.get_child,
                parent_id=parent,
                name="missing",
                max_retries=0,
            )
            for parent in ("one", "two")
        ]
        assert [future.result() for future in futures] == [None, None]


def test_eventually_consistent_reconciliation_checks_again_before_repost(
    tmp_path, monkeypatch
):
    export = tmp_path / "source.json"
    export.write_text(
        json.dumps({"files": [{"path": "/a.rar", "gcid": GCID, "size": "123"}]}),
        encoding="utf-8",
    )
    scope = inspect_export_scope(export)
    state = open_or_plan_import_state(
        state_path=tmp_path / "state.sqlite3",
        source_path=export,
        source_sha256=scope.source_sha256,
        target_parent_id="",
    )

    class EventuallyVisibleClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.posted = False
            self.post_list_calls = 0

        def list_page(self, *, parent_id, page_token='', page_size=500):
            if self.posted and not page_token:
                self.post_list_calls += 1
                if self.post_list_calls >= 2:
                    self.children[""]["a.rar"] = {
                        "name": "a.rar",
                        "id": "file-a",
                        "size": "123",
                        "hash": GCID,
                        "kind": "drive#file", "phase": "PHASE_TYPE_COMPLETE", "trashed": False}
            return super().list_page(
                parent_id=parent_id, page_token=page_token, page_size=page_size
            )

        def instant_transfer(self, *, record, parent_id):
            self.transfer_calls += 1
            self.posted = True
            return Decision(DecisionKind.RETRYABLE, error="response lost")

    client = EventuallyVisibleClient()
    monkeypatch.setattr("pikpak_fastlink.runner.compute_backoff", lambda attempt: 0)
    run_import_files(
        state=state,
        client=client,
        coordinator=RemoteTreeCoordinator(client),
        max_retries=2,
        flush_every=10,
    )
    assert client.transfer_calls == 1
    assert state.stats["completed"] == 1
    state.close()


def test_duplicate_paths_are_rejected_by_sqlite_planner(tmp_path):
    export = tmp_path / "duplicate.json"
    export.write_text(
        json.dumps(
            {
                "files": [
                    {"path": "/a.bin", "gcid": GCID, "size": "1"},
                    {"path": "a.bin", "gcid": GCID, "size": "1"},
                ]
            }
        ),
        encoding="utf-8",
    )
    scope = inspect_export_scope(export)
    with pytest.raises(ValueError, match="duplicate normalized path"):
        open_or_plan_import_state(
            state_path=tmp_path / "state.sqlite3",
            source_path=export,
            source_sha256=scope.source_sha256,
            target_parent_id="",
        )
