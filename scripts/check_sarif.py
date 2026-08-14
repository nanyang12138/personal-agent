from __future__ import annotations

import json
import sys
from pathlib import Path


def findings(root: Path) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*.sarif")):
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        for run in document.get("runs", []):
            rules = {
                rule.get("id"): rule
                for rule in run.get("tool", {}).get("driver", {}).get("rules", [])
            }
            for result in run.get("results", []):
                rule_id = str(result.get("ruleId") or "unknown")
                level = str(result.get("level") or "warning")
                message = str(result.get("message", {}).get("text") or "")
                rule = rules.get(rule_id, {})
                precision = str(rule.get("properties", {}).get("precision") or "unknown")
                if level in {"error", "warning"} and precision in {
                    "high",
                    "very-high",
                    "unknown",
                }:
                    results.append((rule_id, level, message))
    return results


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_sarif.py <sarif-directory>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    if not root.is_dir():
        print(f"SARIF directory is missing: {root}", file=sys.stderr)
        return 2
    alerts = findings(root)
    if alerts:
        for rule_id, level, message in alerts:
            print(f"{level}: {rule_id}: {message}")
        return 1
    print("No high-confidence CodeQL findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
