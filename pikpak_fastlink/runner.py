from __future__ import annotations

import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from pikpak_fastlink.api import Decision, DecisionKind
from pikpak_fastlink.models import normalize_gcid, normalize_remote_id, parse_size


class CredentialFatalError(RuntimeError):
    pass


def safe_print(message: str) -> None:
    try:
        print(message)
    except Exception:
        pass


def compute_backoff(attempt: int) -> float:
    return min(30.0, float(2**attempt)) + random.uniform(0.0, 0.5)


def list_remote_children(*, client, parent_id: str, max_retries: int) -> list[dict]:
    """Return a complete directory or fail before publishing a partial listing."""
    items: list[dict] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_tokens: set[str] = set()
    page_token = ""
    while True:
        decision = call_with_retries(
            lambda: client.list_page(parent_id=parent_id, page_token=page_token),
            max_retries=max_retries,
        )
        if decision.kind is DecisionKind.CREDENTIAL_FATAL:
            raise CredentialFatalError(decision.error or "credential failure")
        if decision.kind is not DecisionKind.COMPLETED:
            raise RuntimeError(decision.error or "directory listing failed")
        payload = decision.payload
        if not isinstance(payload, dict):
            raise RuntimeError("invalid directory listing payload")
        page_items = payload.get("items")
        next_token = payload.get("next_page_token")
        if not isinstance(page_items, list) or not isinstance(next_token, str):
            raise RuntimeError("invalid directory listing page or token")
        for item in page_items:
            if not isinstance(item, dict):
                raise RuntimeError("invalid remote file-list item")
            try:
                file_id = normalize_remote_id(item.get("id"))
            except ValueError as exc:
                raise RuntimeError(
                    "invalid remote file-list item: missing name or id"
                ) from exc
            if file_id in seen_ids:
                raise RuntimeError(
                    f"duplicate remote id: {file_id}; rerun to rescan"
                )
            seen_ids.add(file_id)
            if item.get("trashed", False) is not False:
                if item.get("trashed") is not True:
                    raise RuntimeError("invalid remote trashed flag")
                continue
            name = item.get("name")
            if (
                not isinstance(name, str) or not name or name in {".", ".."}
                or "/" in name or "\\" in name
                or item.get("kind") not in {"drive#file", "drive#folder"}
            ):
                raise RuntimeError("invalid remote file-list name or kind")
            if "parent_id" in item and item["parent_id"] != parent_id:
                raise RuntimeError("remote file-list parent mismatch")
            if name in seen_names:
                raise RuntimeError(f"duplicate remote child name: {name}")
            seen_names.add(name)
            items.append({**item, "id": file_id})
        if not next_token:
            return items
        if next_token in seen_tokens:
            raise RuntimeError("repeated directory listing token; rerun to rescan")
        seen_tokens.add(next_token)
        page_token = next_token


