from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .core import PersonalAgentStore, default_home


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def read_hook_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Hook input must be a JSON object.")
    return parsed


def user_prompt_submit(store: PersonalAgentStore, data: dict[str, Any]) -> dict[str, Any]:
    if data.get("agent_id"):
        return {}
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        return {}
    session_id = str(data.get("session_id") or "unknown-session")
    prompt_id = data.get("prompt_id")
    workspace = str(data.get("cwd") or "")

    store.stage_feedback_from_prompt(session_id=session_id, prompt=prompt)
    store.start_turn(
        session_id=session_id,
        prompt_id=str(prompt_id) if prompt_id else None,
        workspace=workspace,
        user_prompt=prompt,
    )
    context = store.build_context(prompt, workspace=workspace)
    if not context:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }


def stop_hook(store: PersonalAgentStore, data: dict[str, Any]) -> dict[str, Any]:
    if data.get("agent_id"):
        return {}
    session_id = str(data.get("session_id") or "unknown-session")
    response = str(data.get("last_assistant_message") or "")
    episode_id = store.finish_turn(
        session_id=session_id,
        assistant_response=response,
        revise_last=bool(data.get("stop_hook_active")),
    )
    if episode_id is None:
        return {}

    enforce = os.environ.get("PERSONAL_AGENT_ENFORCE_EVALS", "").casefold() in {
        "1",
        "true",
        "yes",
    }
    if not enforce or data.get("stop_hook_active"):
        return {}
    failures = store.failed_eval_results(episode_id)
    if not failures:
        return {}
    details = "\n".join(f"- {item.name}: {item.details}" for item in failures)
    return {
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": (
                "Personal Agent evals found unresolved obligations. "
                "Address them before finishing:\n" + details
            ),
        }
    }


def hook_command(store: PersonalAgentStore, event: str) -> int:
    data = read_hook_input()
    if event == "user-prompt-submit":
        result = user_prompt_submit(store, data)
    elif event == "stop":
        result = stop_hook(store, data)
    else:
        raise ValueError(f"Unsupported hook event: {event}")
    if result:
        print_json(result)
    return 0


def command_line_for_hook(event: str) -> str:
    arguments = [sys.executable, "-m", "personal_agent", "hook", event]
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    import shlex

    return shlex.join(arguments)


def merge_hook(settings: dict[str, Any], event: str, command: str) -> bool:
    hooks = settings.setdefault("hooks", {})
    event_hooks = hooks.setdefault(event, [])
    for matcher_group in event_hooks:
        for hook in matcher_group.get("hooks", []):
            if hook.get("command") == command:
                return False
    event_hooks.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 15 if event == "UserPromptSubmit" else 30,
                    "statusMessage": (
                        "Loading personal context..."
                        if event == "UserPromptSubmit"
                        else "Recording personal-agent episode..."
                    ),
                }
            ]
        }
    )
    return True


