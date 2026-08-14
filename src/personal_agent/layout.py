from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LAYOUT_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def generation_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:12]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def sqlite_online_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True, timeout=30)
    target_connection = sqlite3.connect(temporary, timeout=30)
    try:
        source_connection.backup(target_connection)
        target_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    os.replace(temporary, destination)


class DirectoryLock:
    """Cross-platform exclusive OS lock released automatically on process exit."""

    def __init__(self, path: Path, *, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        self.acquired = False
        self._stream: Any | None = None

    def __enter__(self) -> DirectoryLock:
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError(f"Lock path must not be a symlink: {self.path}")
        self._stream = self.path.open("a+b")
        self._stream.seek(0, os.SEEK_END)
        if self._stream.tell() == 0:
            self._stream.write(b"\0")
            self._stream.flush()
        while True:
            try:
                self._lock_nonblocking()
                self.acquired = True
                return self
            except OSError as error:
                if time.monotonic() >= deadline:
                    self._stream.close()
                    self._stream = None
                    raise TimeoutError(f"Timed out waiting for lock: {self.path}") from error
                time.sleep(0.1)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.acquired:
            self._unlock()
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        self.acquired = False

    def _lock_nonblocking(self) -> None:
        if self._stream is None:
            raise RuntimeError("Lock stream is not open.")
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        if self._stream is None:
            return
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)


