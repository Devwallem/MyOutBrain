from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
import json
import msvcrt
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from myoutbrain.core_types import IntegrityError, WriterLocked


def atomic_write(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _transaction_target(root: Path, relative_path: str) -> Path:
    root_resolved = root.resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root_resolved):
        raise IntegrityError(f"transaction target escapes the library: {relative_path}")
    return target


def _read_transaction_manifest(transaction_path: Path) -> list[dict[str, object]]:
    manifest_path = transaction_path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = manifest["entries"]
        if not isinstance(entries, list):
            raise TypeError("transaction entries are not a list")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("target"), str)
                or not isinstance(entry.get("existed"), bool)
                or not isinstance(entry.get("index"), int)
            ):
                raise TypeError("transaction entry is invalid")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise IntegrityError(f"invalid transaction manifest: {manifest_path}") from error
    return entries


def _recover_transaction(root: Path, transaction_path: Path) -> None:
    entries = _read_transaction_manifest(transaction_path)
    committed = (transaction_path / "committed").is_file()
    for entry in entries:
        index = entry["index"]
        target_value = entry["target"]
        existed = entry["existed"]
        if not isinstance(index, int) or not isinstance(target_value, str) or not isinstance(existed, bool):
            raise IntegrityError(f"invalid transaction entry in: {transaction_path}")
        target = _transaction_target(root, target_value)
        if committed:
            replacement_path = transaction_path / "after" / str(index)
            atomic_write(target, replacement_path.read_bytes())
        elif existed:
            previous_path = transaction_path / "before" / str(index)
            atomic_write(target, previous_path.read_bytes())
        else:
            target.unlink(missing_ok=True)
    shutil.rmtree(transaction_path)


def recover_transactions(root: Path) -> None:
    transactions_root = root / "store" / "transactions"
    if not transactions_root.is_dir():
        return
    for transaction_path in sorted(transactions_root.iterdir()):
        if not transaction_path.is_dir():
            raise IntegrityError(f"unexpected transaction entry: {transaction_path}")
        if not (transaction_path / "manifest.json").is_file():
            shutil.rmtree(transaction_path)
            continue
        _recover_transaction(root, transaction_path)


def atomic_commit(
    root: Path,
    changes: Sequence[tuple[Path, bytes]],
    *,
    fault_injections: dict[int, str] | None = None,
) -> None:
    transactions_root = root / "store" / "transactions"
    transactions_root.mkdir(parents=True, exist_ok=True)
    transaction_path = transactions_root / f"txn_{uuid.uuid4().hex}"
    (transaction_path / "before").mkdir(parents=True)
    (transaction_path / "after").mkdir()
    entries: list[dict[str, object]] = []
    for index, (path, content) in enumerate(changes):
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        if existed:
            atomic_write(transaction_path / "before" / str(index), path.read_bytes())
        atomic_write(transaction_path / "after" / str(index), content)
        entries.append(
            {
                "index": index,
                "target": path.resolve().relative_to(root.resolve()).as_posix(),
                "existed": existed,
            }
        )
    atomic_write(transaction_path / "manifest.json", json_document({"entries": entries}))

    try:
        for index, (path, content) in enumerate(changes):
            atomic_write(path, content)
            injected_fault = (
                fault_injections.get(index) if fault_injections is not None else None
            )
            if (
                injected_fault is not None
                and os.environ.get("MYOUTBRAIN_FAULT_INJECTION") == injected_fault
            ):
                os._exit(86)
        atomic_write(transaction_path / "committed", b"committed\n")
    except BaseException:
        _recover_transaction(root, transaction_path)
        raise
    shutil.rmtree(transaction_path)


@contextmanager
def writer_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".myoutbrain.lock"
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        if os.fstat(lock_descriptor).st_size == 0:
            os.write(lock_descriptor, b" ")
        os.lseek(lock_descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(lock_descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise WriterLocked from error
        os.ftruncate(lock_descriptor, 0)
        os.lseek(lock_descriptor, 0, os.SEEK_SET)
        os.write(lock_descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        try:
            os.lseek(lock_descriptor, 0, os.SEEK_SET)
            msvcrt.locking(lock_descriptor, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(lock_descriptor)


def hold_writer_lock_for_acceptance_test() -> None:
    if os.environ.get("MYOUTBRAIN_FAULT_INJECTION") != "hold-writer-lock":
        return
    ready_file = os.environ.get("MYOUTBRAIN_LOCK_READY_FILE")
    if ready_file is not None:
        Path(ready_file).write_text(str(os.getpid()), encoding="ascii")
    duration = float(os.environ.get("MYOUTBRAIN_HOLD_SECONDS", "1"))
    time.sleep(duration)


def event_journal_change(
    root: Path,
    *events: Mapping[str, object],
) -> tuple[Path, bytes]:
    journal_path = root / "store" / "journal" / "events.jsonl"
    try:
        existing_journal = journal_path.read_bytes() if journal_path.exists() else b""
    except OSError as error:
        raise IntegrityError(f"cannot read event journal: {journal_path}") from error
    event_lines = b"".join(
        json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
        for event in events
    )
    return journal_path, existing_journal + event_lines


def json_document(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
