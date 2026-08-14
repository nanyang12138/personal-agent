import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_agent.cli import (
    install_claude,
    stop_failure_hook,
    stop_hook,
    uninstall_claude,
    user_prompt_submit,
)
from personal_agent.core import PersonalAgentStore, looks_sensitive, redact_text
from personal_agent.maintenance import purge_profile


class SecurityAndInstallationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.home = self.root / "agent-home"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_capture_is_disabled_by_default(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        self.assertIsNone(
            store.start_turn(
                session_id="session",
                prompt_id="prompt",
                workspace="C:\\repo",
                user_prompt="private task",
            )
        )
        self.assertEqual(store.list_episodes(), [])

    def test_redacted_capture_does_not_store_detected_secret(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
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
        store.start_turn(
            session_id="session",
            prompt_id="prompt",
            workspace="C:\\repo",
            user_prompt="password is hunter2 and continue",
        )
        store.finish_turn(
            session_id="session",
            assistant_response="token=abc123456789012345678901234567",
        )
        episode = store.list_episodes()[0]
        self.assertIsNone(episode["user_prompt"])
        self.assertIsNone(episode["assistant_response"])
        self.assertIn("[REDACTED]", episode["prompt_preview"])
        self.assertIn("[REDACTED]", episode["response_preview"])
        self.assertNotIn("hunter2", episode["prompt_preview"])

    def test_common_secret_forms_are_detected_and_redacted(self) -> None:
        samples = [
            "client_secret=very-secret-value",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
            "AWS_SECRET_ACCESS_KEY=abcdef1234567890",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(looks_sensitive(sample))
                self.assertNotIn(sample.split("=", 1)[-1], redact_text(sample))

    def test_metadata_capture_persists_eval_selection_without_prompt_text(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        manifest = store.manifest()
        manifest["capture"].update({"episodes": True, "mode": "metadata", "preview_chars": 0})
        store.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        user_prompt_submit(
            store,
            {
                "session_id": "session",
                "prompt_id": "prompt",
                "cwd": "C:\\repo",
                "prompt": "Please fix this bug.",
            },
        )
        stop_hook(
            store,
            {
                "session_id": "session",
                "prompt_id": "prompt",
                "last_assistant_message": "I ran the tests and verified the fix.",
                "stop_hook_active": False,
            },
        )
        episode = store.list_episodes()[0]
        self.assertIsNone(episode["user_prompt"])
        self.assertNotEqual(episode["applicable_eval_ids"], "[]")
        with store.connection() as connection:
            runs = connection.execute(
                "SELECT COUNT(*) FROM eval_runs WHERE episode_id = ?",
                (episode["id"],),
            ).fetchone()[0]
        self.assertGreater(runs, 0)

    def test_stop_failure_closes_started_episode(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        manifest = store.manifest()
        manifest["capture"].update({"episodes": True, "mode": "metadata", "preview_chars": 0})
        store.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        user_prompt_submit(
            store,
            {
                "session_id": "session",
                "prompt_id": "prompt",
                "cwd": "C:\\repo",
                "prompt": "task",
            },
        )
        stop_failure_hook(
            store,
            {
                "session_id": "session",
                "prompt_id": "prompt",
                "error": "rate_limit",
            },
        )
        self.assertEqual(store.list_episodes()[0]["status"], "failed")

    def test_context_budget_preserves_boundary(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        manifest = store.manifest()
        manifest["context"]["max_chars"] = 1200
        manifest["context"]["memory_chars"] = 1000
        store.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        store.add_memory("x" * 10000, kind="lesson")
        context = store.build_context("xxxx", workspace="C:\\repo")
        self.assertLessEqual(len(context), 1200)
        self.assertTrue(context.endswith("</personal-agent-context>"))

    def test_install_dry_run_has_no_filesystem_side_effects(self) -> None:
        store = PersonalAgentStore(self.home)
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir()
        original = {"model": "test-model"}
        settings.write_text(json.dumps(original), encoding="utf-8")
        executable = self.root / "personal-agent.exe"
        executable.write_text("test", encoding="utf-8")

        with patch(
            "personal_agent.cli.claude_version",
            return_value={
                "executable": "claude",
                "version": "2.1.300",
                "supported": True,
            },
        ):
            result = install_claude(
                store,
                settings_path=settings,
                dry_run=True,
                hook_executable=str(executable),
            )

        self.assertTrue(result["dry_run"])
        self.assertFalse(self.home.exists())
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), original)

    def test_install_and_uninstall_restore_touched_settings(self) -> None:
        store = PersonalAgentStore(self.home)
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir()
        original = {
            "model": "test-model",
            "autoMemoryEnabled": False,
            "autoMemoryDirectory": "C:\\old-memory",
            "env": {
                "EXISTING_VALUE": "preserve-me",
                "PERSONAL_AGENT_HOME": "C:\\old-home",
            },
        }
        settings.write_text(json.dumps(original), encoding="utf-8")
        executable = self.root / "personal-agent.exe"
        executable.write_text("test", encoding="utf-8")

        with patch(
            "personal_agent.cli.claude_version",
            return_value={
                "executable": "claude",
                "version": "2.1.300",
                "supported": True,
            },
        ):
            install_claude(
                store,
                settings_path=settings,
                dry_run=False,
                enable_auto_memory=True,
                hook_executable=str(executable),
            )

        installed = json.loads(settings.read_text(encoding="utf-8"))
        self.assertTrue(installed["autoMemoryEnabled"])
        self.assertIn("UserPromptSubmit", installed["hooks"])
        self.assertEqual(installed["env"]["EXISTING_VALUE"], "preserve-me")

        result = uninstall_claude(store, settings_path=settings)
        restored = json.loads(settings.read_text(encoding="utf-8"))
        self.assertTrue(result["data_preserved"])
        self.assertEqual(restored, original)
        self.assertTrue(self.home.exists())

    def test_repeated_install_preserves_first_baseline(self) -> None:
        store = PersonalAgentStore(self.home)
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir()
        original = {
            "autoMemoryEnabled": False,
            "env": {"EXISTING_VALUE": "preserve"},
        }
        settings.write_text(json.dumps(original), encoding="utf-8")
        executable = self.root / "personal-agent.exe"
        executable.write_text("test", encoding="utf-8")
        version = {
            "executable": "claude",
            "version": "2.1.300",
            "supported": True,
        }
        with patch("personal_agent.cli.claude_version", return_value=version):
            install_claude(
                store,
                settings_path=settings,
                dry_run=False,
                enable_auto_memory=True,
                hook_executable=str(executable),
            )
            install_claude(
                store,
                settings_path=settings,
                dry_run=False,
                enable_auto_memory=True,
                hook_executable=str(executable),
            )
        result = uninstall_claude(store, settings_path=settings)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), original)

    def test_install_mode_change_still_uninstalls_auto_memory(self) -> None:
        store = PersonalAgentStore(self.home)
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir()
        original = {"autoMemoryEnabled": False}
        settings.write_text(json.dumps(original), encoding="utf-8")
        executable = self.root / "personal-agent.exe"
        executable.write_text("test", encoding="utf-8")
        version = {
            "executable": "claude",
            "version": "2.1.300",
            "supported": True,
        }
        with patch("personal_agent.cli.claude_version", return_value=version):
            install_claude(
                store,
                settings_path=settings,
                dry_run=False,
                enable_auto_memory=True,
                hook_executable=str(executable),
            )
            install_claude(
                store,
                settings_path=settings,
                dry_run=False,
                enable_auto_memory=False,
                hook_executable=str(executable),
            )
        result = uninstall_claude(store, settings_path=settings)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), original)

    def test_uninstall_preserves_user_changes_after_install(self) -> None:
        store = PersonalAgentStore(self.home)
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(
            json.dumps({"autoMemoryEnabled": False}),
            encoding="utf-8",
        )
        executable = self.root / "personal-agent.exe"
        executable.write_text("test", encoding="utf-8")
        version = {
            "executable": "claude",
            "version": "2.1.300",
            "supported": True,
        }
        with patch("personal_agent.cli.claude_version", return_value=version):
            install_claude(
                store,
                settings_path=settings,
                dry_run=False,
                enable_auto_memory=True,
                hook_executable=str(executable),
            )
        installed = json.loads(settings.read_text(encoding="utf-8"))
        installed["autoMemoryDirectory"] = "C:\\user-changed-memory"
        settings.write_text(json.dumps(installed), encoding="utf-8")

        result = uninstall_claude(store, settings_path=settings)
        restored = json.loads(settings.read_text(encoding="utf-8"))
        self.assertIn("autoMemoryDirectory", result["conflicts"])
        self.assertEqual(
            restored["autoMemoryDirectory"],
            "C:\\user-changed-memory",
        )
        self.assertNotIn("hooks", restored)

    def test_symlinked_home_is_rejected(self) -> None:
        real_home = self.root / "real-home"
        real_home.mkdir()
        linked_home = self.root / "linked-home"
        try:
            os.symlink(real_home, linked_home, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symlink creation is unavailable: {error}")
        store = PersonalAgentStore(linked_home)
        with self.assertRaises(ValueError):
            store.initialize()

    def test_unsupported_claude_version_blocks_install(self) -> None:
        store = PersonalAgentStore(self.home)
        settings = self.root / ".claude" / "settings.json"
        executable = self.root / "personal-agent.exe"
        executable.write_text("test", encoding="utf-8")
        with (
            patch(
                "personal_agent.cli.claude_version",
                return_value={
                    "executable": "claude",
                    "version": "2.0.67",
                    "supported": False,
                },
            ),
            self.assertRaises(ValueError),
        ):
            install_claude(
                store,
                settings_path=settings,
                dry_run=False,
                hook_executable=str(executable),
            )
        self.assertFalse(self.home.exists())
        self.assertFalse(settings.exists())

    def test_profile_purge_removes_generations_backups_and_user_files(self) -> None:
        store = PersonalAgentStore(self.home, profile_id="private")
        store.initialize()
        store.add_memory("delete me", kind="lesson")
        manifest = store.manifest()
        manifest["capture"].update({"episodes": True, "mode": "metadata", "preview_chars": 0})
        store.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        store.start_turn(
            session_id="session",
            prompt_id="prompt",
            workspace="C:\\repo",
            user_prompt="Please fix this issue.",
        )
        store.finish_turn(
            session_id="session",
            prompt_id="prompt",
            assistant_response="I ran the tests and verified the fix.",
        )
        with store.layout.locked():
            backup = store.layout.snapshot_active(label="contains-private")

        report = purge_profile(store, include_backups=True)
        self.assertTrue(report["user_files_removed"])
        self.assertFalse(store.profile_dir.exists())

        reopened = PersonalAgentStore(self.home, profile_id="private")
        reopened.initialize()
        self.assertEqual(reopened.list_memories(), [])

        connection = sqlite3.connect(backup / "state.db")
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM memories WHERE profile_id = 'private'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)
        for directory in list(store.layout.generations_dir.iterdir()) + [backup]:
            database = directory / "state.db"
            if not database.exists():
                continue
            connection = sqlite3.connect(database)
            try:
                has_eval_runs = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'eval_runs'
                    """
                ).fetchone()
                eval_run_count = (
                    connection.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0]
                    if has_eval_runs
                    else 0
                )
            finally:
                connection.close()
            self.assertEqual(eval_run_count, 0)


if __name__ == "__main__":
    unittest.main()
