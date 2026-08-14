import json
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

    def update_manifest(self, **updates: object) -> None:
        manifest = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        for key, value in updates.items():
            manifest[key] = value
        self.store.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def enable_redacted_capture(self) -> None:
        manifest = self.store.manifest()
        manifest["capture"].update(
            {
                "episodes": True,
                "mode": "redacted",
                "preview_chars": 1000,
            }
        )
        self.store.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_initialize_creates_package_memory_and_evals(self) -> None:
        profile = self.home / "user" / "profiles" / "default"
        self.assertTrue((profile / "manifest.json").exists())
        self.assertTrue((profile / "rules").is_dir())
        self.assertTrue((profile / "skills").is_dir())
        self.assertEqual(list((profile / "rules").iterdir()), [])
        self.assertEqual(list((profile / "skills").iterdir()), [])
        self.assertTrue((profile / "memory" / "MEMORY.md").exists())
        self.assertEqual(self.store.status()["data_schema_version"], 4)
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

    def test_repeated_correction_requires_manual_promotion(self) -> None:
        for _ in range(3):
            candidate_id = self.store.stage_candidate(
                text="以后分析失败时应该先检查原始日志。",
                kind="lesson",
                reason="test",
            )
        candidates = self.store.list_candidates(status="pending")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(self.store.list_memories()), 0)
        self.store.approve_candidate(candidate_id)
        self.assertEqual(len(self.store.list_memories()), 1)

    def test_sensitive_memory_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_memory("api_key=abcdefghijklmnopqrstuvwxyz123456")

    def test_prompt_and_stop_hooks_record_episode_and_eval(self) -> None:
        self.enable_redacted_capture()
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

    def test_required_text_eval_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_eval(
                name="required-verification",
                query_terms=["实现", "修改"],
                required_any=["测试", "验证"],
                forbidden_any=[],
                severity="required",
            )

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
        hook_executable = Path(self.tempdir.name) / "personal-agent.exe"
        hook_executable.write_text("test", encoding="utf-8")

        result = install_claude(
            self.store,
            settings_path=settings,
            dry_run=False,
            enable_auto_memory=True,
            allow_unsupported_claude=True,
            hook_executable=str(hook_executable),
        )
        installed = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(installed["model"], "test-model")
        self.assertEqual(installed["env"]["EXISTING_VALUE"], "preserve-me")
        self.assertEqual(installed["env"]["PERSONAL_AGENT_HOME"], str(self.home))
        self.assertTrue(installed["autoMemoryEnabled"])
        self.assertEqual(installed["autoMemoryDirectory"], str(self.store.memory_dir))
        self.assertIn("UserPromptSubmit", installed["hooks"])
        self.assertIn("Stop", installed["hooks"])
        handler = installed["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual(handler["command"], str(hook_executable.resolve()))
        self.assertEqual(handler["args"], ["hook", "user-prompt-submit"])
        self.assertTrue(Path(result["install_manifest"]).exists())


if __name__ == "__main__":
    unittest.main()
