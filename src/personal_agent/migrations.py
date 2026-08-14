from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from .layout import utc_now

CURRENT_SCHEMA_VERSION = 4


def execute_statements(connection: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                connection.execute(sql)
    if statement.strip():
        raise ValueError("Incomplete SQL migration statement.")


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(connection, table):
        return set()
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def add_column_if_missing(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if column not in column_names(connection, table):
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')


def create_latest_schema(connection: sqlite3.Connection) -> None:
    execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            migration_id TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL,
            app_version TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL DEFAULT 'default',
            workspace_id TEXT NOT NULL DEFAULT 'global',
            session_id TEXT NOT NULL,
            prompt_id TEXT,
            prompt_digest TEXT NOT NULL DEFAULT '',
            response_digest TEXT NOT NULL DEFAULT '',
            user_prompt TEXT,
            assistant_response TEXT,
            prompt_preview TEXT,
            response_preview TEXT,
            capture_mode TEXT NOT NULL DEFAULT 'metadata',
            applicable_eval_ids TEXT NOT NULL DEFAULT '[]',
            response_revision INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'started',
            engine TEXT NOT NULL DEFAULT 'claude-code',
            model TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS episode_responses (
            episode_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            response_digest TEXT NOT NULL,
            response_text TEXT,
            response_preview TEXT,
            capture_mode TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(episode_id, revision),
            FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL DEFAULT 'default',
            workspace_id TEXT NOT NULL DEFAULT 'global',
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',
            authority TEXT NOT NULL DEFAULT 'user',
            confidence REAL NOT NULL DEFAULT 1.0,
            status TEXT NOT NULL DEFAULT 'active',
            source TEXT,
            source_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL DEFAULT 'default',
            workspace_id TEXT NOT NULL DEFAULT 'global',
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            evidence_key TEXT NOT NULL DEFAULT '',
            polarity TEXT NOT NULL DEFAULT 'unknown',
            occurrences INTEGER NOT NULL DEFAULT 1,
            distinct_episode_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT,
            source_episode_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(source_episode_id) REFERENCES episodes(id)
        );
        CREATE TABLE IF NOT EXISTS candidate_evidence (
            candidate_id INTEGER NOT NULL,
            episode_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(candidate_id, episode_id),
            FOREIGN KEY(candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
            FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS evals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL DEFAULT 'default',
            name TEXT NOT NULL,
            format_version INTEGER NOT NULL DEFAULT 1,
            query_terms TEXT NOT NULL,
            required_any TEXT NOT NULL,
            forbidden_any TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'advisory',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(profile_id, name)
        );

        CREATE TABLE IF NOT EXISTS eval_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id INTEGER NOT NULL,
            eval_id INTEGER NOT NULL,
            response_revision INTEGER NOT NULL DEFAULT 0,
            passed INTEGER NOT NULL,
            details TEXT NOT NULL,
            evidence TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
            FOREIGN KEY(eval_id) REFERENCES evals(id) ON DELETE CASCADE,
            UNIQUE(episode_id, eval_id, response_revision)
        );
        """,
    )


def create_common_indexes(connection: sqlite3.Connection) -> None:
    execute_statements(
        connection,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_prompt_id_v2
            ON episodes(profile_id, engine, prompt_id)
            WHERE prompt_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_episode_session_v2
            ON episodes(profile_id, session_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_scope_v2
            ON memories(profile_id, workspace_id, status, kind);
        """,
    )


def rebuild_legacy_episodes(connection: sqlite3.Connection) -> None:
    if "workspace" not in column_names(connection, "episodes"):
        return
    connection.execute("PRAGMA legacy_alter_table=ON")
    connection.execute("ALTER TABLE episodes RENAME TO episodes_legacy_v0")
    create_latest_schema(connection)
    connection.execute(
        """
        INSERT INTO episodes (
            id, profile_id, workspace_id, session_id, prompt_id,
            prompt_digest, response_digest, user_prompt, assistant_response,
            prompt_preview, response_preview, capture_mode,
            applicable_eval_ids, response_revision, status, engine, model,
            created_at, updated_at, completed_at
        )
        SELECT id, 'default', 'legacy', session_id, prompt_id,
               '', '', user_prompt, assistant_response,
               NULL, NULL, 'legacy-full',
               '[]', CASE WHEN assistant_response IS NULL THEN 0 ELSE 1 END,
               status, engine, model, created_at,
               COALESCE(completed_at, created_at), completed_at
        FROM episodes_legacy_v0
        """
    )
    connection.execute("DROP TABLE episodes_legacy_v0")
    connection.execute("PRAGMA legacy_alter_table=OFF")


def rebuild_legacy_evals(connection: sqlite3.Connection) -> None:
    if "profile_id" in column_names(connection, "evals"):
        return
    connection.execute("PRAGMA legacy_alter_table=ON")
    connection.execute("ALTER TABLE evals RENAME TO evals_legacy_v0")
    create_latest_schema(connection)
    connection.execute(
        """
        INSERT INTO evals (
            id, profile_id, name, format_version, query_terms,
            required_any, forbidden_any, severity, enabled,
            created_at, updated_at
        )
        SELECT id, 'default', name, 1, query_terms,
               required_any, forbidden_any, 'advisory', enabled,
               created_at, updated_at
        FROM evals_legacy_v0
        """
    )
    connection.execute("DROP TABLE evals_legacy_v0")
    connection.execute("PRAGMA legacy_alter_table=OFF")


def rebuild_evals_with_global_unique_name(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "evals"):
        return
    has_global_unique = False
    for index in connection.execute("PRAGMA index_list('evals')"):
        if not int(index[2]):
            continue
        columns = [str(row[2]) for row in connection.execute(f"PRAGMA index_info('{index[1]}')")]
        if columns == ["name"]:
            has_global_unique = True
            break
    if not has_global_unique:
        return
    connection.execute("PRAGMA legacy_alter_table=ON")
    connection.execute("ALTER TABLE evals RENAME TO evals_global_unique_legacy")
    create_latest_schema(connection)
    connection.execute(
        """
        INSERT INTO evals (
            id, profile_id, name, format_version, query_terms,
            required_any, forbidden_any, severity, enabled,
            created_at, updated_at
        )
        SELECT id, profile_id, name, format_version, query_terms,
               required_any, forbidden_any, severity, enabled,
               created_at, updated_at
        FROM evals_global_unique_legacy
        """
    )
    connection.execute("DROP TABLE evals_global_unique_legacy")
    connection.execute("PRAGMA legacy_alter_table=OFF")


def migration_0001_namespaces_and_capture(connection: sqlite3.Connection) -> None:
    create_latest_schema(connection)
    rebuild_legacy_episodes(connection)
    rebuild_legacy_evals(connection)
    now = utc_now()

    for column, definition in (
        ("profile_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'global'"),
        ("prompt_digest", "TEXT NOT NULL DEFAULT ''"),
        ("response_digest", "TEXT NOT NULL DEFAULT ''"),
        ("prompt_preview", "TEXT"),
        ("response_preview", "TEXT"),
        ("capture_mode", "TEXT NOT NULL DEFAULT 'legacy-full'"),
        ("response_revision", "INTEGER NOT NULL DEFAULT 0"),
        ("updated_at", f"TEXT NOT NULL DEFAULT '{now}'"),
    ):
        add_column_if_missing(connection, "episodes", column, definition)

    for column, definition in (
        ("profile_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'global'"),
        ("authority", "TEXT NOT NULL DEFAULT 'user'"),
        ("source_hash", "TEXT"),
    ):
        add_column_if_missing(connection, "memories", column, definition)

    for column, definition in (
        ("profile_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'global'"),
        ("evidence_key", "TEXT NOT NULL DEFAULT ''"),
        ("polarity", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("distinct_episode_count", "INTEGER NOT NULL DEFAULT 1"),
    ):
        add_column_if_missing(connection, "candidates", column, definition)

    for column, definition in (
        ("profile_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("format_version", "INTEGER NOT NULL DEFAULT 1"),
        ("severity", "TEXT NOT NULL DEFAULT 'advisory'"),
    ):
        add_column_if_missing(connection, "evals", column, definition)

    for column, definition in (
        ("response_revision", "INTEGER NOT NULL DEFAULT 0"),
        ("evidence", "TEXT"),
    ):
        add_column_if_missing(connection, "eval_runs", column, definition)
    create_common_indexes(connection)


def migration_0002_constraints_and_evidence(connection: sqlite3.Connection) -> None:
    create_latest_schema(connection)
    create_common_indexes(connection)
    connection.execute(
        """
        DELETE FROM eval_runs
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM eval_runs
            GROUP BY episode_id, eval_id, response_revision
        )
        """
    )
    connection.execute(
        """
        UPDATE candidates
        SET evidence_key = CASE
            WHEN evidence_key = '' THEN fingerprint
            ELSE evidence_key
        END
        """
    )
    duplicates = list(
        connection.execute(
            """
            SELECT profile_id, workspace_id, kind, evidence_key, MIN(id) AS keep_id
            FROM candidates
            WHERE status = 'pending'
            GROUP BY profile_id, workspace_id, kind, evidence_key
            HAVING COUNT(*) > 1
            """
        )
    )
    for duplicate in duplicates:
        rows = list(
            connection.execute(
                """
                SELECT id, occurrences, distinct_episode_count
                FROM candidates
                WHERE profile_id = ? AND workspace_id = ? AND kind = ?
                  AND evidence_key = ? AND status = 'pending'
                ORDER BY id
                """,
                (
                    duplicate["profile_id"],
                    duplicate["workspace_id"],
                    duplicate["kind"],
                    duplicate["evidence_key"],
                ),
            )
        )
        keep_id = int(duplicate["keep_id"])
        total_occurrences = sum(int(row["occurrences"]) for row in rows)
        total_distinct = sum(int(row["distinct_episode_count"]) for row in rows)
        connection.execute(
            """
            UPDATE candidates
            SET occurrences = ?, distinct_episode_count = ?
            WHERE id = ?
            """,
            (total_occurrences, total_distinct, keep_id),
        )
        connection.executemany(
            "UPDATE candidates SET status = 'superseded' WHERE id = ?",
            [(int(row["id"]),) for row in rows if int(row["id"]) != keep_id],
        )

    execute_statements(
        connection,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_pending_key
            ON candidates(profile_id, workspace_id, kind, evidence_key)
            WHERE status = 'pending';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_profile_name
            ON evals(profile_id, name);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_run_revision
            ON eval_runs(episode_id, eval_id, response_revision);
        """,
    )


def migration_0003_eval_selection_and_response_history(
    connection: sqlite3.Connection,
) -> None:
    create_latest_schema(connection)
    rebuild_evals_with_global_unique_name(connection)
    add_column_if_missing(
        connection,
        "episodes",
        "applicable_eval_ids",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO episode_responses (
            episode_id, revision, response_digest, response_text,
            response_preview, capture_mode, created_at
        )
        SELECT id, response_revision, response_digest, assistant_response,
               response_preview, capture_mode, COALESCE(completed_at, updated_at)
        FROM episodes
        WHERE response_revision > 0
        """
    )


def migration_0004_rebuild_global_eval_constraint(
    connection: sqlite3.Connection,
) -> None:
    rebuild_evals_with_global_unique_name(connection)
    create_common_indexes(connection)


Migration = tuple[int, str, str, Callable[[sqlite3.Connection], None]]

MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "0001_namespaces_and_capture",
        "sha256:9a23c8d432cd78c24776cf1ff88dd7e28e2cd4bbf7d93687c78b0dbcf183ae1e",
        migration_0001_namespaces_and_capture,
    ),
    (
        2,
        "0002_constraints_and_evidence",
        "sha256:e4a6bea3d98b0d004b81b4a263e28a80973596691a524f04257ef5ac89518e7d",
        migration_0002_constraints_and_evidence,
    ),
    (
        3,
        "0003_eval_selection_and_response_history",
        "sha256:4ea40e18cd51278d580c8cf533f50c295f933648042e8441f43e100a1bc31fa5",
        migration_0003_eval_selection_and_response_history,
    ),
    (
        4,
        "0004_rebuild_global_eval_constraint",
        "sha256:b4fcb7cb0433a4666ec4e8fdfe3566d7aac563f9683cf95b5c6243e6384d8c6e",
        migration_0004_rebuild_global_eval_constraint,
    ),
)


def current_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(database: Path, *, app_version: str) -> int:
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        version = current_version(connection)
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema {version} is newer than supported {CURRENT_SCHEMA_VERSION}."
            )
        connection.execute("PRAGMA foreign_keys=OFF")
        for target, migration_id, checksum, migration in MIGRATIONS:
            if target <= version:
                continue
            connection.execute("BEGIN IMMEDIATE")
            try:
                migration(connection)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO schema_migrations (
                        version, migration_id, checksum, app_version, applied_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (target, migration_id, checksum, app_version, utc_now()),
                )
                connection.execute(f"PRAGMA user_version={target}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            version = target
        connection.execute("PRAGMA foreign_keys=ON")
        verify_database(connection)
        return version
    finally:
        connection.close()


def verify_database(connection: sqlite3.Connection) -> None:
    quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
    if quick_check != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {quick_check}")
    foreign_key_errors = list(connection.execute("PRAGMA foreign_key_check"))
    if foreign_key_errors:
        raise RuntimeError(
            f"SQLite foreign_key_check failed with {len(foreign_key_errors)} errors."
        )
    verify_migration_history(connection)


def verify_migration_history(connection: sqlite3.Connection) -> None:
    version = current_version(connection)
    if version == 0:
        return
    if not table_exists(connection, "schema_migrations"):
        raise RuntimeError("schema_migrations table is missing.")
    expected = {
        target: (migration_id, checksum)
        for target, migration_id, checksum, _ in MIGRATIONS
        if target <= version
    }
    rows = {
        int(row[0]): (str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT version, migration_id, checksum FROM schema_migrations"
        )
    }
    for target, values in expected.items():
        if rows.get(target) != values:
            raise RuntimeError(
                f"Migration history mismatch at version {target}: "
                f"expected {values}, found {rows.get(target)}"
            )