class RemoteTreeCoordinator:
    def __init__(self, client) -> None:
        self.client = client
        self._cache_lock = threading.Lock()
        self._parent_locks: dict[str, threading.RLock] = {}
        self._children: dict[str, dict[str, dict]] = {}

    def _parent_lock(self, parent_id: str) -> threading.RLock:
        with self._cache_lock:
            return self._parent_locks.setdefault(parent_id, threading.RLock())

    def _list_children(
        self, *, parent_id: str, max_retries: int, refresh: bool = False
    ) -> dict[str, dict]:
        with self._cache_lock:
            if not refresh and parent_id in self._children:
                return self._children[parent_id]
        with self._parent_lock(parent_id):
            with self._cache_lock:
                if not refresh and parent_id in self._children:
                    return self._children[parent_id]
            items = list_remote_children(
                client=self.client, parent_id=parent_id, max_retries=max_retries
            )
            children = {}
            for item in items:
                name = item["name"]
                children[name] = item
            with self._cache_lock:
                self._children[parent_id] = children
            return children

    def ensure_directory(self, *, parent_id: str, name: str, max_retries: int) -> str:
        # Serialize shared batch folders, including uncertain creation reconciliation.
        with self._parent_lock(parent_id):
            existing = self.find_directory(
                parent_id=parent_id, name=name, max_retries=max_retries
            )
            if existing is not None:
                return existing
            uncertain = False

            def create():
                nonlocal uncertain
                if uncertain:
                    existing = self.find_directory(
                        parent_id=parent_id, name=name, max_retries=max_retries,
                        refresh=True,
                    )
                    if existing is not None:
                        return Decision(DecisionKind.DIRECTORY_CREATED, file_id=existing)
                decision = self.client.mkdir(parent_id=parent_id, name=name)
                if decision.kind is DecisionKind.RETRYABLE:
                    uncertain = True
                    existing = self.find_directory(
                        parent_id=parent_id, name=name, max_retries=max_retries,
                        refresh=True,
                    )
                    if existing is not None:
                        return Decision(DecisionKind.DIRECTORY_CREATED, file_id=existing)
                return decision

            decision = call_with_retries(create, max_retries=max_retries)
            if decision.kind is DecisionKind.CREDENTIAL_FATAL:
                raise CredentialFatalError(decision.error or "credential failure")
            if decision.kind is not DecisionKind.DIRECTORY_CREATED:
                raise RuntimeError(decision.error or f"failed to create directory: {name}")
            file_id = str(decision.file_id)
            with self._cache_lock:
                self._children[parent_id][name] = {
                    "id": file_id, "name": name, "kind": "drive#folder"
                }
            return file_id

    def classify_file(
        self,
        *,
        parent_id: str,
        record,
        max_retries: int,
        refresh: bool = False,
    ) -> str:
        children = self._list_children(
            parent_id=parent_id, max_retries=max_retries, refresh=refresh
        )
        existing = children.get(record.file_name)
        if existing is None:
            return "missing"
        if (
            existing.get("kind") != "drive#file"
            or existing.get("phase") != "PHASE_TYPE_COMPLETE"
        ):
            return "collision"
        try:
            same = (
                normalize_gcid(existing.get("hash")) == record.gcid
                and parse_size(existing.get("size")) == record.size
            )
        except (TypeError, ValueError):
            same = False
        return "completed" if same else "collision"

    def remember_file(self, *, parent_id: str, record) -> None:
        with self._cache_lock:
            self._children.setdefault(parent_id, {})[record.file_name] = {
                "name": record.file_name,
                "size": record.size,
                "hash": record.gcid,
                "kind": "drive#file",
                "phase": "PHASE_TYPE_COMPLETE",
            }

    def get_child(
        self, *, parent_id: str, name: str, max_retries: int, refresh: bool = False
    ) -> dict | None:
        children = self._list_children(
            parent_id=parent_id, max_retries=max_retries, refresh=refresh
        )
        return children.get(name)

    def find_directory(
        self, *, parent_id: str, name: str, max_retries: int, refresh: bool = False
    ) -> str | None:
        children = self._list_children(
            parent_id=parent_id, max_retries=max_retries, refresh=refresh
        )
        item = children.get(name)
        if item is None:
            return None
        if item.get("kind") != "drive#folder":
            raise RuntimeError(f"file-directory collision: {name}")
        return str(item["id"])


def create_remote_directories(
    *, state, coordinator: RemoteTreeCoordinator, max_retries: int
) -> None:
    folder_map = state.folder_map()
    for folder_key, parent_key in state.pending_folder_rows():
        parent_id = folder_map[parent_key]
        remote_id = coordinator.ensure_directory(
            parent_id=parent_id,
            name=folder_key.rsplit("/", 1)[-1],
            max_retries=max_retries,
        )
        folder_map[folder_key] = remote_id
        state.record_folder_created(folder_key, remote_id)
        safe_print(f"Directory progress: created={folder_key}")