def install_claude(
    store: PersonalAgentStore, *, settings_path: Path, dry_run: bool
) -> dict[str, Any]:
    store.initialize()
    settings: dict[str, Any] = {}
    if settings_path.exists():
        parsed = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError(f"{settings_path} must contain a JSON object.")
        settings = parsed

    changed_keys: list[str] = []
    if settings.get("autoMemoryEnabled") is not True:
        settings["autoMemoryEnabled"] = True
        changed_keys.append("autoMemoryEnabled")
    memory_path = str(store.memory_dir)
    if settings.get("autoMemoryDirectory") != memory_path:
        settings["autoMemoryDirectory"] = memory_path
        changed_keys.append("autoMemoryDirectory")

    environment = settings.setdefault("env", {})
    if environment.get("PERSONAL_AGENT_HOME") != str(store.home):
        environment["PERSONAL_AGENT_HOME"] = str(store.home)
        changed_keys.append("env.PERSONAL_AGENT_HOME")

    for event_name, command_name in (
        ("UserPromptSubmit", "user-prompt-submit"),
        ("Stop", "stop"),
    ):
        if merge_hook(settings, event_name, command_line_for_hook(command_name)):
            changed_keys.append(f"hooks.{event_name}")

    result = {
        "settings_path": str(settings_path),
        "home": str(store.home),
        "memory_directory": str(store.memory_dir),
        "changed_keys": changed_keys,
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = settings_path.with_name(f"{settings_path.name}.bak-{timestamp}")
        shutil.copy2(settings_path, backup)
        result["backup_path"] = str(backup)
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def add_home_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help=f"Local state directory. Default: {default_home()}",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="personal-agent",
        description="Manage a user-owned Rules, Skills, Memory, and Evals layer.",
    )
    add_home_argument(parser)
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("init", help="Create the local package and database.")
    subcommands.add_parser("status", help="Show local package status.")
    subcommands.add_parser("doctor", help="Check the installation and local state.")

    context = subcommands.add_parser("context", help="Build context for a task.")
    context.add_argument("query")
    context.add_argument("--workspace", default="")

    hook = subcommands.add_parser("hook", help="Run a Claude Code hook handler.")
    hook.add_argument("event", choices=["user-prompt-submit", "stop"])

    install = subcommands.add_parser("install-claude", help="Install Claude Code hooks.")
    install.add_argument(
        "--settings",
        type=Path,
        default=Path.home() / ".claude" / "settings.json",
    )
    install.add_argument("--dry-run", action="store_true")

    memory = subcommands.add_parser("memory", help="Manage active memories.")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_add = memory_commands.add_parser("add")
    memory_add.add_argument("text")
    memory_add.add_argument("--kind", default="lesson")
    memory_add.add_argument("--scope", default="global")
    memory_commands.add_parser("list")

    candidates = subcommands.add_parser("candidates", help="Review learned candidates.")
    candidate_commands = candidates.add_subparsers(
        dest="candidate_command", required=True
    )
    candidate_commands.add_parser("list")
    candidate_approve = candidate_commands.add_parser("approve")
    candidate_approve.add_argument("id", type=int)
    candidate_reject = candidate_commands.add_parser("reject")
    candidate_reject.add_argument("id", type=int)

    episodes = subcommands.add_parser("episodes", help="List recorded episodes.")
    episodes.add_argument("--limit", type=int, default=20)

    evals = subcommands.add_parser("evals", help="Manage eval definitions.")
    eval_commands = evals.add_subparsers(dest="eval_command", required=True)
    eval_commands.add_parser("list")
    eval_add = eval_commands.add_parser("add")
    eval_add.add_argument("name")
    eval_add.add_argument("--query", action="append", default=[])
    eval_add.add_argument("--require", action="append", default=[])
    eval_add.add_argument("--forbid", action="append", default=[])

    return parser


def doctor(store: PersonalAgentStore) -> dict[str, Any]:
    store.initialize()
    claude_settings = Path.home() / ".claude" / "settings.json"
    hook_events: list[str] = []
    auto_memory_directory = None
    if claude_settings.exists():
        settings = json.loads(claude_settings.read_text(encoding="utf-8"))
        hooks = settings.get("hooks", {})
        hook_events = sorted(hooks)
        auto_memory_directory = settings.get("autoMemoryDirectory")
    return {
        **store.status(),
        "python": sys.version.split()[0],
        "claude_settings": str(claude_settings),
        "claude_hook_events": hook_events,
        "claude_auto_memory_directory": auto_memory_directory,
        "package_importable": True,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    store = PersonalAgentStore(args.home)

    try:
        if args.command == "init":
            store.initialize()
            print_json(store.status())
        elif args.command == "status":
            print_json(store.status())
        elif args.command == "doctor":
            print_json(doctor(store))
        elif args.command == "context":
            print(store.build_context(args.query, workspace=args.workspace))
        elif args.command == "hook":
            return hook_command(store, args.event)
        elif args.command == "install-claude":
            print_json(
                install_claude(
                    store,
                    settings_path=args.settings.expanduser().resolve(),
                    dry_run=args.dry_run,
                )
            )
        elif args.command == "memory":
            if args.memory_command == "add":
                memory_id = store.add_memory(
                    args.text, kind=args.kind, scope=args.scope
                )
                print_json({"memory_id": memory_id})
            else:
                print_json([dict(row) for row in store.list_memories()])
        elif args.command == "candidates":
            if args.candidate_command == "list":
                print_json([dict(row) for row in store.list_candidates()])
            elif args.candidate_command == "approve":
                print_json({"memory_id": store.approve_candidate(args.id)})
            else:
                store.reject_candidate(args.id)
                print_json({"rejected": args.id})
        elif args.command == "episodes":
            print_json([dict(row) for row in store.list_episodes(limit=args.limit)])
        elif args.command == "evals":
            if args.eval_command == "list":
                print_json([dict(row) for row in store.list_evals()])
            else:
                eval_id = store.add_eval(
                    name=args.name,
                    query_terms=args.query,
                    required_any=args.require,
                    forbidden_any=args.forbid,
                )
                print_json({"eval_id": eval_id})
        else:
            parser.error(f"Unsupported command: {args.command}")
    except (ValueError, json.JSONDecodeError, sqlite3.Error) as error:
        print(f"personal-agent: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
