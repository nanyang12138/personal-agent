from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .layout import StateLayout, atomic_write_json
from .migrations import (
    CURRENT_SCHEMA_VERSION,
    apply_migrations,
    current_version,
    verify_database,
)

APP_VERSION = "0.2.0"

DEFAULT_CORE_RULES = """# Personal Agent Core Rules

- Treat Rules, Skills, Memory, and Evals as user-owned assets; the model is a replaceable engine.
- Apply only the personal context relevant to the current task.
- Distinguish verified facts, inference, and suggestions.
- For tool-heavy work, delegate execution when supported and independently verify material results.
- Do not claim completion without evidence that satisfies the active evals.
- Never store credentials, secrets, employer-confidential data, or raw private code in personal memory.
- The model may propose changes to Rules or Skills, but only the promotion pipeline may activate them.
"""

DEFAULT_SKILL = """---
name: evidence-first-work
description: Performs implementation, debugging, and technical research with explicit acceptance criteria, evidence, and independent verification. Use for 修改、实现、修复、调试、技术调研和验证任务.
---

# Evidence-first work

1. Convert the request into an objective, acceptance criteria, allowed actions, and forbidden actions.
2. Gather primary evidence before selecting a solution.
3. Make the smallest justified change.
4. Run the relevant validation.
5. Report the outcome, artifacts, evidence, validation, and remaining risk.
6. Never use a worker's own summary as the only proof of success.
"""

DEFAULT_EVALS = [
    {
        "name": "evidence-for-changes",
        "query_terms": ["implement", "fix", "change", "修改", "实现", "修复"],
        "required_any": ["test", "verify", "validation", "测试", "验证", "检查"],
        "forbidden_any": [],
        "enabled": True,
    },
    {
        "name": "prior-art-for-patent-claims",
        "query_terms": ["patent", "novelty", "专利", "创新性", "新颖性"],
        "required_any": ["prior art", "closest work", "现有技术", "直接先例", "新颖性"],
        "forbidden_any": [],
        "enabled": True,
    },
]

CORRECTION_PATTERNS = (
    r"\bremember\b",
    r"\bnext time\b",
    r"\bthat is not what i want\b",
    r"\byou should\b",
    r"\bdon['’]?t\b",
    "以后",
    "记住",
    "不对",
    "不是我要",
    "我希望",
    "应该先",
    "不要再",
    "这次哪里做错",
)

SENSITIVE_PATTERNS = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b(?:api[_ -]?key|password|passwd|secret|token)\b\s*(?::|=|\bis\b)\s*\S+",
    r"\b(?:client_secret|aws_secret_access_key|refresh_token|access_token)\b\s*[:=]\s*\S+",
    r"\bauthorization\s*:\s*bearer\s+\S+",
    r"\bcookie\s*:\s*\S+",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\bgh[opusr]_[A-Za-z0-9]{20,}\b",
    r"\bsk-[A-Za-z0-9_-]{20,}\b",
    "公司内部",
    "客户机密",
    "保密信息",
    "confidential",
)

REDACTION_PATTERNS = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    r"\b(?:api[_ -]?key|password|passwd|secret|token)\b\s*(?::|=|\bis\b)\s*\S+",
    r"\b(?:client_secret|aws_secret_access_key|refresh_token|access_token)\b\s*[:=]\s*\S+",
    r"\bauthorization\s*:\s*bearer\s+\S+",
    r"\bcookie\s*:\s*\S+",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\bgh[opusr]_[A-Za-z0-9]{20,}\b",
    r"\bsk-[A-Za-z0-9_-]{20,}\b",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_home() -> Path:
    configured = os.environ.get("PERSONAL_AGENT_HOME")
    if configured:
        return Path(configured).expanduser().absolute()
    return (Path.home() / ".personal-agent").absolute()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def character_ngrams(value: str, n: int = 2) -> set[str]:
    normalized = re.sub(r"\s+", "", normalize_text(value))
    if not normalized:
        return set()
    if len(normalized) <= n:
        return {normalized}
    return {normalized[index : index + n] for index in range(len(normalized) - n + 1)}


def text_similarity(left: str, right: str) -> float:
    left_grams = character_ngrams(left)
    right_grams = character_ngrams(right)
    if not left_grams or not right_grams:
        return 0.0
    overlap = len(left_grams & right_grams)
    return overlap / math.sqrt(len(left_grams) * len(right_grams))


def contains_any(text: str, values: Iterable[str]) -> bool:
    normalized = normalize_text(text)
    return any(normalize_text(value) in normalized for value in values if value)


def looks_sensitive(text: str) -> bool:
    return any(
        re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in SENSITIVE_PATTERNS
    )


