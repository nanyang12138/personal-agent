import json
import os
import tempfile
import unittest
from pathlib import Path

from personal_agent.cli import install_claude, stop_hook, user_prompt_submit
from personal_agent.core import PersonalAgentStore


class PersonalAgentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "agent-home"
        self.store = PersonalAgentStore(self.home)
        self.store.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_initialize_creates_package_memory_and_evals(self) -> None:
        self.assertTrue((self.home / "package" / "manifest.json").exists())
        self.assertTrue((self.home / "package" / "rules" / "core.md").exists())
        self.assertTrue(
            (
                self.home
                / "package"
                / "skills"
                / "evidence-first-work"
                / "SKILL.md"
            ).exists()
        )
        self.assertTrue((self.home / "memory" / "MEMORY.md").exists())
        self.assertGreaterEqual(len(self.store.list_evals()), 2)

    def test_context_selects_rules_skill_memory_and_eval(self) -> None:
        self.store.add_memory(
            "Always inspect the earliest original failure before choosing a root cause.",
            kind="lesson",
        )
        context = self.store.build_context(
            "Please fix this failure and verify the change.",
            workspace="C:\\repo",
        )
        self.assertIn("Active personal rules", context)
        self.assertIn("Relevant skill: evidence-first-work", context)
        self.assertIn("Relevant learned memory", context)
        self.assertIn("Active eval obligations", context)

    def test_chinese_task_selects_default_skill(self) -> None:
        context = self.store.build_context(
            "请实现这个修改并完成验证。",
            workspace="C:\\repo",
        )
        self.assertIn("Relevant skill: evidence-first-work", context)

    def test_repeated_correction_auto_promotes_low_risk_memory(self) -> None:
        for _ in range(3):
            candidate_id = self.store.stage_candidate(
                text="以后分析失败时应该先检查原始日志。",
                kind="lesson",
                reason="test",
            )
        candidates = self.store.list_candidates(status="approved")
        memories = self.store.list_memories()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(memories), 1)
        self.assertIn("原始日志", memories[0]["text"])
        self.store.approve_candidate(candidate_id)
        self.assertEqual(len(self.store.list_memories()), 1)

    def test_sensitive_memory_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_memory("api_key=abcdefghijklmnopqrstuvwxyz123456")

    def test_prompt_and_stop_hooks_record_episode_and_eval(self) -> None:
        prompt_result = user_prompt_submit(
            self.store,
            {
                "session_id": "session-1",
                "prompt_id": "prompt-1",
                "cwd": "C:\\repo",
                "prompt": "请实现这个修改并完成验证。",
            },
        )
        self.assertEqual(
            prompt_result["hookSpecificOutput"]["hookEventName"],
            "UserPromptSubmit",
        )

        stop_result = stop_hook(
            self.store,
            {
                "session_id": "session-1",
                "last_assistant_message": "Implemented the change and ran the tests successfully.",
                "stop_hook_active": False,
            },
        )
        self.assertEqual(stop_result, {})
        episodes = self.store.list_episodes()
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["status"], "completed")
        self.assertEqual(self.store.failed_eval_results(int(episodes[0]["id"])), [])

    def test_stop_hook_rechecks_eval_after_one_continuation(self) -> None:
        user_prompt_submit(
            self.store,
            {
                "session_id": "session-2",
                "prompt_id": "prompt-2",
                "cwd": "C:\\repo",
                "prompt": "请实现这个修改。",
            },
        )
        old_value = os.environ.get("PERSONAL_AGENT_ENFORCE_EVALS")
        os.environ["PERSONAL_AGENT_ENFORCE_EVALS"] = "1"
        try:
            first = stop_hook(
                self.store,
                {
                    "session_id": "session-2",
                    "last_assistant_message": "修改已经完成。",
                    "stop_hook_active": False,
                },
            )
            self.assertIn("additionalContext", first["hookSpecificOutput"])

            second = stop_hook(
                self.store,
                {
                    "session_id": "session-2",
                    "last_assistant_message": "修改已经完成，并运行测试完成验证。",
                    "stop_hook_active": True,
                },
            )
            self.assertEqual(second, {})
            episode_id = int(self.store.list_episodes()[0]["id"])
            self.assertEqual(self.store.failed_eval_results(episode_id), [])
        finally:
            if old_value is None:
                os.environ.pop("PERSONAL_AGENT_ENFORCE_EVALS", None)
            else:
                os.environ["PERSONAL_AGENT_ENFORCE_EVALS"] = old_value

    def test_install_claude_preserves_existing_settings(self) -> None:
        settings = Path(self.tempdir.name) / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps(
                {
                    "model": "test-model",
                    "env": {"EXISTING_VALUE": "preserve-me"},
                }
            ),
            encoding="utf-8",
        )

        result = install_claude(self.store, settings_path=settings, dry_run=False)
        installed = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(installed["model"], "test-model")
        self.assertEqual(installed["env"]["EXISTING_VALUE"], "preserve-me")
        self.assertEqual(installed["env"]["PERSONAL_AGENT_HOME"], str(self.home))
        self.assertTrue(installed["autoMemoryEnabled"])
        self.assertEqual(installed["autoMemoryDirectory"], str(self.store.memory_dir))
        self.assertIn("UserPromptSubmit", installed["hooks"])
        self.assertIn("Stop", installed["hooks"])
        self.assertTrue(Path(result["backup_path"]).exists())


if __name__ == "__main__":
    unittest.main()
