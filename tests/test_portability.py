import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from personal_agent.core import PersonalAgentStore
from personal_agent.layout import StateLayout
from personal_agent.portability import export_profile, import_profile, read_validated_bundle


class PortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.home = self.root / "agent-home"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_export_import_round_trip_into_new_profile(self) -> None:
        source = PersonalAgentStore(self.home, profile_id="source")
        source.initialize()
        source.add_memory("Prefer conclusions before details.", kind="preference")
        source.add_eval(
            name="custom-eval",
            query_terms=["analysis"],
            required_any=["evidence"],
            forbidden_any=["guess"],
        )
        custom_rule = source.rules_dir / "custom.md"
        custom_rule.write_text("# Custom\n\nUse primary evidence.\n", encoding="utf-8")

        bundle = export_profile(source, self.root / "profile.pa-bundle")
        imported = import_profile(
            self.home,
            bundle,
            target_profile="imported",
            activate_assets=True,
        )

        memories = imported.list_memories()
        self.assertEqual([row["text"] for row in memories], ["Prefer conclusions before details."])
        self.assertTrue((imported.rules_dir / "custom.md").exists())
        self.assertIn("custom-eval", [row["name"] for row in imported.list_evals()])

    def test_import_quarantines_assets_by_default(self) -> None:
        source = PersonalAgentStore(self.home, profile_id="source")
        source.initialize()
        source.add_memory("private preference", kind="preference")
        (source.rules_dir / "custom.md").write_text(
            "# Custom\n\nDo something.\n",
            encoding="utf-8",
        )
        bundle = export_profile(source, self.root / "profile.pa-bundle")

        imported = import_profile(
            self.home,
            bundle,
            target_profile="quarantined",
        )

        self.assertEqual(imported.list_memories(), [])
        self.assertFalse((imported.rules_dir / "custom.md").exists())
        self.assertTrue(
            (
                imported.profile_dir / "quarantine" / "imported-assets" / "rules" / "custom.md"
            ).exists()
        )

    def test_export_excludes_episodes_by_default(self) -> None:
        store = PersonalAgentStore(self.home)
        store.initialize()
        manifest = store.manifest()
        manifest["capture"].update({"episodes": True, "mode": "redacted", "preview_chars": 100})
        store.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        store.start_turn(
            session_id="session",
            prompt_id="prompt",
            workspace="C:\\repo",
            user_prompt="private episode",
        )
        store.finish_turn(session_id="session", assistant_response="answer")

        bundle = export_profile(store, self.root / "profile.pa-bundle")
        manifest, entries = read_validated_bundle(bundle)
        self.assertFalse(manifest["include_episodes"])
        self.assertNotIn("data/episodes.jsonl", entries)

    def test_import_rejects_path_traversal(self) -> None:
        bundle = self.root / "unsafe.pa-bundle"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("../escape.txt", "bad")
            archive.writestr(
                "manifest.json",
                json.dumps({"format": "personal-agent.export/v1", "inventory": []}),
            )
        with self.assertRaises(ValueError):
            read_validated_bundle(bundle)

    def test_import_rejects_unlisted_inventory_entry(self) -> None:
        bundle = self.root / "unlisted.pa-bundle"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("user/rules/unlisted.md", "# injected")
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "format": "personal-agent.export/v1",
                        "inventory": [],
                    }
                ),
            )
        with self.assertRaises(ValueError):
            read_validated_bundle(bundle)

    def test_failed_import_leaves_no_target_profile(self) -> None:
        source = PersonalAgentStore(self.home, profile_id="source")
        source.initialize()
        source.add_memory("valid memory", kind="lesson")
        bundle = export_profile(source, self.root / "profile.pa-bundle")

        manifest, entries = read_validated_bundle(bundle)
        memory_rows = [
            json.loads(line)
            for line in entries["data/memories.jsonl"].decode("utf-8").splitlines()
            if line
        ]
        memory_rows[0]["confidence"] = "not-a-number"
        replacement = ("\n".join(json.dumps(row) for row in memory_rows) + "\n").encode()
        for item in manifest["inventory"]:
            if item["path"] == "data/memories.jsonl":
                import hashlib

                item["size"] = len(replacement)
                item["sha256"] = hashlib.sha256(replacement).hexdigest()

        broken = self.root / "broken.pa-bundle"
        with zipfile.ZipFile(broken, "w") as archive:
            for path, payload in entries.items():
                if path == "manifest.json":
                    continue
                archive.writestr(
                    path,
                    replacement if path == "data/memories.jsonl" else payload,
                )
            archive.writestr("manifest.json", json.dumps(manifest))

        with self.assertRaises(ValueError):
            import_profile(
                self.home,
                broken,
                target_profile="partial",
            )
        self.assertFalse((self.home / "user" / "profiles" / "partial").exists())

    def test_interrupted_import_removes_staged_generation_and_profile(self) -> None:
        source = PersonalAgentStore(self.home, profile_id="source")
        source.initialize()
        source.add_memory("valid memory", kind="lesson")
        bundle = export_profile(source, self.root / "profile.pa-bundle")
        generations_before = {path.name for path in (self.home / "state" / "generations").iterdir()}

        with (
            patch(
                "personal_agent.portability.import_rows",
                side_effect=KeyboardInterrupt(),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            import_profile(
                self.home,
                bundle,
                target_profile="interrupted",
            )

        generations_after = {path.name for path in (self.home / "state" / "generations").iterdir()}
        self.assertEqual(generations_after, generations_before)
        self.assertFalse((self.home / "user" / "profiles" / "interrupted").exists())

    def test_interrupt_after_active_switch_preserves_committed_import(self) -> None:
        source = PersonalAgentStore(self.home, profile_id="source")
        source.initialize()
        source.add_memory("committed memory", kind="lesson")
        bundle = export_profile(source, self.root / "profile.pa-bundle")
        original_switch = StateLayout.switch

        def switch_then_interrupt(layout: StateLayout, identifier: str, *, reason: str) -> None:
            original_switch(layout, identifier, reason=reason)
            if reason.startswith("import-"):
                raise KeyboardInterrupt()

        with (
            patch.object(StateLayout, "switch", switch_then_interrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            import_profile(
                self.home,
                bundle,
                target_profile="committed",
                activate_assets=True,
            )

        reopened = PersonalAgentStore(self.home, profile_id="committed")
        reopened.initialize()
        self.assertEqual(
            [row["text"] for row in reopened.list_memories()],
            ["committed memory"],
        )


if __name__ == "__main__":
    unittest.main()
