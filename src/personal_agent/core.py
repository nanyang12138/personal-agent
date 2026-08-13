from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = 1

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
    r"\b(?:api[_ -]?key|password|passwd|secret|token)\b\s*[:=]",
    r"\b[A-Za-z0-9_\-]{28,}\b",
    "公司内部",
    "客户机密",
    "保密信息",
    "confidential",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_home() -> Path:
    configured = os.environ.get("PERSONAL_AGENT_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".personal-agent").resolve()


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
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in SENSITIVE_PATTERNS)


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


class PersonalAgentStore:
    def __init__(self, home: Path | str | None = None) -> None:
        self.home = Path(home).expanduser().resolve() if home else default_home()
        self.package_dir = self.home / "package"
        self.rules_dir = self.package_dir / "rules"
        self.skills_dir = self.package_dir / "skills"
        self.evals_dir = self.package_dir / "evals"
        self.memory_dir = self.home / "memory"
        self.db_path = self.home / "state.db"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
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

    def initialize(self) -> None:
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.evals_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = self.package_dir / "manifest.json"
        if not manifest_path.exists():
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "package_version": "0.1.0",
                        "learning": {
                            "capture_episodes": True,
                            "stage_corrections": True,
                            "auto_promote_after": 3,
                            "auto_promote_kinds": ["preference", "lesson"],
                            "never_auto_promote": ["rule", "skill", "permission"],
                        },
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

        core_rules = self.rules_dir / "core.md"
        if not core_rules.exists():
            core_rules.write_text(DEFAULT_CORE_RULES, encoding="utf-8")

        skill_dir = self.skills_dir / "evidence-first-work"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        if not skill_path.exists():
            skill_path.write_text(DEFAULT_SKILL, encoding="utf-8")

        memory_index = self.memory_dir / "MEMORY.md"
        if not memory_index.exists():
            memory_index.write_text(
                "# Personal Agent Memory\n\n"
                "Claude Code may maintain this directory through Auto Memory. "
                "Do not store secrets or employer-confidential content here.\n",
                encoding="utf-8",
            )

        with self.connection() as connection:
            self._create_schema(connection)
            for eval_definition in DEFAULT_EVALS:
                self._insert_eval_if_missing(connection, eval_definition)

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                prompt_id TEXT,
                workspace TEXT NOT NULL,
                user_prompt TEXT NOT NULL,
                assistant_response TEXT,
                status TEXT NOT NULL DEFAULT 'started',
                engine TEXT NOT NULL DEFAULT 'claude-code',
                model TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_prompt_id
                ON episodes(prompt_id)
                WHERE prompt_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_episode_session
                ON episodes(session_id, id DESC);

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'global',
                confidence REAL NOT NULL DEFAULT 1.0,
                status TEXT NOT NULL DEFAULT 'active',
                source TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT,
                source_episode_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(source_episode_id) REFERENCES episodes(id)
            );

            CREATE INDEX IF NOT EXISTS idx_candidate_status
                ON candidates(status, kind);

            CREATE TABLE IF NOT EXISTS evals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                query_terms TEXT NOT NULL,
                required_any TEXT NOT NULL,
                forbidden_any TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS eval_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL,
                eval_id INTEGER NOT NULL,
                passed INTEGER NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(episode_id) REFERENCES episodes(id),
                FOREIGN KEY(eval_id) REFERENCES evals(id)
            );
            """
        )

    def _insert_eval_if_missing(
        self, connection: sqlite3.Connection, definition: dict[str, Any]
    ) -> None:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO evals (
                name, query_terms, required_any, forbidden_any, enabled, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO NOTHING
            """,
            (
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
        return json.loads((self.package_dir / "manifest.json").read_text(encoding="utf-8"))

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
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memories (
                    kind, text, scope, confidence, status, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (kind, text.strip(), scope, confidence, source, now, now),
            )
            return int(cursor.lastrowid)

    def list_memories(self, *, status: str = "active") -> list[sqlite3.Row]:
        self.initialize()
        with self.connection() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM memories WHERE status = ? ORDER BY updated_at DESC, id DESC",
                    (status,),
                )
            )

    def search_memories(
        self, query: str, *, workspace: str | None = None, limit: int = 5
    ) -> list[tuple[sqlite3.Row, float]]:
        candidates = self.list_memories()
        ranked: list[tuple[sqlite3.Row, float]] = []
        for row in candidates:
            if row["scope"] not in ("global", workspace):
                continue
            similarity = text_similarity(query, row["text"])
            if row["kind"] in ("preference", "rule"):
                similarity += 0.15
            similarity *= float(row["confidence"])
            if similarity > 0.02:
                ranked.append((row, similarity))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked[:limit]

    def start_turn(
        self,
        *,
        session_id: str,
        prompt_id: str | None,
        workspace: str,
        user_prompt: str,
        engine: str = "claude-code",
        model: str | None = None,
    ) -> int:
        self.initialize()
        now = utc_now()
        with self.connection() as connection:
            if prompt_id:
                existing = connection.execute(
                    "SELECT id FROM episodes WHERE prompt_id = ?", (prompt_id,)
                ).fetchone()
                if existing:
                    return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO episodes (
                    session_id, prompt_id, workspace, user_prompt, engine, model, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, prompt_id, workspace, user_prompt, engine, model, now),
            )
            return int(cursor.lastrowid)

    def latest_completed_episode(self, session_id: str) -> sqlite3.Row | None:
        self.initialize()
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM episodes
                WHERE session_id = ? AND status = 'completed'
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()

    def finish_turn(
        self,
        *,
        session_id: str,
        assistant_response: str,
        revise_last: bool = False,
    ) -> int | None:
        self.initialize()
        with self.connection() as connection:
            episode = connection.execute(
                """
                SELECT id FROM episodes
                WHERE session_id = ? AND status = 'started'
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if not episode and revise_last:
                episode = connection.execute(
                    """
                    SELECT id FROM episodes
                    WHERE session_id = ? AND status = 'completed'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
            if not episode:
                return None
            episode_id = int(episode["id"])
            connection.execute(
                "DELETE FROM eval_runs WHERE episode_id = ?",
                (episode_id,),
            )
            connection.execute(
                """
                UPDATE episodes
                SET assistant_response = ?, status = 'completed', completed_at = ?
                WHERE id = ?
                """,
                (assistant_response, utc_now(), episode_id),
            )
        self.run_evals(episode_id)
        return episode_id

    def list_episodes(self, *, limit: int = 20) -> list[sqlite3.Row]:
        self.initialize()
        with self.connection() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,)
                )
            )

    def stage_feedback_from_prompt(self, *, session_id: str, prompt: str) -> int | None:
        kind = classify_feedback(prompt)
        if not kind or looks_sensitive(prompt):
            return None
        previous = self.latest_completed_episode(session_id)
        source_episode_id = int(previous["id"]) if previous else None
        return self.stage_candidate(
            text=prompt.strip(),
            kind=kind,
            reason="Detected explicit user correction or preference in a subsequent turn.",
            source_episode_id=source_episode_id,
        )

    def stage_candidate(
        self,
        *,
        text: str,
        kind: str,
        reason: str,
        source_episode_id: int | None = None,
    ) -> int:
        self.initialize()
        if looks_sensitive(text):
            raise ValueError("Refusing to stage sensitive or confidential text.")
        fingerprint = normalize_text(text)
        now = utc_now()
        with self.connection() as connection:
            pending = list(
                connection.execute(
                    "SELECT * FROM candidates WHERE status = 'pending' AND kind = ?",
                    (kind,),
                )
            )
            best = max(
                ((row, text_similarity(text, row["text"])) for row in pending),
                key=lambda item: item[1],
                default=(None, 0.0),
            )
            if best[0] is not None and best[1] >= 0.72:
                candidate_id = int(best[0]["id"])
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
                        kind, text, fingerprint, occurrences, status, reason,
                        source_episode_id, created_at, updated_at
                    )
                    VALUES (?, ?, ?, 1, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        kind,
                        text,
                        fingerprint,
                        reason,
                        source_episode_id,
                        now,
                        now,
                    ),
                )
                candidate_id = int(cursor.lastrowid)

            candidate = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()

        learning = self.manifest().get("learning", {})
        threshold = int(learning.get("auto_promote_after", 0) or 0)
        allowed = set(learning.get("auto_promote_kinds", []))
        if threshold and candidate and candidate["occurrences"] >= threshold and kind in allowed:
            self.approve_candidate(candidate_id, source="auto-repeated-feedback")
        return candidate_id

    def list_candidates(self, *, status: str = "pending") -> list[sqlite3.Row]:
        self.initialize()
        with self.connection() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM candidates WHERE status = ? ORDER BY updated_at DESC",
                    (status,),
                )
            )

    def approve_candidate(self, candidate_id: int, *, source: str = "approved") -> int:
        self.initialize()
        with self.connection() as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if not candidate:
                raise ValueError(f"Candidate {candidate_id} does not exist.")
            if candidate["status"] == "approved":
                existing = connection.execute(
                    "SELECT id FROM memories WHERE source LIKE ?",
                    (f"candidate:{candidate_id}:%",),
                ).fetchone()
                if existing:
                    return int(existing["id"])
            if looks_sensitive(candidate["text"]):
                raise ValueError("Refusing to promote sensitive or confidential text.")
            now = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO memories (
                    kind, text, scope, confidence, status, source, created_at, updated_at
                )
                VALUES (?, ?, 'global', ?, 'active', ?, ?, ?)
                """,
                (
                    candidate["kind"],
                    candidate["text"],
                    min(0.95, 0.6 + 0.1 * int(candidate["occurrences"])),
                    f"candidate:{candidate_id}:{source}",
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
        with self.connection() as connection:
            connection.execute(
                "UPDATE candidates SET status = 'rejected', updated_at = ? WHERE id = ?",
                (utc_now(), candidate_id),
            )

    def add_eval(
        self,
        *,
        name: str,
        query_terms: list[str],
        required_any: list[str],
        forbidden_any: list[str],
        enabled: bool = True,
    ) -> int:
        self.initialize()
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO evals (
                    name, query_terms, required_any, forbidden_any, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    query_terms = excluded.query_terms,
                    required_any = excluded.required_any,
                    forbidden_any = excluded.forbidden_any,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    name,
                    json.dumps(query_terms, ensure_ascii=False),
                    json.dumps(required_any, ensure_ascii=False),
                    json.dumps(forbidden_any, ensure_ascii=False),
                    int(enabled),
                    now,
                    now,
                ),
            )
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            row = connection.execute("SELECT id FROM evals WHERE name = ?", (name,)).fetchone()
            return int(row["id"])

    def list_evals(self) -> list[sqlite3.Row]:
        self.initialize()
        with self.connection() as connection:
            return list(connection.execute("SELECT * FROM evals ORDER BY name"))

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
        with self.connection() as connection:
            episode = connection.execute(
                "SELECT * FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
            if not episode:
                raise ValueError(f"Episode {episode_id} does not exist.")
            response = episode["assistant_response"] or ""
            results: list[EvalResult] = []
            for row in self.relevant_evals(episode["user_prompt"]):
                required = json.loads(row["required_any"])
                forbidden = json.loads(row["forbidden_any"])
                missing_required = bool(required) and not contains_any(response, required)
                present_forbidden = [item for item in forbidden if contains_any(response, [item])]
                passed = not missing_required and not present_forbidden
                details: list[str] = []
                if missing_required:
                    details.append(f"missing one of: {required}")
                if present_forbidden:
                    details.append(f"contains forbidden: {present_forbidden}")
                if not details:
                    details.append("passed")
                result = EvalResult(
                    eval_id=int(row["id"]),
                    name=str(row["name"]),
                    passed=passed,
                    details="; ".join(details),
                )
                results.append(result)
                connection.execute(
                    """
                    INSERT INTO eval_runs (episode_id, eval_id, passed, details, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        episode_id,
                        result.eval_id,
                        int(result.passed),
                        result.details,
                        utc_now(),
                    ),
                )
            return results

    def _read_core_rules(self) -> str:
        rules: list[str] = []
        for path in sorted(self.rules_dir.glob("*.md")):
            rules.append(path.read_text(encoding="utf-8").strip())
        return "\n\n".join(rules)

    def _skill_description(self, text: str) -> str:
        match = re.search(r"^description:\s*(.+)$", text, flags=re.MULTILINE)
        return match.group(1).strip().strip("\"'") if match else ""

    def matching_skills(self, prompt: str, *, limit: int = 2) -> list[tuple[Path, float]]:
        self.initialize()
        ranked: list[tuple[Path, float]] = []
        for path in self.skills_dir.glob("*/SKILL.md"):
            text = path.read_text(encoding="utf-8")
            description = self._skill_description(text)
            score = text_similarity(prompt, description)
            if score > 0.03:
                ranked.append((path, score))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked[:limit]

    def build_context(self, prompt: str, *, workspace: str) -> str:
        self.initialize()
        sections: list[str] = []
        core_rules = self._read_core_rules()
        if core_rules:
            sections.append("## Active personal rules\n" + core_rules[:5000])

        memories = self.search_memories(prompt, workspace=workspace, limit=5)
        if memories:
            lines = [f"- [{row['kind']}] {row['text']}" for row, _ in memories]
            sections.append("## Relevant learned memory\n" + "\n".join(lines))

        skills = self.matching_skills(prompt)
        for path, _ in skills:
            sections.append(
                f"## Relevant skill: {path.parent.name}\n"
                + path.read_text(encoding="utf-8")[:7000]
            )

        evals = self.relevant_evals(prompt)
        if evals:
            lines = []
            for row in evals:
                required = json.loads(row["required_any"])
                forbidden = json.loads(row["forbidden_any"])
                lines.append(
                    f"- {row['name']}: include evidence matching one of {required or ['none']}; "
                    f"avoid {forbidden or ['none']}."
                )
            sections.append("## Active eval obligations\n" + "\n".join(lines))

        if not sections:
            return ""
        return (
            "<personal-agent-context>\n"
            + "\n\n".join(sections)
            + "\n</personal-agent-context>"
        )

    def failed_eval_results(self, episode_id: int) -> list[EvalResult]:
        self.initialize()
        with self.connection() as connection:
            rows = list(
                connection.execute(
                    """
                    SELECT e.id AS eval_id, e.name, r.passed, r.details
                    FROM eval_runs r
                    JOIN evals e ON e.id = r.eval_id
                    WHERE r.episode_id = ? AND r.passed = 0
                    ORDER BY r.id
                    """,
                    (episode_id,),
                )
            )
        return [
            EvalResult(
                eval_id=int(row["eval_id"]),
                name=str(row["name"]),
                passed=bool(row["passed"]),
                details=str(row["details"]),
            )
            for row in rows
        ]

    def status(self) -> dict[str, Any]:
        self.initialize()
        with self.connection() as connection:
            counts = {
                "episodes": connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
                "active_memories": connection.execute(
                    "SELECT COUNT(*) FROM memories WHERE status = 'active'"
                ).fetchone()[0],
                "pending_candidates": connection.execute(
                    "SELECT COUNT(*) FROM candidates WHERE status = 'pending'"
                ).fetchone()[0],
                "evals": connection.execute(
                    "SELECT COUNT(*) FROM evals WHERE enabled = 1"
                ).fetchone()[0],
                "failed_eval_runs": connection.execute(
                    "SELECT COUNT(*) FROM eval_runs WHERE passed = 0"
                ).fetchone()[0],
            }
        return {
            "home": str(self.home),
            "database": str(self.db_path),
            "memory_directory": str(self.memory_dir),
            **counts,
        }
