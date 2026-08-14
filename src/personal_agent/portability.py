from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from .core import APP_VERSION, PersonalAgentStore, utc_now
from .migrations import CURRENT_SCHEMA_VERSION, current_version, verify_database

EXPORT_FORMAT = "personal-agent.export/v1"
MAX_IMPORT_BYTES = 100 * 1024 * 1024


def jsonl(rows: Iterable[dict[str, Any]]) -> bytes:
    return (
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_archive_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe archive path: {value}")
    return path


def regular_user_files(store: PersonalAgentStore) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for category, root in (
        ("rules", store.rules_dir),
        ("skills", store.skills_dir),
        ("evals", store.evals_dir),
        ("memory", store.memory_dir),
    ):
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                relative = path.resolve().relative_to(root.resolve())
            except (OSError, ValueError):
                continue
            files.append(
                (
                    str(PurePosixPath("user") / category / PurePosixPath(relative.as_posix())),
                    path.read_bytes(),
                )
            )
    return files


def export_profile(
    store: PersonalAgentStore,
    destination: Path,
    *,
    include_episodes: bool = False,
) -> Path:
    store.initialize()
    destination = destination.expanduser().absolute()
    if destination.is_symlink():
        raise ValueError("Refusing to overwrite a symlinked export path.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with store.connection() as connection:
        memories = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM memories WHERE profile_id = ? ORDER BY id",
                (store.profile_id,),
            )
        ]
        candidates = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM candidates WHERE profile_id = ? ORDER BY id",
                (store.profile_id,),
            )
        ]
        evals = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM evals WHERE profile_id = ? ORDER BY id",
                (store.profile_id,),
            )
        ]
        episodes: list[dict[str, Any]] = []
        if include_episodes:
            episodes = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM episodes WHERE profile_id = ? ORDER BY id",
                    (store.profile_id,),
                )
            ]

    entries: list[tuple[str, bytes]] = [
        ("data/memories.jsonl", jsonl(memories)),
        ("data/candidates.jsonl", jsonl(candidates)),
        ("data/evals.jsonl", jsonl(evals)),
    ]
    if include_episodes:
        entries.append(("data/episodes.jsonl", jsonl(episodes)))
    entries.extend(regular_user_files(store))

    inventory = [
        {"path": path, "size": len(payload), "sha256": sha256_bytes(payload)}
        for path, payload in entries
    ]
    manifest = {
        "format": EXPORT_FORMAT,
        "app_version": APP_VERSION,
        "data_schema_version": CURRENT_SCHEMA_VERSION,
        "profile_id": store.profile_id,
        "exported_at": utc_now(),
        "include_episodes": include_episodes,
        "inventory": inventory,
    }
    entries.insert(
        0,
        (
            "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        ),
    )

    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with zipfile.ZipFile(
        temporary,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for archive_path, payload in entries:
            safe_archive_path(archive_path)
            archive.writestr(archive_path, payload)
    os.replace(temporary, destination)
    return destination


def parse_jsonl(payload: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in payload.decode("utf-8-sig").splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            raise ValueError("JSONL records must be objects.")
        rows.append(parsed)
    return rows


def read_validated_bundle(bundle: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    bundle = bundle.expanduser().resolve()
    if not bundle.is_file():
        raise ValueError(f"Export bundle does not exist: {bundle}")
    entries: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(bundle, "r") as archive:
        for info in archive.infolist():
            path = safe_archive_path(info.filename)
            if info.is_dir():
                continue
            if str(path) in entries:
                raise ValueError(f"Duplicate export path: {path}")
            if info.file_size < 0 or total + info.file_size > MAX_IMPORT_BYTES:
                raise ValueError("Export bundle exceeds the import size limit.")
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise ValueError(f"Symlinks are not allowed in exports: {path}")
            payload = archive.read(info)
            total += len(payload)
            entries[str(path)] = payload

    if "manifest.json" not in entries:
        raise ValueError("Export manifest is missing.")
    manifest = json.loads(entries["manifest.json"].decode("utf-8-sig"))
    if manifest.get("format") != EXPORT_FORMAT:
        raise ValueError("Unsupported export format.")
    inventory_paths: set[str] = set()
    for item in manifest.get("inventory", []):
        path = str(safe_archive_path(str(item["path"])))
        if path in inventory_paths:
            raise ValueError(f"Duplicate inventory path: {path}")
        inventory_paths.add(path)
        payload = entries.get(path)
        if payload is None:
            raise ValueError(f"Export entry is missing: {path}")
        if len(payload) != int(item["size"]):
            raise ValueError(f"Export size mismatch: {path}")
        if sha256_bytes(payload) != item["sha256"]:
            raise ValueError(f"Export checksum mismatch: {path}")
    actual_paths = set(entries) - {"manifest.json"}
    if actual_paths != inventory_paths:
        extras = sorted(actual_paths - inventory_paths)
        missing = sorted(inventory_paths - actual_paths)
        raise ValueError(f"Export inventory mismatch; extras={extras}, missing={missing}")
    return manifest, entries


def import_profile(
    home: Path,
    bundle: Path,
    *,
    target_profile: str,
    activate_assets: bool = False,
) -> PersonalAgentStore:
    _, entries = read_validated_bundle(bundle)
    target = PersonalAgentStore(home, profile_id=target_profile)
    if target.profile_dir.exists() and any(target.profile_dir.rglob("*")):
        raise ValueError(f"Target profile already contains user data: {target_profile}")

    episodes = parse_jsonl(entries.get("data/episodes.jsonl", b""))
    memories = parse_jsonl(entries.get("data/memories.jsonl", b""))
    candidates = parse_jsonl(entries.get("data/candidates.jsonl", b""))
    evals = parse_jsonl(entries.get("data/evals.jsonl", b""))
    validate_import_rows(memories, candidates, evals)

    target.layout.ensure_directories()
    staging_root = target.home / "staging" / f"import-{uuid.uuid4().hex}"
    profile_stage = staging_root / "profile"
    profile_stage.mkdir(parents=True)
    generation: str | None = None
    profile_moved = False
    committed = False
    try:
        for archive_path, payload in entries.items():
            path = PurePosixPath(archive_path)
            if not path.parts or path.parts[0] != "user" or len(path.parts) < 3:
                continue
            category = path.parts[1]
            if category not in {"rules", "skills", "evals", "memory"}:
                raise ValueError(f"Unsupported imported user category: {category}")
            if activate_assets:
                destination = profile_stage.joinpath(category, *path.parts[2:])
            else:
                destination = profile_stage.joinpath(
                    "quarantine",
                    "imported-assets",
                    category,
                    *path.parts[2:],
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)

        # Initialize a database before taking the upgrade lock.
        base = PersonalAgentStore(home, profile_id="default")
        base.initialize()
        with target.layout.locked(timeout=60):
            generation, staged_database = target.layout.clone_active_to_staging()
            connection = sqlite3.connect(staged_database, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                connection.execute("BEGIN IMMEDIATE")
                import_rows(
                    connection,
                    target_profile=target.profile_id,
                    episodes=episodes,
                    memories=memories,
                    candidates=candidates,
                    evals=evals,
                    activate_assets=activate_assets,
                )
                connection.commit()
                verify_database(connection)
                schema_version = current_version(connection)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

            if target.profile_dir.exists():
                raise ValueError(f"Target profile appeared during import: {target.profile_id}")
            target.profile_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(profile_stage, target.profile_dir)
            profile_moved = True
            try:
                target.layout.mark_generation_ready(
                    generation,
                    app_version=APP_VERSION,
                    data_schema_version=schema_version,
                )
                target.layout.switch(generation, reason=f"import-{target.profile_id}")
                committed = True
            except Exception:
                shutil.rmtree(target.profile_dir, ignore_errors=True)
                profile_moved = False
                raise
    except BaseException:
        if generation is not None and target.layout.active_generation_id() == generation:
            committed = True
        if not committed and generation is not None:
            shutil.rmtree(
                target.layout.generation_dir(generation),
                ignore_errors=True,
            )
        if not committed and profile_moved:
            shutil.rmtree(target.profile_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    imported = PersonalAgentStore(home, profile_id=target_profile)
    imported.initialize()
    return imported


def validate_import_rows(
    memories: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    evals: list[dict[str, Any]],
) -> None:
    for row in memories:
        confidence = float(row.get("confidence", 1.0))
        if not 0 <= confidence <= 1:
            raise ValueError("Imported memory confidence must be between 0 and 1.")
        if not isinstance(row.get("text", ""), str):
            raise ValueError("Imported memory text must be a string.")
    for row in candidates:
        if not isinstance(row.get("text", ""), str):
            raise ValueError("Imported candidate text must be a string.")
        int(row.get("occurrences", 1))
        int(row.get("distinct_episode_count", 0))
    for row in evals:
        if not isinstance(row.get("name", ""), str) or not row.get("name"):
            raise ValueError("Imported Eval name must be a non-empty string.")
        for field in ("query_terms", "required_any", "forbidden_any"):
            value = row.get(field, "[]")
            parsed = json.loads(value) if isinstance(value, str) else value
            if not isinstance(parsed, list):
                raise ValueError(f"Imported Eval {field} must be a JSON list.")


def import_rows(
    connection: sqlite3.Connection,
    *,
    target_profile: str,
    episodes: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    evals: list[dict[str, Any]],
    activate_assets: bool,
) -> None:
    episode_id_map: dict[int, int] = {}
    for row in episodes:
        cursor = connection.execute(
            """
            INSERT INTO episodes (
                profile_id, workspace_id, session_id, prompt_id,
                prompt_digest, response_digest, user_prompt,
                assistant_response, prompt_preview, response_preview,
                capture_mode, applicable_eval_ids, response_revision,
                status, engine, model, created_at, updated_at, completed_at
            )
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_profile,
                row.get("workspace_id", "global"),
                row.get("session_id", "imported"),
                row.get("prompt_digest", ""),
                row.get("response_digest", ""),
                row.get("user_prompt"),
                row.get("assistant_response"),
                row.get("prompt_preview"),
                row.get("response_preview"),
                row.get("capture_mode", "metadata"),
                row.get("applicable_eval_ids", "[]"),
                int(row.get("response_revision", 0)),
                row.get("status", "completed"),
                row.get("engine", "import"),
                row.get("model"),
                row.get("created_at", utc_now()),
                row.get("updated_at", utc_now()),
                row.get("completed_at"),
            ),
        )
        episode_id_map[int(row.get("id", 0))] = int(cursor.lastrowid)

    for row in memories:
        connection.execute(
            """
            INSERT INTO memories (
                profile_id, workspace_id, kind, text, scope, authority,
                confidence, status, source, source_hash, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_profile,
                row.get("workspace_id", "global"),
                row.get("kind", "lesson"),
                row.get("text", ""),
                row.get("scope", "global"),
                row.get("authority", "user"),
                float(row.get("confidence", 1.0)),
                row.get("status", "active") if activate_assets else "quarantined",
                row.get("source", "import"),
                row.get("source_hash"),
                row.get("created_at", utc_now()),
                row.get("updated_at", utc_now()),
            ),
        )

    for row in candidates:
        source_episode = row.get("source_episode_id")
        connection.execute(
            """
            INSERT OR IGNORE INTO candidates (
                profile_id, workspace_id, kind, text, fingerprint,
                evidence_key, polarity, occurrences, distinct_episode_count,
                status, reason, source_episode_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_profile,
                row.get("workspace_id", "global"),
                row.get("kind", "lesson"),
                row.get("text", ""),
                row.get("fingerprint", ""),
                row.get("evidence_key", ""),
                row.get("polarity", "unknown"),
                int(row.get("occurrences", 1)),
                int(row.get("distinct_episode_count", 0)),
                row.get("status", "pending") if activate_assets else "quarantined",
                row.get("reason"),
                episode_id_map.get(int(source_episode or 0)),
                row.get("created_at", utc_now()),
                row.get("updated_at", utc_now()),
            ),
        )

    for row in evals:
        connection.execute(
            """
            INSERT INTO evals (
                profile_id, name, format_version, query_terms,
                required_any, forbidden_any, severity, enabled,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id, name) DO UPDATE SET
                query_terms = excluded.query_terms,
                required_any = excluded.required_any,
                forbidden_any = excluded.forbidden_any,
                severity = excluded.severity,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                target_profile,
                row.get("name", "imported-eval"),
                int(row.get("format_version", 1)),
                row.get("query_terms", "[]"),
                row.get("required_any", "[]"),
                row.get("forbidden_any", "[]"),
                row.get("severity", "advisory"),
                int(row.get("enabled", 1)) if activate_assets else 0,
                row.get("created_at", utc_now()),
                row.get("updated_at", utc_now()),
            ),
        )