def redact_text(text: str) -> str:
    redacted = text
    for pattern in REDACTION_PATTERNS:
        redacted = re.sub(
            pattern,
            "[REDACTED]",
            redacted,
            flags=re.IGNORECASE | re.DOTALL,
        )
    return redacted


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_workspace_id(workspace: str) -> str:
    if not workspace:
        return "global"
    try:
        normalized = str(Path(workspace).expanduser().resolve())
    except OSError:
        normalized = workspace.strip()
    if os.name == "nt":
        normalized = os.path.normcase(normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def escape_context(value: str) -> str:
    return value.replace("<", "&lt;").replace(">", "&gt;")


def classify_feedback(text: str) -> str | None:
    if not any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in CORRECTION_PATTERNS):
        return None
    normalized = normalize_text(text)
    if any(token in normalized for token in ("我喜欢", "我希望", "prefer", "always respond")):
        return "preference"
    return "lesson"


@dataclass(frozen=True)
class EvalResult:
    eval_id: int
    name: str
    passed: bool
    details: str
    severity: str = "advisory"


@dataclass(frozen=True)
class SkillMatch:
    name: str
    text: str
    score: float


class PersonalAgentStore:
    def __init__(
        self,
        home: Path | str | None = None,
        *,
        profile_id: str | None = None,
    ) -> None:
        self.home = Path(home).expanduser().absolute() if home else default_home()
        requested_profile = profile_id or os.environ.get("PERSONAL_AGENT_PROFILE", "default")
        requested_profile = requested_profile.casefold()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", requested_profile):
            raise ValueError("Profile ID may contain only letters, numbers, _ and -.")
        self.profile_id = requested_profile
        self.layout = StateLayout(self.home)
        self.profile_dir = self.layout.user_dir / "profiles" / self.profile_id
        self.rules_dir = self.profile_dir / "rules"
        self.skills_dir = self.profile_dir / "skills"
        self.evals_dir = self.profile_dir / "evals"
        self.memory_dir = self.profile_dir / "memory"
        self.manifest_path = self.profile_dir / "manifest.json"
        self.db_path: Path | None = None
        self._initialized = False

    def connect(self) -> sqlite3.Connection:
        if self.db_path is None:
            raise RuntimeError("Personal Agent store is not initialized.")
        active_database = self.layout.active_db()
        if active_database != self.db_path:
            self.db_path = active_database
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        with self.layout.locked(timeout=30):
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        self.layout.ensure_directories()
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.evals_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._copy_legacy_user_assets()

        if not self.manifest_path.exists():
            atomic_write_json(
                self.manifest_path,
                {
                    "format": "personal-agent.profile/v1",
                    "profile_id": self.profile_id,
                    "package_version": APP_VERSION,
                    "capture": {
                        "episodes": False,
                        "mode": "metadata",
                        "preview_chars": 0,
                        "retention_days": 30,
                    },
                    "learning": {
                        "stage_corrections": False,
                        "auto_promote_after": 0,
                        "auto_promote_kinds": [],
                        "never_auto_promote": [
                            "rule",
                            "skill",
                            "permission",
                            "preference",
                            "lesson",
                        ],
                    },
                    "context": {
                        "max_chars": 9000,
                        "rules_chars": 2200,
                        "memory_chars": 2200,
                        "skills_chars": 3500,
                        "evals_chars": 900,
                    },
                },
            )

        memory_index = self.memory_dir / "MEMORY.md"
        if not memory_index.exists():
            memory_index.write_text(
                "# Personal Agent Memory\n\n"
                "Claude Code may maintain this directory through Auto Memory. "
                "Do not store secrets or employer-confidential content here.\n",
                encoding="utf-8",
            )

        self.db_path = self._prepare_database()
        with self.write_connection() as connection:
            for eval_definition in DEFAULT_EVALS:
                self._insert_eval_if_missing(connection, eval_definition)
        self._initialized = True

    def _copy_legacy_user_assets(self) -> None:
        marker = self.layout.control_dir / f"legacy-assets-{self.profile_id}.json"
        if marker.exists():
            return
        if not self.layout.legacy_package.exists():
            return
        copied: list[str] = []
        for name, destination in (
            ("rules", self.rules_dir),
            ("skills", self.skills_dir),
            ("evals", self.evals_dir),
        ):
            source = self.layout.legacy_package / name
            if not source.is_dir():
                continue
            for path in source.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    path.resolve().relative_to(source.resolve())
                except (OSError, ValueError):
                    continue
                relative = path.relative_to(source)
                target = destination / relative
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                copied.append(str(target.relative_to(self.profile_dir)))
        legacy_memory_index = self.layout.legacy_memory / "MEMORY.md"
        target_memory_index = self.memory_dir / "MEMORY.md"
        if (
            legacy_memory_index.is_file()
            and not legacy_memory_index.is_symlink()
            and not target_memory_index.exists()
        ):
            shutil.copy2(legacy_memory_index, target_memory_index)
            copied.append(str(target_memory_index.relative_to(self.profile_dir)))
        legacy_manifest = self.layout.legacy_package / "manifest.json"
        if legacy_manifest.is_file() and not legacy_manifest.is_symlink():
            migration_dir = self.profile_dir / "migrations"
            migration_dir.mkdir(parents=True, exist_ok=True)
            destination = migration_dir / "legacy-manifest.json"
            if not destination.exists():
                shutil.copy2(legacy_manifest, destination)
                copied.append(str(destination.relative_to(self.profile_dir)))
        atomic_write_json(
            marker,
            {
                "format": "personal-agent.legacy-assets/v1",
                "profile_id": self.profile_id,
                "copied_at": utc_now(),
                "copied": copied,
                "legacy_source_preserved": True,
            },
        )

    def _prepare_database(self) -> Path:
        active = self.layout.initialize(max_schema_version=CURRENT_SCHEMA_VERSION)
        connection = sqlite3.connect(active, timeout=30)
        try:
            version = current_version(connection)
        finally:
            connection.close()
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(f"Data schema {version} is newer than this application supports.")
        if version == CURRENT_SCHEMA_VERSION:
            connection = sqlite3.connect(active, timeout=30)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                verify_database(connection)
            finally:
                connection.close()
            return active

        with self.layout.locked(timeout=60):
            active = self.layout.active_db()
            connection = sqlite3.connect(active, timeout=30)
            try:
                version = current_version(connection)
            finally:
                connection.close()
            if version == CURRENT_SCHEMA_VERSION:
                return active
            self.layout.snapshot_active(label=f"pre-schema-{version}-to-{CURRENT_SCHEMA_VERSION}")
            generation, staged_database = self.layout.clone_active_to_staging()
            migrated = apply_migrations(staged_database, app_version=APP_VERSION)
            self.layout.mark_generation_ready(
                generation,
                app_version=APP_VERSION,
                data_schema_version=migrated,
            )
            self.layout.switch(generation, reason=f"schema-{version}-to-{migrated}")
            return self.layout.active_db()

    def _insert_eval_if_missing(
        self, connection: sqlite3.Connection, definition: dict[str, Any]
    ) -> None:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO evals (
                profile_id, name, query_terms, required_any, forbidden_any,
                severity, enabled, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'advisory', ?, ?, ?)
            ON CONFLICT(profile_id, name) DO NOTHING
            """,
            (
                self.profile_id,
                definition["name"],
                json.dumps(definition["query_terms"], ensure_ascii=False),
                json.dumps(definition["required_any"], ensure_ascii=False),
                json.dumps(definition["forbidden_any"], ensure_ascii=False),
                int(definition.get("enabled", True)),
                now,
                now,
            ),
        )

    def manifest(self) -> dict[str, Any]:
        self.initialize()
        return json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))

    def add_memory(
        self,
        text: str,
        *,
        kind: str = "lesson",
        scope: str = "global",
        confidence: float = 1.0,
        source: str = "manual",
    ) -> int:
        self.initialize()
        if looks_sensitive(text):
            raise ValueError("Refusing to store text that looks sensitive or confidential.")
        now = utc_now()
        workspace_id = "global" if scope == "global" else canonical_workspace_id(scope)
        with self.write_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memories (
                    profile_id, workspace_id, kind, text, scope, authority,
                    confidence, status, source, source_hash, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'user', ?, 'active', ?, ?, ?, ?)
                """,
                (
                    self.profile_id,
                    workspace_id,
                    kind,
                    text.strip(),
                    scope,
                    confidence,
                    source,
                    text_digest(text),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list_memories(self, *, status: str = "active") -> list[sqlite3.Row]:
        self.initialize()
        with self.connection() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM memories
                    WHERE profile_id = ? AND status = ?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (self.profile_id, status),
                )
            )

    def delete_memory(self, memory_id: int) -> bool:
        self.initialize()
        with self.write_connection() as connection:
            cursor = connection.execute(
                "DELETE FROM memories WHERE id = ? AND profile_id = ?",
                (memory_id, self.profile_id),
            )
            return cursor.rowcount > 0

    def search_memories(
        self, query: str, *, workspace: str | None = None, limit: int = 5
    ) -> list[tuple[sqlite3.Row, float]]:
        candidates = self.list_memories()
        ranked: list[tuple[sqlite3.Row, float]] = []
        workspace_id = canonical_workspace_id(workspace or "")
        for row in candidates:
            if row["workspace_id"] not in ("global", workspace_id):
                continue
            similarity = text_similarity(query, row["text"])
            if row["kind"] in ("preference", "rule"):
                similarity += 0.15
            similarity *= float(row["confidence"])
            if similarity > 0.02:
                ranked.append((row, similarity))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked[:limit]

    def capture_policy(self) -> dict[str, Any]:
        capture = self.manifest().get("capture", {})
        return {
            "episodes": bool(capture.get("episodes", False)),
            "mode": str(capture.get("mode", "metadata")),
            "preview_chars": max(0, min(int(capture.get("preview_chars", 0)), 2000)),
            "retention_days": max(0, int(capture.get("retention_days", 30))),
        }

    def start_turn(
        self,
        *,
        session_id: str,
        prompt_id: str | None,
        workspace: str,
        user_prompt: str,
        engine: str = "claude-code",
        model: str | None = None,
    ) -> int | None:
        self.initialize()
        policy = self.capture_policy()
        if not policy["episodes"]:
            return None
        self.prune_expired_episodes()
        mode = policy["mode"]
        if mode not in {"metadata", "redacted", "full"}:
            raise ValueError(f"Unsupported capture mode: {mode}")
        redacted_prompt = redact_text(user_prompt)
        if mode == "full" and looks_sensitive(user_prompt):
            mode = "redacted"
        stored_prompt = user_prompt if mode == "full" else None
        preview_chars = int(policy["preview_chars"])
        prompt_preview = (
            redacted_prompt[:preview_chars] if mode == "redacted" and preview_chars else None
        )
        workspace_id = canonical_workspace_id(workspace)
        applicable_eval_ids = [int(row["id"]) for row in self.relevant_evals(user_prompt)]
        applicable_eval_json = json.dumps(applicable_eval_ids)
        now = utc_now()
        with self.write_connection() as connection:
            if prompt_id:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO episodes (
                        profile_id, workspace_id, session_id, prompt_id,
                        prompt_digest, user_prompt, prompt_preview, capture_mode,
                        applicable_eval_ids, status, engine, model, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?, ?, ?, ?)
                    """,
                    (
                        self.profile_id,
                        workspace_id,
                        session_id,
                        prompt_id,
                        text_digest(user_prompt),
                        stored_prompt,
                        prompt_preview,
                        mode,
                        applicable_eval_json,
                        engine,
                        model,
                        now,
                        now,
                    ),
                )
                existing = connection.execute(
                    """
                    SELECT id FROM episodes
                    WHERE profile_id = ? AND engine = ? AND prompt_id = ?
                    """,
                    (self.profile_id, engine, prompt_id),
                ).fetchone()
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO episodes (
                    profile_id, workspace_id, session_id, prompt_id,
                    prompt_digest, user_prompt, prompt_preview, capture_mode,
                    applicable_eval_ids, status, engine, model, created_at, updated_at
                )
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'started', ?, ?, ?, ?)
                """,
                (
                    self.profile_id,
                    workspace_id,
                    session_id,
                    text_digest(user_prompt),
                    stored_prompt,
                    prompt_preview,
                    mode,
                    applicable_eval_json,
                    engine,
                    model,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def latest_completed_episode(self, session_id: str) -> sqlite3.Row | None:
        self.initialize()
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM episodes
                WHERE profile_id = ? AND session_id = ? AND status = 'completed'
                ORDER BY id DESC LIMIT 1
                """,
                (self.profile_id, session_id),
            ).fetchone()

    def finish_turn(
        self,
        *,
        session_id: str,
        assistant_response: str,
        revise_last: bool = False,
        prompt_id: str | None = None,
    ) -> int | None:
        self.initialize()
        if not self.capture_policy()["episodes"]:
            return None
        with self.write_connection() as connection:
            if prompt_id:
                episode = connection.execute(
                    """
                    SELECT * FROM episodes
                    WHERE profile_id = ? AND session_id = ? AND prompt_id = ?
                      AND status = 'started'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (self.profile_id, session_id, prompt_id),
                ).fetchone()
            else:
                episode = connection.execute(
                    """
                    SELECT * FROM episodes
                    WHERE profile_id = ? AND session_id = ? AND status = 'started'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (self.profile_id, session_id),
                ).fetchone()
            if not episode and revise_last:
                if prompt_id:
                    episode = connection.execute(
                        """
                        SELECT * FROM episodes
                        WHERE profile_id = ? AND session_id = ? AND prompt_id = ?
                          AND status = 'completed'
                        ORDER BY id DESC LIMIT 1
                        """,
                        (self.profile_id, session_id, prompt_id),
                    ).fetchone()
                else:
                    episode = connection.execute(
                        """
                        SELECT * FROM episodes
                        WHERE profile_id = ? AND session_id = ? AND status = 'completed'
                        ORDER BY id DESC LIMIT 1
                        """,
                        (self.profile_id, session_id),
                    ).fetchone()
            if not episode:
                return None
            episode_id = int(episode["id"])
            revision = int(episode["response_revision"] or 0) + 1
            mode = str(episode["capture_mode"])
            redacted_response = redact_text(assistant_response)
            stored_response = (
                assistant_response
                if mode == "full" and not looks_sensitive(assistant_response)
                else None
            )
            preview_chars = int(self.capture_policy()["preview_chars"])
            response_preview = (
                redacted_response[:preview_chars] if mode == "redacted" and preview_chars else None
            )
            connection.execute(
                """
                UPDATE episodes
                SET assistant_response = ?, response_preview = ?,
                    response_digest = ?, response_revision = ?,
                    status = 'completed', updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    stored_response,
                    response_preview,
                    text_digest(assistant_response),
                    revision,
                    utc_now(),
                    utc_now(),
                    episode_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO episode_responses (
                    episode_id, revision, response_digest, response_text,
                    response_preview, capture_mode, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id, revision) DO UPDATE SET
                    response_digest = excluded.response_digest,
                    response_text = excluded.response_text,
                    response_preview = excluded.response_preview,
                    capture_mode = excluded.capture_mode,
                    created_at = excluded.created_at
                """,
                (
                    episode_id,
                    revision,
                    text_digest(assistant_response),
                    stored_response,
                    response_preview,
                    mode,
                    utc_now(),
                ),
            )
            self._run_evals_in_connection(
                connection,
                episode_id=episode_id,
                prompt=episode["user_prompt"] or episode["prompt_preview"] or "",
                response=assistant_response,
                response_revision=revision,
                applicable_eval_ids=json.loads(episode["applicable_eval_ids"] or "[]"),
            )
        return episode_id

    def fail_turn(
        self,
        *,
        session_id: str,
        prompt_id: str | None,
        error_type: str,
    ) -> int | None:
        self.initialize()
        if not self.capture_policy()["episodes"]:
            return None
        with self.write_connection() as connection:
            if prompt_id:
                episode = connection.execute(
                    """
                    SELECT id FROM episodes
                    WHERE profile_id = ? AND session_id = ? AND prompt_id = ?
                      AND status = 'started'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (self.profile_id, session_id, prompt_id),
                ).fetchone()
            else:
                episode = connection.execute(
                    """
                    SELECT id FROM episodes
                    WHERE profile_id = ? AND session_id = ? AND status = 'started'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (self.profile_id, session_id),
                ).fetchone()
            if episode is None:
                return None
            episode_id = int(episode["id"])
            connection.execute(
                """
                UPDATE episodes
                SET status = 'failed', response_preview = ?, updated_at = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (
                    f"[Claude Code error: {redact_text(error_type)[:120]}]",
                    utc_now(),
                    utc_now(),
                    episode_id,
                ),
            )
            return episode_id

    def list_episodes(self, *, limit: int = 20) -> list[sqlite3.Row]:
        self.initialize()
        with self.connection() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM episodes
                    WHERE profile_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (self.profile_id, limit),
                )
            )

    def delete_episode(self, episode_id: int) -> bool:
        self.initialize()
        with self.write_connection() as connection:
            connection.execute(
                """
                UPDATE candidates
                SET source_episode_id = NULL
                WHERE source_episode_id = ? AND profile_id = ?
                """,
                (episode_id, self.profile_id),
            )
            cursor = connection.execute(
                "DELETE FROM episodes WHERE id = ? AND profile_id = ?",
                (episode_id, self.profile_id),
            )
            return cursor.rowcount > 0

    def prune_expired_episodes(self) -> int:
        self.initialize()
        days = int(self.capture_policy()["retention_days"])
        if days <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.write_connection() as connection:
            expired_ids = [
                int(row["id"])
                for row in connection.execute(
                    """
                    SELECT id FROM episodes
                    WHERE profile_id = ?
                      AND COALESCE(completed_at, created_at) < ?
                    """,
                    (self.profile_id, cutoff),
                )
            ]
            if not expired_ids:
                return 0
            placeholders = ",".join("?" for _ in expired_ids)
            connection.execute(
                f"""
                UPDATE candidates
                SET source_episode_id = NULL
                WHERE profile_id = ? AND source_episode_id IN ({placeholders})
                """,
                (self.profile_id, *expired_ids),
            )
            connection.execute(
                f"""
                DELETE FROM episodes
                WHERE profile_id = ? AND id IN ({placeholders})
                """,
                (self.profile_id, *expired_ids),
            )
            return len(expired_ids)

    def compact_deleted_content(self) -> None:
        self.initialize()
        connection = self.connect()
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
        finally:
            connection.close()

    def stage_feedback_from_prompt(self, *, session_id: str, prompt: str) -> int | None:
        learning = self.manifest().get("learning", {})
        if not bool(learning.get("stage_corrections", False)):
            return None
        kind = classify_feedback(prompt)
        if not kind or looks_sensitive(prompt):
            return None
        previous = self.latest_completed_episode(session_id)
        if previous is None:
            return None
        return self.stage_candidate(
            text=prompt.strip(),
            kind=kind,
            reason="Detected explicit user correction or preference in a subsequent turn.",
            source_episode_id=int(previous["id"]),
            workspace_id=str(previous["workspace_id"]),
        )

    def stage_candidate(
        self,
        *,
        text: str,
        kind: str,
        reason: str,
        source_episode_id: int | None = None,
        workspace_id: str = "global",
    ) -> int:
        self.initialize()
        if looks_sensitive(text):
            raise ValueError("Refusing to stage sensitive or confidential text.")
        fingerprint = normalize_text(text)
        evidence_key = text_digest(f"{kind}\0{fingerprint}")
        polarity = (
            "negative"
            if contains_any(text, ["不要", "不能", "never", "don't", "do not"])
            else "positive"
        )
        now = utc_now()
        with self.write_connection() as connection:
            candidate = connection.execute(
                """
                SELECT * FROM candidates
                WHERE profile_id = ? AND workspace_id = ? AND kind = ?
                  AND evidence_key = ? AND status = 'pending'
                """,
                (self.profile_id, workspace_id, kind, evidence_key),
            ).fetchone()
            if candidate is not None:
                candidate_id = int(candidate["id"])
                connection.execute(
                    """
                    UPDATE candidates
                    SET occurrences = occurrences + 1, updated_at = ?, reason = ?
                    WHERE id = ?
                    """,
                    (now, reason, candidate_id),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO candidates (
                        profile_id, workspace_id, kind, text, fingerprint,
                        evidence_key, polarity, occurrences, distinct_episode_count,
                        status, reason, source_episode_id, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        self.profile_id,
                        workspace_id,
                        kind,
                        text,
                        fingerprint,
                        evidence_key,
                        polarity,
                        reason,
                        source_episode_id,
                        now,
                        now,
                    ),
                )
                candidate_id = int(cursor.lastrowid)

            if source_episode_id is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO candidate_evidence (
                        candidate_id, episode_id, created_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (candidate_id, source_episode_id, now),
                )
                distinct = connection.execute(
                    """
                    SELECT COUNT(*) FROM candidate_evidence
                    WHERE candidate_id = ?
                    """,
                    (candidate_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    UPDATE candidates
                    SET distinct_episode_count = ?
                    WHERE id = ?
                    """,
                    (int(distinct), candidate_id),
                )
        return candidate_id

    def list_candidates(self, *, status: str = "pending") -> list[sqlite3.Row]:
        self.initialize()
        with self.connection() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM candidates
                    WHERE profile_id = ? AND status = ?
                    ORDER BY updated_at DESC
                    """,
                    (self.profile_id, status),
                )
            )

    def approve_candidate(self, candidate_id: int, *, source: str = "approved") -> int:
        self.initialize()
        with self.write_connection() as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE id = ? AND profile_id = ?",
                (candidate_id, self.profile_id),
            ).fetchone()
            if not candidate:
                raise ValueError(f"Candidate {candidate_id} does not exist.")
            if candidate["status"] == "approved":
                existing = connection.execute(
                    """
                    SELECT id FROM memories
                    WHERE profile_id = ? AND source LIKE ?
                    """,
                    (self.profile_id, f"candidate:{candidate_id}:%"),
                ).fetchone()
                if existing:
                    return int(existing["id"])
            if looks_sensitive(candidate["text"]):
                raise ValueError("Refusing to promote sensitive or confidential text.")
            now = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO memories (
                    profile_id, workspace_id, kind, text, scope, authority,
                    confidence, status, source, source_hash, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'workspace', 'user', ?, 'active', ?, ?, ?, ?)
                """,
                (
                    self.profile_id,
                    candidate["workspace_id"],
                    candidate["kind"],
                    candidate["text"],
                    min(0.95, 0.6 + 0.1 * int(candidate["occurrences"])),
                    f"candidate:{candidate_id}:{source}",
                    text_digest(candidate["text"]),
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE candidates SET status = 'approved', updated_at = ? WHERE id = ?",
                (now, candidate_id),
            )
            return int(cursor.lastrowid)

    def reject_candidate(self, candidate_id: int) -> None:
        self.initialize()
        with self.write_connection() as connection:
            connection.execute(
                """
                UPDATE candidates
                SET status = 'rejected', updated_at = ?
                WHERE id = ? AND profile_id = ?
                """,
                (utc_now(), candidate_id, self.profile_id),
            )

    def add_eval(
        self,
        *,
        name: str,
        query_terms: list[str],
        required_any: list[str],
        forbidden_any: list[str],
        enabled: bool = True,
        severity: str = "advisory",
    ) -> int:
        self.initialize()
        if severity not in {"advisory", "required", "blocking"}:
            raise ValueError("Eval severity must be advisory, required, or blocking.")
        if severity != "advisory":
            raise ValueError(
                "Response-text Evals are advisory only. "
                "Required/blocking Evals need structured tool evidence."
            )
        now = utc_now()
        with self.write_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO evals (
                    profile_id, name, query_terms, required_any, forbidden_any,
                    severity, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, name) DO UPDATE SET
                    query_terms = excluded.query_terms,
                    required_any = excluded.required_any,
                    forbidden_any = excluded.forbidden_any,
                    severity = excluded.severity,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    self.profile_id,
                    name,
                    json.dumps(query_terms, ensure_ascii=False),
                    json.dumps(required_any, ensure_ascii=False),
                    json.dumps(forbidden_any, ensure_ascii=False),
                    severity,
                    int(enabled),
                    now,
                    now,
                ),
            )
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            row = connection.execute(
                "SELECT id FROM evals WHERE profile_id = ? AND name = ?",
                (self.profile_id, name),
            ).fetchone()
            return int(row["id"])

    def list_evals(self) -> list[sqlite3.Row]:
        self.initialize()
        with self.connection() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM evals WHERE profile_id = ? ORDER BY name",
                    (self.profile_id,),
                )
            )

    def relevant_evals(self, prompt: str) -> list[sqlite3.Row]:
        evals = self.list_evals()
        relevant: list[sqlite3.Row] = []
        for row in evals:
            if not row["enabled"]:
                continue
            terms = json.loads(row["query_terms"])
            if not terms or contains_any(prompt, terms):
                relevant.append(row)
        return relevant

    def run_evals(self, episode_id: int) -> list[EvalResult]:
        self.initialize()
        with self.write_connection() as connection:
            episode = connection.execute(
                "SELECT * FROM episodes WHERE id = ? AND profile_id = ?",
                (episode_id, self.profile_id),
            ).fetchone()
            if not episode:
                raise ValueError(f"Episode {episode_id} does not exist.")
            prompt = episode["user_prompt"] or episode["prompt_preview"] or ""
            response = episode["assistant_response"] or episode["response_preview"] or ""
            return self._run_evals_in_connection(
                connection,
                episode_id=episode_id,
                prompt=prompt,
                response=response,
                response_revision=int(episode["response_revision"] or 0),
                applicable_eval_ids=json.loads(episode["applicable_eval_ids"] or "[]"),
            )

    def _run_evals_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        episode_id: int,
        prompt: str,
        response: str,
        response_revision: int,
        applicable_eval_ids: list[int] | None = None,
    ) -> list[EvalResult]:
        rows = list(
            connection.execute(
                """
                SELECT * FROM evals
                WHERE profile_id = ? AND enabled = 1
                ORDER BY name
                """,
                (self.profile_id,),
            )
        )
        results: list[EvalResult] = []
        for row in rows:
            if applicable_eval_ids is not None and int(row["id"]) not in applicable_eval_ids:
                continue
            terms = json.loads(row["query_terms"])
            if applicable_eval_ids is None and terms and not contains_any(prompt, terms):
                continue
            required = json.loads(row["required_any"])
            forbidden = json.loads(row["forbidden_any"])
            missing_required = bool(required) and not contains_any(response, required)
            present_forbidden = [item for item in forbidden if contains_any(response, [item])]
            passed = not missing_required and not present_forbidden
            details: list[str] = []
            if missing_required:
                details.append(f"advisory text check missing one of: {required}")
            if present_forbidden:
                details.append(f"advisory text check contains forbidden: {present_forbidden}")
            if not details:
                details.append("advisory text check passed")
            result = EvalResult(
                eval_id=int(row["id"]),
                name=str(row["name"]),
                passed=passed,
                details="; ".join(details),
                severity=str(row["severity"]),
            )
            results.append(result)
            connection.execute(
                """
                INSERT INTO eval_runs (
                    episode_id, eval_id, response_revision, passed,
                    details, evidence, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id, eval_id, response_revision)
                DO UPDATE SET
                    passed = excluded.passed,
                    details = excluded.details,
                    evidence = excluded.evidence,
                    created_at = excluded.created_at
                """,
                (
                    episode_id,
                    result.eval_id,
                    response_revision,
                    int(result.passed),
                    result.details,
                    "response-text-lint",
                    utc_now(),
                ),
            )
        return results

    def _read_core_rules(self) -> str:
        rules: list[str] = [DEFAULT_CORE_RULES.strip()]
        for path in sorted(self.rules_dir.glob("*.md")):
            if not self._safe_user_file(path, self.rules_dir):
                continue
            rules.append(path.read_text(encoding="utf-8").strip())
        return "\n\n".join(rules)

    def _safe_user_file(self, path: Path, root: Path) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        try:
            path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return False
        return True

    def _skill_description(self, text: str) -> str:
        match = re.search(r"^description:\s*(.+)$", text, flags=re.MULTILINE)
        return match.group(1).strip().strip("\"'") if match else ""

    def matching_skills(self, prompt: str, *, limit: int = 2) -> list[SkillMatch]:
        self.initialize()
        ranked: list[SkillMatch] = []
        user_skill_names: set[str] = set()
        for path in self.skills_dir.glob("*/SKILL.md"):
            if not self._safe_user_file(path, self.skills_dir):
                continue
            text = path.read_text(encoding="utf-8")
            name = path.parent.name
            user_skill_names.add(name)
            description = self._skill_description(text)
            score = text_similarity(prompt, description)
            if score > 0.03:
                ranked.append(SkillMatch(name=name, text=text, score=score))
        if "evidence-first-work" not in user_skill_names:
            description = self._skill_description(DEFAULT_SKILL)
            score = text_similarity(prompt, description)
            if score > 0.03:
                ranked.append(
                    SkillMatch(
                        name="evidence-first-work",
                        text=DEFAULT_SKILL,
                        score=score,
                    )
                )
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:limit]

    def build_context(self, prompt: str, *, workspace: str) -> str:
        self.initialize()
        context_policy = self.manifest().get("context", {})
        total_budget = max(1000, min(int(context_policy.get("max_chars", 9000)), 9000))
        rules_budget = max(0, int(context_policy.get("rules_chars", 2200)))
        memory_budget = max(0, int(context_policy.get("memory_chars", 2200)))
        skills_budget = max(0, int(context_policy.get("skills_chars", 3500)))
        evals_budget = max(0, int(context_policy.get("evals_chars", 900)))
        sections: list[str] = []
        core_rules = self._read_core_rules()
        if core_rules:
            sections.append(
                "## Active personal rules\n" + escape_context(core_rules[:rules_budget])
            )

        memories = self.search_memories(prompt, workspace=workspace, limit=5)
        if memories:
            lines = [f"- [{row['kind']}] {escape_context(str(row['text']))}" for row, _ in memories]
            sections.append("## Relevant learned memory\n" + "\n".join(lines)[:memory_budget])

        skills = self.matching_skills(prompt)
        skill_budget_each = skills_budget // max(1, len(skills))
        for skill in skills:
            sections.append(
                f"## Relevant skill: {skill.name}\n"
                + escape_context(skill.text[:skill_budget_each])
            )

        evals = self.relevant_evals(prompt)
        if evals:
            lines = []
            for row in evals:
                required = json.loads(row["required_any"])
                forbidden = json.loads(row["forbidden_any"])
                lines.append(
                    f"- {row['name']}: include evidence matching one of {required or ['none']}; "
                    f"avoid {forbidden or ['none']}; severity={row['severity']}."
                )
            sections.append("## Active eval obligations\n" + "\n".join(lines)[:evals_budget])

        if not sections:
            return ""
        prefix = "<personal-agent-context>\n"
        suffix = "\n</personal-agent-context>"
        body_budget = max(0, total_budget - len(prefix) - len(suffix))
        return prefix + "\n\n".join(sections)[:body_budget] + suffix

    def failed_eval_results(
        self, episode_id: int, *, minimum_severity: str | None = None
    ) -> list[EvalResult]:
        self.initialize()
        with self.connection() as connection:
            rows = list(
                connection.execute(
                    """
                    SELECT e.id AS eval_id, e.name, e.severity, r.passed, r.details
                    FROM eval_runs r
                    JOIN evals e ON e.id = r.eval_id
                    JOIN episodes ep ON ep.id = r.episode_id
                    JOIN (
                        SELECT episode_id, eval_id, MAX(response_revision) AS latest_revision
                        FROM eval_runs
                        GROUP BY episode_id, eval_id
                    ) latest
                      ON latest.episode_id = r.episode_id
                     AND latest.eval_id = r.eval_id
                     AND latest.latest_revision = r.response_revision
                    WHERE r.episode_id = ? AND ep.profile_id = ? AND r.passed = 0
                    ORDER BY r.id
                    """,
                    (episode_id, self.profile_id),
                )
            )
        severity_order = {"advisory": 0, "required": 1, "blocking": 2}
        threshold = severity_order.get(minimum_severity or "advisory", 0)
        return [
            EvalResult(
                eval_id=int(row["eval_id"]),
                name=str(row["name"]),
                passed=bool(row["passed"]),
                details=str(row["details"]),
                severity=str(row["severity"]),
            )
            for row in rows
            if severity_order.get(str(row["severity"]), 0) >= threshold
        ]

    def status(self) -> dict[str, Any]:
        self.initialize()
        with self.connection() as connection:
            counts = {
                "episodes": connection.execute(
                    "SELECT COUNT(*) FROM episodes WHERE profile_id = ?",
                    (self.profile_id,),
                ).fetchone()[0],
                "active_memories": connection.execute(
                    """
                    SELECT COUNT(*) FROM memories
                    WHERE profile_id = ? AND status = 'active'
                    """,
                    (self.profile_id,),
                ).fetchone()[0],
                "pending_candidates": connection.execute(
                    """
                    SELECT COUNT(*) FROM candidates
                    WHERE profile_id = ? AND status = 'pending'
                    """,
                    (self.profile_id,),
                ).fetchone()[0],
                "evals": connection.execute(
                    """
                    SELECT COUNT(*) FROM evals
                    WHERE profile_id = ? AND enabled = 1
                    """,
                    (self.profile_id,),
                ).fetchone()[0],
                "failed_eval_runs": connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM eval_runs r
                    JOIN episodes e ON e.id = r.episode_id
                    WHERE e.profile_id = ? AND r.passed = 0
                    """,
                    (self.profile_id,),
                ).fetchone()[0],
            }
            schema_version = current_version(connection)
        return {
            "home": str(self.home),
            "profile_id": self.profile_id,
            "database": str(self.db_path),
            "memory_directory": str(self.memory_dir),
            "layout_version": 1,
            "data_schema_version": schema_version,
            "active_generation": self.layout.active_generation_id(),
            **counts,
        }