def run_import_files(
    *,
    state,
    client,
    coordinator: RemoteTreeCoordinator,
    max_retries: int,
    flush_every: int,
) -> dict[str, int | bool]:
    folder_map = state.folder_map()
    stop = threading.Event()
    outcomes: list[dict] = []
    processed = 0
    credential_fatal = False

    def process(record) -> dict:
        try:
            parent_id = folder_map[record.relative_parent_dir]
        except KeyError as exc:
            raise RuntimeError(
                f"missing remote folder mapping: {record.relative_parent_dir}"
            ) from exc
        preflight = coordinator.classify_file(
            parent_id=parent_id,
            record=record,
            max_retries=max_retries,
        )
        if preflight == "completed":
            return {"status": "completed", "record_key": record.key, "retries": 0}
        if preflight == "collision":
            return {
                "status": "failed",
                "record_key": record.key,
                "error": f"target path collision: {record.path}",
                "retries": 0,
            }

        def reconcile_uncertain_request():
            reconciled = coordinator.classify_file(
                parent_id=parent_id,
                record=record,
                max_retries=max_retries,
                refresh=True,
            )
            if reconciled == "completed":
                return {
                    "status": "completed",
                    "record_key": record.key,
                    "retries": attempts,
                }
            if reconciled == "collision":
                return {
                    "status": "failed",
                    "record_key": record.key,
                    "error": f"target path collision after uncertain request: {record.path}",
                    "retries": attempts,
                }
            return None

        attempts = 0
        while not stop.is_set():
            decision = client.instant_transfer(record=record, parent_id=parent_id)
            if decision.kind is DecisionKind.COMPLETED:
                coordinator.remember_file(parent_id=parent_id, record=record)
                return {
                    "status": "completed",
                    "record_key": record.key,
                    "retries": attempts,
                }
            if decision.kind is DecisionKind.NOT_REUSABLE:
                safe_print(f"NOT_REUSABLE: {record.path}: {decision.error or 'instant reuse unavailable'}")
                return {
                    "status": "not_reusable",
                    "record_key": record.key,
                    "error": decision.error,
                    "retries": attempts,
                }
            if decision.kind is DecisionKind.CREDENTIAL_FATAL:
                stop.set()
                return {
                    "status": "deferred",
                    "record_key": record.key,
                    "credential_fatal": True,
                }
            if decision.kind is DecisionKind.RETRYABLE:
                terminal = reconcile_uncertain_request()
                if terminal is not None:
                    return terminal
                if attempts < max_retries:
                    attempts += 1
                    if stop.wait(compute_backoff(attempts)):
                        break
                    terminal = reconcile_uncertain_request()
                    if terminal is not None:
                        return terminal
                    continue
            return {
                "status": "failed",
                "record_key": record.key,
                "error": decision.error or "rapid transfer failed",
                "retries": attempts,
            }
        return {"status": "deferred", "record_key": record.key}

    records = state.iter_pending_records()
    active: dict[Future, object] = {}
    try:
        with ThreadPoolExecutor(max_workers=state.workers) as executor:
            exhausted = False
            while active or not exhausted:
                while (
                    not exhausted
                    and not stop.is_set()
                    and len(active) < state.workers * 2
                ):
                    try:
                        record = next(records)
                    except StopIteration:
                        exhausted = True
                        break
                    active[executor.submit(process, record)] = record
                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    active.pop(future)
                    outcome = future.result()
                    if outcome.get("credential_fatal"):
                        if not credential_fatal:
                            safe_print("Credentials rejected; copy fresh browser credentials into .env and rerun.")
                        credential_fatal = True
                    if outcome["status"] == "deferred":
                        continue
                    outcomes.append(outcome)
                    processed += 1
                    if len(outcomes) >= flush_every:
                        state.flush_outcomes(outcomes)
                        outcomes = []
                        safe_print(f"State flush: terminal_outcomes={processed}")
    finally:
        state.flush_outcomes(outcomes)
    return {"processed": processed, "credential_fatal": credential_fatal}


def call_with_retries(call, *, max_retries: int) -> Decision:
    attempts = 0
    while True:
        decision = call()
        if decision.kind is not DecisionKind.RETRYABLE or attempts >= max_retries:
            return decision
        attempts += 1
        time.sleep(compute_backoff(attempts))
