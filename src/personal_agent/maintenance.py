from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .core import PersonalAgentStore
from .layout import atomic_write_json, sha256_file, utc_now

PROFILE_TABLES = ("candidates", "memories", "evals", "episodes")


def purge_profile_from_database(database: Path, profile_id: str) -> dict[str, int]:
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    counts: dict[str, int] = {}
    try:
        connection.execute("BEGIN IMMEDIATE")
        candidate_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info('candidates')")
        }
        episode_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info('episodes')")
        }
        eval_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info('evals')")}
        if "candidate_evidence" in {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }:
            if "profile_id" in candidate_columns:
                connection.execute(
                    """
                    DELETE FROM candidate_evidence
                    WHERE candidate_id IN (
                        SELECT id FROM candidates WHERE profile_id = ?
                    )
                    """,
                    (profile_id,),
                )
            elif profile_id == "default":
                connection.execute("DELETE FROM candidate_evidence")
        if "eval_runs" in {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }:
            if "profile_id" in episode_columns and "profile_id" in eval_columns:
                connection.execute(
                    """
                    DELETE FROM eval_runs
                    WHERE episode_id IN (
                        SELECT id FROM episodes WHERE profile_id = ?
                    )
                       OR eval_id IN (
                        SELECT id FROM evals WHERE profile_id = ?
                    )
                    """,
                    (profile_id, profile_id),
                )
            elif profile_id == "default":
                connection.execute("DELETE FROM eval_runs")
        for table in PROFILE_TABLES:
            columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if not columns:
                continue
            if "profile_id" in columns:
                cursor = connection.execute(
                    f'DELETE FROM "{table}" WHERE profile_id = ?',
                    (profile_id,),
                )
            elif profile_id == "default":
                cursor = connection.execute(f'DELETE FROM "{table}"')
            else:
                continue
            counts[table] = max(0, cursor.rowcount)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(f"Database failed quick_check after purge: {database}")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return counts


def update_manifest_after_purge(
    manifest_path: Path,
    database: Path,
    *,
    profile_id: str,
) -> None:
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    manifest["last_privacy_purge"] = {
        "profile_id": profile_id,
        "purged_at": utc_now(),
        "database_sha256": sha256_file(database),
    }
    if manifest.get("format") == "personal-agent.backup/v1":
        manifest["database_sha256"] = sha256_file(database)
    atomic_write_json(manifest_path, manifest)


def purge_profile(
    store: PersonalAgentStore,
    *,
    include_backups: bool,
) -> dict[str, Any]:
    store.layout.ensure_directories()
    profile_id = store.profile_id
    for manifest_path in (store.layout.control_dir / "installs").glob("*.json"):
        try:
            install = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if install.get("profile_id") == profile_id:
            raise ValueError("Uninstall the engine adapter before purging this profile.")
    report: dict[str, Any] = {
        "profile_id": profile_id,
        "generations": {},
        "backups": {},
        "user_files_removed": False,
        "external_exports_removed": False,
    }
    with store.layout.locked(timeout=60):
        for directory in store.layout.generations_dir.iterdir():
            if not directory.is_dir():
                continue
            database = directory / "state.db"
            if not database.is_file():
                continue
            report["generations"][directory.name] = purge_profile_from_database(
                database, profile_id
            )
            update_manifest_after_purge(
                directory / "generation.json",
                database,
                profile_id=profile_id,
            )

        if include_backups:
            for directory in store.layout.backups_dir.iterdir():
                if not directory.is_dir():
                    continue
                database = directory / "state.db"
                if not database.is_file():
                    continue
                report["backups"][directory.name] = purge_profile_from_database(
                    database, profile_id
                )
                update_manifest_after_purge(
                    directory / "backup.json",
                    database,
                    profile_id=profile_id,
                )

        if profile_id == "default" and store.layout.legacy_db.is_file():
            report["legacy_database"] = purge_profile_from_database(
                store.layout.legacy_db,
                profile_id,
            )
        if profile_id == "default":
            for legacy_path in (
                store.layout.legacy_package,
                store.layout.legacy_memory,
            ):
                if legacy_path.exists():
                    shutil.rmtree(legacy_path)

        if store.profile_dir.exists():
            shutil.rmtree(store.profile_dir)
            report["user_files_removed"] = True
    return report
