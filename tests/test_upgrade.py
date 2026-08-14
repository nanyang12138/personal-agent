import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from personal_agent.core import PersonalAgentStore
from personal_agent.layout import sha256_file
from personal_agent.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS


def enable_capture(store: PersonalAgentStore) -> None:
    manifest = store.manifest()
    manifest["capture"].update(
        {
            "episodes": True,
            "mode": "redacted",
            "preview_chars": 1000,
        }
    )
    store.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class UpgradeAndConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "agent-home"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_legacy_database_is_preserved_and_migrated_to_generation(self) -> None:
        self.home.mkdir(parents=True)
        legacy = self.home / "state.db"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE episodes (
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
            INSERT INTO episodes (
                session_id, prompt_id, workspace, user_prompt, status, created_at
            ) VALUES ('legacy-session', 'legacy-prompt', 'C:\\repo', 'legacy task',
                      'started', '2026-01-01T00:00:00Z');
            CREATE TABLE memories (
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
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT,
                source_episode_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE evals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                query_terms TEXT NOT NULL,
                required_any TEXT NOT NULL,
                forbidden_any TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE eval_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL,
                eval_id INTEGER NOT NULL,
                passed INTEGER NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.commit()
        connection.close()

        store = PersonalAgentStore(self.home)
        store.initialize()

        self.assertTrue(legacy.exists())
        self.assertNotEqual(store.db_path, legacy)
        self.assertEqual(store.status()["data_schema_version"], 4)
        episodes = store.list_episodes()
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["prompt_id"], "legacy-prompt")
        self.assertTrue(any(self.home.joinpath("backups").iterdir()))

        enable_capture(store)
        created = store.start_turn(
            session_id="new-session",
            prompt_id="new-prompt",
            workspace="C:\\new-repo",
            user_prompt="new task",
        )
        self.assertIsNotNone(created)

        second_profile = PersonalAgentStore(self.home, profile_id="Second")
        second_profile.initialize()
        self.assertGreaterEqual(len(second_profile.list_evals()), 2)

    def test_same_prompt_id_is_idempotent_under_concurrency(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        enable_capture(store)

        def write() -> int | None:
            return store.start_turn(
                session_id="session",
                prompt_id="same-prompt",
                workspace="C:\\repo",
                user_prompt="implement and verify",
            )

        with ThreadPoolExecutor(max_workers=16) as executor:
            identifiers = list(executor.map(lambda _: write(), range(32)))

        self.assertEqual(len(set(identifiers)), 1)
        self.assertEqual(len(store.list_episodes()), 1)

    def test_candidate_staging_is_idempotent_under_concurrency(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()

        def write() -> int:
            return store.stage_candidate(
                text="以后先检查原始日志。",
                kind="lesson",
                reason="concurrency-test",
            )

        with ThreadPoolExecutor(max_workers=16) as executor:
            identifiers = list(executor.map(lambda _: write(), range(32)))

        self.assertEqual(len(set(identifiers)), 1)
        candidates = store.list_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["occurrences"], 32)
        self.assertEqual(len(store.list_memories()), 0)

    def test_backup_restore_switches_to_new_generation_without_overwrite(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        store.add_memory("first memory", kind="lesson")
        with store.layout.locked():
            backup = store.layout.snapshot_active(label="before-second-memory")
        store.add_memory("second memory", kind="lesson")
        generation_before_restore = store.layout.active_generation_id()

        restored_generation = store.layout.restore_backup(backup)
        self.assertNotEqual(restored_generation, generation_before_restore)

        restored = PersonalAgentStore(self.home)
        restored.initialize()
        memories = restored.list_memories()
        self.assertEqual([row["text"] for row in memories], ["first memory"])

    def test_generation_cutover_blocks_writer_and_reopens_active_database(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        store.add_memory("before cutover", kind="lesson")

        with ThreadPoolExecutor(max_workers=1) as executor:
            with store.layout.locked():
                generation, _ = store.layout.clone_active_to_staging()
                store.layout.mark_generation_ready(
                    generation,
                    app_version="test",
                    data_schema_version=CURRENT_SCHEMA_VERSION,
                )
                future = executor.submit(
                    store.add_memory,
                    "late write",
                    kind="lesson",
                )
                time.sleep(0.2)
                self.assertFalse(future.done())
                store.layout.switch(generation, reason="test-cutover")
            future.result(timeout=5)

        reopened = PersonalAgentStore(self.home)
        reopened.initialize()
        self.assertEqual(
            {row["text"] for row in reopened.list_memories()},
            {"before cutover", "late write"},
        )

    def test_missing_active_pointer_recovers_latest_ready_generation(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        store.add_memory("recover me", kind="lesson")
        store.layout.active_pointer.unlink()

        reopened = PersonalAgentStore(self.home)
        reopened.initialize()
        self.assertEqual(
            [row["text"] for row in reopened.list_memories()],
            ["recover me"],
        )

    def test_restore_rejects_future_schema_even_with_matching_checksum(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        with store.layout.locked():
            backup = store.layout.snapshot_active(label="future-schema")
        database = backup / "state.db"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA user_version=999")
        connection.close()
        manifest_path = backup / "backup.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["data_schema_version"] = 999
        manifest["database_sha256"] = sha256_file(database)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(ValueError):
            store.layout.restore_backup(
                backup,
                max_schema_version=CURRENT_SCHEMA_VERSION,
            )

    def test_pointer_recovery_skips_future_schema_generation(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        store.add_memory("compatible data", kind="lesson")
        with store.layout.locked():
            future_generation, future_database = store.layout.clone_active_to_staging()
            store.layout.mark_generation_ready(
                future_generation,
                app_version="future",
                data_schema_version=999,
            )
            connection = sqlite3.connect(future_database)
            connection.execute("PRAGMA user_version=999")
            connection.close()
        store.layout.active_pointer.unlink()

        reopened = PersonalAgentStore(self.home)
        reopened.initialize()
        self.assertNotEqual(
            reopened.layout.active_generation_id(),
            future_generation,
        )
        self.assertEqual(
            [row["text"] for row in reopened.list_memories()],
            ["compatible data"],
        )

    def test_pointer_recovery_prefers_restored_generation_sequence(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        store.add_memory("keep", kind="lesson")
        with store.layout.locked():
            backup = store.layout.snapshot_active(label="before-extra")
        store.add_memory("remove-by-restore", kind="lesson")
        store.layout.restore_backup(
            backup,
            max_schema_version=CURRENT_SCHEMA_VERSION,
        )
        store.layout.active_pointer.unlink()

        reopened = PersonalAgentStore(self.home)
        reopened.initialize()
        self.assertEqual(
            [row["text"] for row in reopened.list_memories()],
            ["keep"],
        )

    def test_future_schema_is_refused(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        database = store.db_path
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA user_version=999")
        connection.close()

        reopened = PersonalAgentStore(self.home)
        with self.assertRaises(RuntimeError):
            reopened.initialize()

    def test_profile_ids_are_case_normalized(self) -> None:
        first = PersonalAgentStore(self.home, profile_id="Work")
        second = PersonalAgentStore(self.home, profile_id="work")
        self.assertEqual(first.profile_id, "work")
        self.assertEqual(first.profile_dir, second.profile_dir)

    def test_legacy_user_assets_are_copied_only_once(self) -> None:
        legacy_rule = self.home / "package" / "rules" / "old.md"
        legacy_rule.parent.mkdir(parents=True)
        legacy_rule.write_text("# Old rule", encoding="utf-8")

        store = PersonalAgentStore(self.home)
        store.initialize()
        copied = store.rules_dir / "old.md"
        self.assertTrue(copied.exists())
        copied.unlink()

        reopened = PersonalAgentStore(self.home)
        reopened.initialize()
        self.assertFalse(copied.exists())

    def test_migration_history_checksum_tampering_is_refused(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        connection = sqlite3.connect(store.db_path)
        connection.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
        connection.commit()
        connection.close()

        reopened = PersonalAgentStore(self.home)
        with self.assertRaises(RuntimeError):
            reopened.initialize()

    def test_v2_global_eval_unique_constraint_is_rebuilt(self) -> None:
        self.home.mkdir(parents=True)
        legacy = self.home / "state.db"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                migration_id TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL,
                app_version TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE evals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT NOT NULL DEFAULT 'default',
                name TEXT NOT NULL UNIQUE,
                format_version INTEGER NOT NULL DEFAULT 1,
                query_terms TEXT NOT NULL,
                required_any TEXT NOT NULL,
                forbidden_any TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'advisory',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            PRAGMA user_version=2;
            """
        )
        for target, migration_id, checksum, _ in MIGRATIONS[:2]:
            connection.execute(
                """
                INSERT INTO schema_migrations (
                    version, migration_id, checksum, app_version, applied_at
                ) VALUES (?, ?, ?, '0.1.0', '2026-01-01T00:00:00Z')
                """,
                (target, migration_id, checksum),
            )
        connection.commit()
        connection.close()

        first = PersonalAgentStore(self.home, profile_id="first")
        first.initialize()
        second = PersonalAgentStore(self.home, profile_id="second")
        second.initialize()
        self.assertGreaterEqual(len(first.list_evals()), 2)
        self.assertGreaterEqual(len(second.list_evals()), 2)

    def test_preexisting_v3_global_eval_constraint_runs_v4_migration(self) -> None:
        self.home.mkdir(parents=True)
        legacy = self.home / "state.db"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                migration_id TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL,
                app_version TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE evals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT NOT NULL DEFAULT 'default',
                name TEXT NOT NULL UNIQUE,
                format_version INTEGER NOT NULL DEFAULT 1,
                query_terms TEXT NOT NULL,
                required_any TEXT NOT NULL,
                forbidden_any TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'advisory',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            PRAGMA user_version=3;
            """
        )
        for target, migration_id, checksum, _ in MIGRATIONS[:3]:
            connection.execute(
                """
                INSERT INTO schema_migrations (
                    version, migration_id, checksum, app_version, applied_at
                ) VALUES (?, ?, ?, '0.1.0', '2026-01-01T00:00:00Z')
                """,
                (target, migration_id, checksum),
            )
        connection.commit()
        connection.close()

        first = PersonalAgentStore(self.home, profile_id="first")
        first.initialize()
        second = PersonalAgentStore(self.home, profile_id="second")
        second.initialize()
        self.assertGreaterEqual(len(first.list_evals()), 2)
        self.assertGreaterEqual(len(second.list_evals()), 2)


if __name__ == "__main__":
    unittest.main()