class StateLayout:
    def __init__(self, home: Path) -> None:
        self.home = home.expanduser().absolute()
        self.control_dir = self.home / "control"
        self.state_dir = self.home / "state"
        self.generations_dir = self.state_dir / "generations"
        self.user_dir = self.home / "user"
        self.cache_dir = self.home / "cache"
        self.backups_dir = self.home / "backups"
        self.runtime_dir = self.home / "runtime"
        self.active_pointer = self.control_dir / "active.json"
        self.install_manifest = self.control_dir / "install.json"
        self.upgrade_lock = self.runtime_dir / "upgrade.lock"
        self.legacy_db = self.home / "state.db"
        self.legacy_package = self.home / "package"
        self.legacy_memory = self.home / "memory"

    def ensure_directories(self) -> None:
        if self.home.exists() and path_is_link_or_reparse_point(self.home):
            raise ValueError(
                f"PERSONAL_AGENT_HOME must not be a link or reparse point: {self.home}"
            )
        for path in (
            self.control_dir,
            self.generations_dir,
            self.user_dir,
            self.cache_dir,
            self.backups_dir,
            self.runtime_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                path.chmod(0o700)

    @contextmanager
    def locked(self, *, timeout: float = 30.0) -> Iterator[None]:
        with DirectoryLock(self.upgrade_lock, timeout=timeout):
            yield

    def _read_active(self) -> dict[str, Any] | None:
        if not self.active_pointer.exists():
            return None
        parsed = json.loads(self.active_pointer.read_text(encoding="utf-8-sig"))
        if parsed.get("format") != "personal-agent.active/v1":
            raise ValueError("Unsupported active pointer format.")
        return parsed

    def active_generation_id(self) -> str | None:
        active = self._read_active()
        return str(active["generation_id"]) if active else None

    def generation_dir(self, identifier: str) -> Path:
        if not re_safe_identifier(identifier):
            raise ValueError(f"Invalid generation identifier: {identifier}")
        return self.generations_dir / identifier

    def active_db(self) -> Path:
        identifier = self.active_generation_id()
        if not identifier:
            raise RuntimeError("Personal Agent state is not initialized.")
        directory = self.generation_dir(identifier)
        manifest_path = directory / "generation.json"
        if not manifest_path.is_file():
            raise RuntimeError(f"Active generation manifest is missing: {identifier}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if (
            manifest.get("format") != "personal-agent.generation/v1"
            or manifest.get("generation_id") != identifier
        ):
            raise RuntimeError(f"Active generation manifest is invalid: {identifier}")
        database = directory / "state.db"
        if not database.is_file():
            raise RuntimeError(f"Active database is missing: {database}")
        return database

    def recover_active_generation(self, *, max_schema_version: int | None = None) -> str | None:
        candidates: list[tuple[int, str, str]] = []
        for directory in self.generations_dir.iterdir():
            if not directory.is_dir():
                continue
            manifest_path = directory / "generation.json"
            database = directory / "state.db"
            if not manifest_path.is_file() or not database.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                if manifest.get("format") != "personal-agent.generation/v1":
                    continue
                if manifest.get("generation_id") != directory.name:
                    continue
                if manifest.get("status") != "ready":
                    continue
                connection = sqlite3.connect(database, timeout=5)
                try:
                    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        continue
                    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if max_schema_version is not None and schema_version > max_schema_version:
                        continue
                finally:
                    connection.close()
                candidates.append(
                    (
                        int(manifest.get("sequence", 0)),
                        str(manifest.get("verified_at") or ""),
                        directory.name,
                    )
                )
            except (OSError, json.JSONDecodeError, sqlite3.Error):
                continue
        if not candidates:
            return None
        candidates.sort()
        identifier = candidates[-1][2]
        self.switch(identifier, reason="active-pointer-recovery")
        return identifier

    def initialize(self, *, max_schema_version: int | None = None) -> Path:
        self.ensure_directories()
        existing = self.active_generation_id()
        if existing:
            return self.active_db()
        with self.locked():
            existing = self.active_generation_id()
            if existing:
                return self.active_db()
            recovered = self.recover_active_generation(max_schema_version=max_schema_version)
            if recovered:
                return self.active_db()
            identifier = generation_id()
            destination = self.generation_dir(identifier)
            destination.mkdir(parents=False)
            database = destination / "state.db"
            if self.legacy_db.exists():
                sqlite_online_backup(self.legacy_db, database)
                source = "legacy-state.db"
            else:
                sqlite3.connect(database).close()
                source = "fresh"
            atomic_write_json(
                destination / "generation.json",
                {
                    "format": "personal-agent.generation/v1",
                    "generation_id": identifier,
                    "layout_version": LAYOUT_VERSION,
                    "created_at": utc_now(),
                    "source": source,
                    "base_database_sha256": sha256_file(database),
                    "status": "staging",
                },
            )
            self.switch(identifier, reason="initialization")
            return database

    def switch(self, identifier: str, *, reason: str) -> None:
        directory = self.generation_dir(identifier)
        if not (directory / "generation.json").is_file():
            raise ValueError(f"Generation manifest is missing: {identifier}")
        if not (directory / "state.db").is_file():
            raise ValueError(f"Generation database is missing: {identifier}")
        previous = self.active_generation_id()
        atomic_write_json(
            self.active_pointer,
            {
                "format": "personal-agent.active/v1",
                "layout_version": LAYOUT_VERSION,
                "generation_id": identifier,
                "previous_generation_id": previous,
                "reason": reason,
                "switched_at": utc_now(),
            },
        )

    def snapshot_active(self, *, label: str) -> Path:
        self.ensure_directories()
        identifier = self.active_generation_id()
        if not identifier:
            raise RuntimeError("No active generation to back up.")
        safe_label = re_safe_label(label)
        backup_id = (
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{safe_label}-{uuid.uuid4().hex[:8]}"
        )
        destination = self.backups_dir / backup_id
        destination.mkdir()
        database = destination / "state.db"
        sqlite_online_backup(self.active_db(), database)
        connection = sqlite3.connect(database, timeout=30)
        try:
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
        generation_manifest = json.loads(
            (self.generation_dir(identifier) / "generation.json").read_text(encoding="utf-8-sig")
        )
        manifest = {
            "format": "personal-agent.backup/v1",
            "backup_id": backup_id,
            "source_generation_id": identifier,
            "created_at": utc_now(),
            "layout_version": LAYOUT_VERSION,
            "app_version": generation_manifest.get("app_version"),
            "data_schema_version": schema_version,
            "database_sha256": sha256_file(database),
        }
        atomic_write_json(destination / "backup.json", manifest)
        return destination

    def clone_active_to_staging(self) -> tuple[str, Path]:
        source_identifier = self.active_generation_id()
        if not source_identifier:
            raise RuntimeError("No active generation.")
        identifier = generation_id()
        destination = self.generation_dir(identifier)
        destination.mkdir()
        database = destination / "state.db"
        sqlite_online_backup(self.active_db(), database)
        atomic_write_json(
            destination / "generation.json",
            {
                "format": "personal-agent.generation/v1",
                "generation_id": identifier,
                "layout_version": LAYOUT_VERSION,
                "created_at": utc_now(),
                "source": "clone",
                "parent_generation_id": source_identifier,
                "base_database_sha256": sha256_file(database),
                "status": "staging",
            },
        )
        return identifier, database

    def mark_generation_ready(
        self,
        identifier: str,
        *,
        app_version: str,
        data_schema_version: int,
    ) -> None:
        directory = self.generation_dir(identifier)
        database = directory / "state.db"
        manifest = json.loads((directory / "generation.json").read_text(encoding="utf-8-sig"))
        manifest.update(
            {
                "status": "ready",
                "sequence": self.next_generation_sequence(),
                "app_version": app_version,
                "data_schema_version": data_schema_version,
                "base_database_sha256": manifest.get("base_database_sha256", sha256_file(database)),
                "verified_at": utc_now(),
            }
        )
        atomic_write_json(directory / "generation.json", manifest)

    def next_generation_sequence(self) -> int:
        maximum = 0
        for directory in self.generations_dir.iterdir():
            manifest_path = directory / "generation.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                maximum = max(maximum, int(manifest.get("sequence", 0)))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return maximum + 1

    def restore_backup(
        self,
        backup_dir: Path,
        *,
        max_schema_version: int | None = None,
    ) -> str:
        backup_dir = backup_dir.expanduser().resolve()
        manifest_path = backup_dir / "backup.json"
        database_path = backup_dir / "state.db"
        if not manifest_path.is_file() or not database_path.is_file():
            raise ValueError("Backup is incomplete.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if manifest.get("format") != "personal-agent.backup/v1":
            raise ValueError("Unsupported backup format.")
        if sha256_file(database_path) != manifest.get("database_sha256"):
            raise ValueError("Backup checksum mismatch.")
        source_connection = sqlite3.connect(database_path, timeout=30)
        source_connection.execute("PRAGMA foreign_keys=ON")
        try:
            quick_check = source_connection.execute("PRAGMA quick_check").fetchone()[0]
            if quick_check != "ok":
                raise ValueError(f"Backup SQLite quick_check failed: {quick_check}")
            foreign_key_errors = list(source_connection.execute("PRAGMA foreign_key_check"))
            if foreign_key_errors:
                raise ValueError(f"Backup has {len(foreign_key_errors)} foreign-key errors.")
            actual_schema = int(source_connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            source_connection.close()
        declared_schema = manifest.get("data_schema_version")
        if declared_schema is not None and int(declared_schema) != actual_schema:
            raise ValueError("Backup manifest schema does not match the SQLite schema.")
        if max_schema_version is not None and actual_schema > max_schema_version:
            raise ValueError(
                f"Backup schema {actual_schema} is newer than supported {max_schema_version}."
            )
        with self.locked():
            identifier = generation_id()
            destination = self.generation_dir(identifier)
            destination.mkdir()
            sqlite_online_backup(database_path, destination / "state.db")
            restored_connection = sqlite3.connect(destination / "state.db", timeout=30)
            try:
                if restored_connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Restored SQLite database failed quick_check.")
            finally:
                restored_connection.close()
            atomic_write_json(
                destination / "generation.json",
                {
                    "format": "personal-agent.generation/v1",
                    "generation_id": identifier,
                    "layout_version": LAYOUT_VERSION,
                    "created_at": utc_now(),
                    "source": "backup-restore",
                    "backup_id": manifest.get("backup_id"),
                    "app_version": manifest.get("app_version"),
                    "data_schema_version": manifest.get("data_schema_version"),
                    "base_database_sha256": sha256_file(destination / "state.db"),
                    "status": "ready",
                    "sequence": self.next_generation_sequence(),
                    "verified_at": utc_now(),
                },
            )
            self.switch(identifier, reason="backup-restore")
            return identifier


def re_safe_identifier(value: str) -> bool:
    return bool(value) and all(character.isalnum() or character in "-_" for character in value)


def re_safe_label(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "-" for character in value
    )
    return cleaned.strip("-")[:40] or "snapshot"


def path_is_link_or_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt" or not path.exists():
        return False
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
