from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .core import PersonalAgentStore, default_home
from .layout import DirectoryLock, atomic_write_json, utc_now
from .maintenance import purge_profile
from .migrations import CURRENT_SCHEMA_VERSION
from .portability import export_profile, import_profile


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
    if not data.get("session_id"):
        return {}
    session_id = str(data["session_id"])
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
    if not data.get("session_id"):
        return {}
    session_id = str(data["session_id"])
    response = str(data.get("last_assistant_message") or "")
    episode_id = store.finish_turn(
        session_id=session_id,
        assistant_response=response,
        revise_last=bool(data.get("stop_hook_active")),
        prompt_id=str(data["prompt_id"]) if data.get("prompt_id") else None,
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
    failures = store.failed_eval_results(episode_id, minimum_severity="required")
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


def stop_failure_hook(store: PersonalAgentStore, data: dict[str, Any]) -> dict[str, Any]:
    if data.get("agent_id") or not data.get("session_id"):
        return {}
    store.fail_turn(
        session_id=str(data["session_id"]),
        prompt_id=str(data["prompt_id"]) if data.get("prompt_id") else None,
        error_type=str(data.get("error") or "unknown"),
    )
    return {}


def hook_command(store: PersonalAgentStore, event: str) -> int:
    try:
        data = read_hook_input()
        if event == "user-prompt-submit":
            result = user_prompt_submit(store, data)
        elif event == "stop":
            result = stop_hook(store, data)
        elif event == "stop-failure":
            result = stop_failure_hook(store, data)
        else:
            raise ValueError(f"Unsupported hook event: {event}")
        if result:
            print_json(result)
    except Exception as error:
        try:
            logs = store.home / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            with (logs / "hook-errors.log").open("a", encoding="utf-8") as stream:
                stream.write(
                    f"{datetime.now().isoformat(timespec='seconds')} "
                    f"{event} {type(error).__name__}\n"
                )
        except OSError:
            pass
        # Context and recording hooks are availability features, not security gates.
        # Fail open so a broken personal layer never blocks the underlying agent.
        return 0
    return 0


MINIMUM_CLAUDE_VERSION = (2, 1, 196)


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = __import__("re").search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        return None
    return tuple(int(match.group(index)) for index in (1, 2, 3))


def claude_version() -> dict[str, Any]:
    executable = shutil.which("claude")
    if not executable:
        return {"executable": None, "version": None, "supported": False}
    process = subprocess.run(
        [executable, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = (process.stdout or process.stderr).strip()
    parsed = parse_version(output)
    return {
        "executable": executable,
        "version": ".".join(str(value) for value in parsed) if parsed else output,
        "supported": bool(parsed and parsed >= MINIMUM_CLAUDE_VERSION),
    }


def load_settings(settings_path: Path) -> dict[str, Any]:
    if settings_path.is_symlink():
        raise ValueError("Refusing to modify a symlinked Claude settings file.")
    if not settings_path.exists():
        return {}
    parsed = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{settings_path} must contain a JSON object.")
    if "env" in parsed and not isinstance(parsed["env"], dict):
        raise ValueError("Claude settings env must be an object.")
    if "hooks" in parsed and not isinstance(parsed["hooks"], dict):
        raise ValueError("Claude settings hooks must be an object.")
    return parsed


def hook_owned_by_personal_agent(hook: dict[str, Any]) -> bool:
    command = str(hook.get("command") or "")
    args = [str(value) for value in hook.get("args", [])]
    if "-m personal_agent hook" in command:
        return True
    joined = " ".join(args)
    return (
        Path(command).name.casefold().startswith("personal-agent")
        and "hook" in args
        and any(event in joined for event in ("user-prompt-submit", "stop"))
    )


def remove_owned_hooks(settings: dict[str, Any]) -> list[str]:
    hooks = settings.setdefault("hooks", {})
    removed: list[str] = []
    for event in ("UserPromptSubmit", "Stop", "StopFailure"):
        groups = hooks.get(event, [])
        new_groups = []
        for group in groups:
            handlers = group.get("hooks", [])
            kept = [handler for handler in handlers if not hook_owned_by_personal_agent(handler)]
            if len(kept) != len(handlers):
                removed.append(event)
            if kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                new_groups.append(new_group)
        if new_groups:
            hooks[event] = new_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return removed


def hook_handler(executable: str, event: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": executable,
        "args": ["hook", event],
        "timeout": 15 if event == "user-prompt-submit" else 30,
        "statusMessage": (
            "Loading personal context..."
            if event == "user-prompt-submit"
            else "Recording personal-agent episode..."
        ),
    }


def merge_owned_hooks(settings: dict[str, Any], *, executable: str) -> list[str]:
    remove_owned_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    changed: list[str] = []
    for event_name, command_name in (
        ("UserPromptSubmit", "user-prompt-submit"),
        ("Stop", "stop"),
        ("StopFailure", "stop-failure"),
    ):
        hooks.setdefault(event_name, []).append({"hooks": [hook_handler(executable, command_name)]})
        changed.append(event_name)
    return changed


def write_settings_atomic(settings_path: Path, settings: dict[str, Any]) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = None
    if settings_path.exists():
        previous_mode = stat.S_IMODE(settings_path.stat().st_mode)
    atomic_write_json(settings_path, settings)
    if previous_mode is not None and os.name != "nt":
        settings_path.chmod(previous_mode)


def installation_manifest_path(store: PersonalAgentStore, settings_path: Path) -> Path:
    normalized = os.path.normcase(str(settings_path.absolute()))
    identifier = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    return store.layout.control_dir / "installs" / f"{identifier}.json"


def touched_values(
    settings: dict[str, Any],
    *,
    enable_auto_memory: bool,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "env.PERSONAL_AGENT_HOME": settings.get("env", {}).get("PERSONAL_AGENT_HOME"),
        "env.PERSONAL_AGENT_PROFILE": settings.get("env", {}).get("PERSONAL_AGENT_PROFILE"),
    }
    if enable_auto_memory:
        values["autoMemoryEnabled"] = settings.get("autoMemoryEnabled")
        values["autoMemoryDirectory"] = settings.get("autoMemoryDirectory")
    return values


def previous_touched_values(settings: dict[str, Any]) -> dict[str, Any]:
    environment = settings.get("env", {})
    return {
        "autoMemoryEnabled": {
            "present": "autoMemoryEnabled" in settings,
            "value": settings.get("autoMemoryEnabled"),
        },
        "autoMemoryDirectory": {
            "present": "autoMemoryDirectory" in settings,
            "value": settings.get("autoMemoryDirectory"),
        },
        "env.PERSONAL_AGENT_HOME": {
            "present": "PERSONAL_AGENT_HOME" in environment,
            "value": environment.get("PERSONAL_AGENT_HOME"),
        },
        "env.PERSONAL_AGENT_PROFILE": {
            "present": "PERSONAL_AGENT_PROFILE" in environment,
            "value": environment.get("PERSONAL_AGENT_PROFILE"),
        },
    }


def install_claude(
    store: PersonalAgentStore,
    *,
    settings_path: Path,
    dry_run: bool,
    enable_auto_memory: bool = False,
    allow_unsupported_claude: bool = False,
    allow_unstable_hook_executable: bool = False,
    hook_executable: str | None = None,
) -> dict[str, Any]:
    settings_path = settings_path.expanduser().absolute()
    settings = load_settings(settings_path)
    version = claude_version()
    if not allow_unsupported_claude and not version["supported"]:
        raise ValueError(
            "Claude Code >= 2.1.196 is required for the supported Hook protocol. "
            f"Detected: {version['version'] or 'not installed'}."
        )

    executable = hook_executable or shutil.which("personal-agent")
    if not executable:
        raise ValueError(
            "The personal-agent executable is not on PATH. "
            "Install with uv tool or pipx before configuring Claude Code."
        )
    executable = str(Path(executable).resolve())
    executable_path = Path(executable)
    if not executable_path.is_file():
        raise ValueError(f"Hook executable does not exist: {executable_path}")
    unstable_parts = {".venv", "venv"}
    if not allow_unstable_hook_executable and any(
        part.casefold() in unstable_parts for part in executable_path.parts
    ):
        raise ValueError(
            "Refusing to install a Hook from a project virtual environment. "
            "Install Personal Agent with uv tool or pipx first."
        )

    previous = previous_touched_values(settings)
    changed_keys: list[str] = []
    if enable_auto_memory:
        if settings.get("autoMemoryEnabled") is not True:
            settings["autoMemoryEnabled"] = True
            changed_keys.append("autoMemoryEnabled")
        memory_path = str(store.memory_dir)
        if settings.get("autoMemoryDirectory") != memory_path:
            settings["autoMemoryDirectory"] = memory_path
            changed_keys.append("autoMemoryDirectory")

    environment = settings.setdefault("env", {})
    for key, value in (
        ("PERSONAL_AGENT_HOME", str(store.home)),
        ("PERSONAL_AGENT_PROFILE", store.profile_id),
    ):
        if environment.get(key) != value:
            environment[key] = value
            changed_keys.append(f"env.{key}")

    for event in merge_owned_hooks(settings, executable=executable):
        changed_keys.append(f"hooks.{event}")

    result = {
        "settings_path": str(settings_path),
        "home": str(store.home),
        "profile_id": store.profile_id,
        "memory_directory": str(store.memory_dir),
        "hook_executable": executable,
        "claude": version,
        "changed_keys": changed_keys,
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    store.initialize()
    manifest_path = installation_manifest_path(store, settings_path)
    lock_path = settings_path.parent / ".personal-agent-settings.lock"
    with DirectoryLock(lock_path, timeout=30):
        # Re-read under lock so concurrent settings changes are not overwritten.
        settings = load_settings(settings_path)
        if manifest_path.exists():
            existing_install = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            baseline = existing_install.get("previous", previous)
            managed_auto_memory = (
                bool(existing_install.get("enable_auto_memory", False)) or enable_auto_memory
            )
        else:
            baseline = previous_touched_values(settings)
            managed_auto_memory = enable_auto_memory
        if managed_auto_memory:
            settings["autoMemoryEnabled"] = True
            settings["autoMemoryDirectory"] = str(store.memory_dir)
        environment = settings.setdefault("env", {})
        environment["PERSONAL_AGENT_HOME"] = str(store.home)
        environment["PERSONAL_AGENT_PROFILE"] = store.profile_id
        merge_owned_hooks(settings, executable=executable)
        install_record = {
            "format": "personal-agent.install/v1",
            "status": "planned",
            "installed_at": utc_now(),
            "settings_path": str(settings_path),
            "profile_id": store.profile_id,
            "hook_executable": executable,
            "enable_auto_memory": managed_auto_memory,
            "previous": baseline,
            "installed_values": touched_values(
                settings,
                enable_auto_memory=managed_auto_memory,
            ),
        }
        atomic_write_json(manifest_path, install_record)
        write_settings_atomic(settings_path, settings)
        install_record["status"] = "installed"
        install_record["committed_at"] = utc_now()
        atomic_write_json(manifest_path, install_record)
        result["install_manifest"] = str(manifest_path)
    return result


def restore_key(
    target: dict[str, Any],
    key: str,
    previous: dict[str, Any],
) -> None:
    if previous.get("present"):
        target[key] = previous.get("value")
    else:
        target.pop(key, None)


def uninstall_claude(
    store: PersonalAgentStore,
    *,
    settings_path: Path | None = None,
) -> dict[str, Any]:
    if settings_path:
        target_path = settings_path.expanduser().absolute()
        manifest_path = installation_manifest_path(store, target_path)
    else:
        manifests = sorted((store.layout.control_dir / "installs").glob("*.json"))
        if len(manifests) != 1:
            raise ValueError("Specify --settings when zero or multiple Claude installations exist.")
        manifest_path = manifests[0]
    if not manifest_path.exists():
        raise ValueError("No Personal Agent Claude installation manifest was found.")
    install = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    target_path = Path(install["settings_path"])
    lock_path = target_path.parent / ".personal-agent-settings.lock"
    with DirectoryLock(lock_path, timeout=30):
        settings = load_settings(target_path)
        removed_hooks = remove_owned_hooks(settings)
        previous = install.get("previous", {})
        installed_values = install.get("installed_values", {})
        conflicts: list[str] = []
        for key in ("autoMemoryEnabled", "autoMemoryDirectory"):
            if key not in installed_values:
                continue
            if settings.get(key) == installed_values.get(key):
                restore_key(settings, key, previous.get(key, {}))
            else:
                conflicts.append(key)
        environment = settings.get("env", {})
        for key in ("PERSONAL_AGENT_HOME", "PERSONAL_AGENT_PROFILE"):
            installed_key = f"env.{key}"
            if environment.get(key) == installed_values.get(installed_key):
                restore_key(
                    environment,
                    key,
                    previous.get(installed_key, {}),
                )
            else:
                conflicts.append(installed_key)
        if not environment:
            settings.pop("env", None)
        write_settings_atomic(target_path, settings)
        manifest_path.unlink()
    return {
        "settings_path": str(target_path),
        "removed_hooks": sorted(set(removed_hooks)),
        "conflicts": conflicts,
        "data_preserved": True,
    }


def add_home_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help=f"Local state directory. Default: {default_home()}",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("PERSONAL_AGENT_PROFILE", "default"),
        help="Isolated personal-agent profile. Default: default",
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
    hook.add_argument(
        "event",
        choices=["user-prompt-submit", "stop", "stop-failure"],
    )

    install = subcommands.add_parser("install-claude", help="Install Claude Code hooks.")
    install.add_argument(
        "--settings",
        type=Path,
        default=Path.home() / ".claude" / "settings.json",
    )
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--enable-auto-memory", action="store_true")
    install.add_argument("--allow-unsupported-claude", action="store_true")
    install.add_argument("--allow-unstable-hook-executable", action="store_true")
    install.add_argument("--hook-executable")

    uninstall = subcommands.add_parser(
        "uninstall-claude",
        help="Remove only Personal Agent Claude hooks and preserve user data.",
    )
    uninstall.add_argument("--settings", type=Path, default=None)

    backup = subcommands.add_parser("backup", help="Create a verified state snapshot.")
    backup.add_argument("--label", default="manual")

    restore = subcommands.add_parser(
        "restore", help="Restore a verified snapshot into a new data generation."
    )
    restore.add_argument("backup_dir", type=Path)

    export = subcommands.add_parser("export", help="Create a portable logical profile bundle.")
    export.add_argument("destination", type=Path)
    export.add_argument("--include-episodes", action="store_true")

    import_command = subcommands.add_parser(
        "import", help="Import a bundle into a new isolated profile."
    )
    import_command.add_argument("bundle", type=Path)
    import_command.add_argument("--target-profile", required=True)
    import_command.add_argument(
        "--activate-assets",
        action="store_true",
        help="Trust and activate imported Rules, Skills, Memory, and Evals.",
    )

    memory = subcommands.add_parser("memory", help="Manage active memories.")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_add = memory_commands.add_parser("add")
    memory_add.add_argument("text")
    memory_add.add_argument("--kind", default="lesson")
    memory_add.add_argument("--scope", default="global")
    memory_commands.add_parser("list")
    memory_delete = memory_commands.add_parser("delete")
    memory_delete.add_argument("id", type=int)

    candidates = subcommands.add_parser("candidates", help="Review learned candidates.")
    candidate_commands = candidates.add_subparsers(dest="candidate_command", required=True)
    candidate_commands.add_parser("list")
    candidate_approve = candidate_commands.add_parser("approve")
    candidate_approve.add_argument("id", type=int)
    candidate_reject = candidate_commands.add_parser("reject")
    candidate_reject.add_argument("id", type=int)

    episodes = subcommands.add_parser("episodes", help="List recorded episodes.")
    episodes.add_argument("--limit", type=int, default=20)
    episodes.add_argument(
        "--include-content",
        action="store_true",
        help="Include captured previews/full content. May expose sensitive data.",
    )
    episode_commands = episodes.add_subparsers(dest="episode_command")
    episode_delete = episode_commands.add_parser("delete")
    episode_delete.add_argument("id", type=int)

    subcommands.add_parser(
        "compact-deleted",
        help="Checkpoint WAL and VACUUM deleted local content.",
    )

    purge = subcommands.add_parser(
        "purge-profile",
        help="Delete a profile from all state generations and optional backups.",
    )
    purge.add_argument("--include-backups", action="store_true")
    purge.add_argument("--yes", action="store_true")

    evals = subcommands.add_parser("evals", help="Manage eval definitions.")
    eval_commands = evals.add_subparsers(dest="eval_command", required=True)
    eval_commands.add_parser("list")
    eval_add = eval_commands.add_parser("add")
    eval_add.add_argument("name")
    eval_add.add_argument("--query", action="append", default=[])
    eval_add.add_argument("--require", action="append", default=[])
    eval_add.add_argument("--forbid", action="append", default=[])
    eval_add.add_argument(
        "--severity",
        choices=["advisory"],
        default="advisory",
    )

    return parser


def doctor(store: PersonalAgentStore) -> dict[str, Any]:
    install_manifests = sorted((store.layout.control_dir / "installs").glob("*.json"))
    install_records: list[dict[str, Any]] = []
    for path in install_manifests:
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            record["manifest_path"] = str(path)
            install_records.append(record)
        except (OSError, json.JSONDecodeError):
            install_records.append({"manifest_path": str(path), "status": "invalid"})
    matching_install = next(
        (
            record
            for record in install_records
            if record.get("profile_id") == store.profile_id and record.get("settings_path")
        ),
        None,
    )
    claude_settings = (
        Path(matching_install["settings_path"])
        if matching_install
        else Path.home() / ".claude" / "settings.json"
    )
    hook_events: list[str] = []
    owned_hooks: list[dict[str, Any]] = []
    auto_memory_directory = None
    settings_error = None
    if claude_settings.exists():
        try:
            settings = load_settings(claude_settings)
            hooks = settings.get("hooks", {})
            hook_events = sorted(hooks)
            for event, groups in hooks.items():
                for group in groups:
                    for handler in group.get("hooks", []):
                        if hook_owned_by_personal_agent(handler):
                            owned_hooks.append(
                                {
                                    "event": event,
                                    "command": handler.get("command"),
                                    "args": handler.get("args", []),
                                    "command_exists": Path(
                                        str(handler.get("command") or "")
                                    ).exists(),
                                }
                            )
            auto_memory_directory = settings.get("autoMemoryDirectory")
        except (ValueError, json.JSONDecodeError) as error:
            settings_error = str(error)
    active_generation = None
    database = None
    data_schema = None
    database_check = None
    try:
        active_generation = store.layout.active_generation_id()
        if active_generation:
            database_path = store.layout.active_db()
            database = str(database_path)
            connection = sqlite3.connect(database_path, timeout=5)
            try:
                data_schema = connection.execute("PRAGMA user_version").fetchone()[0]
                database_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                connection.close()
    except Exception as error:
        database_check = f"{type(error).__name__}: {error}"
    return {
        "home": str(store.home),
        "profile_id": store.profile_id,
        "initialized": active_generation is not None,
        "active_generation": active_generation,
        "database": database,
        "data_schema_version": data_schema,
        "database_check": database_check,
        "python": sys.version.split()[0],
        "claude": claude_version(),
        "claude_settings": str(claude_settings),
        "claude_settings_error": settings_error,
        "claude_hook_events": hook_events,
        "personal_agent_hooks": owned_hooks,
        "claude_auto_memory_directory": auto_memory_directory,
        "install_manifests": install_records,
        "package_importable": bool(__import__("importlib.util").util.find_spec("personal_agent")),
    }


def episode_view(row: sqlite3.Row, *, include_content: bool) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "profile_id": row["profile_id"],
        "workspace_id": row["workspace_id"],
        "session_id": row["session_id"],
        "prompt_id": row["prompt_id"],
        "prompt_digest": row["prompt_digest"],
        "response_digest": row["response_digest"],
        "capture_mode": row["capture_mode"],
        "status": row["status"],
        "engine": row["engine"],
        "model": row["model"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }
    if include_content:
        result.update(
            {
                "user_prompt": row["user_prompt"],
                "assistant_response": row["assistant_response"],
                "prompt_preview": row["prompt_preview"],
                "response_preview": row["response_preview"],
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    store = PersonalAgentStore(args.home, profile_id=args.profile)

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
                    settings_path=args.settings,
                    dry_run=args.dry_run,
                    enable_auto_memory=args.enable_auto_memory,
                    allow_unsupported_claude=args.allow_unsupported_claude,
                    allow_unstable_hook_executable=args.allow_unstable_hook_executable,
                    hook_executable=args.hook_executable,
                )
            )
        elif args.command == "uninstall-claude":
            print_json(
                uninstall_claude(
                    store,
                    settings_path=args.settings,
                )
            )
        elif args.command == "backup":
            store.initialize()
            with store.layout.locked(timeout=60):
                destination = store.layout.snapshot_active(label=args.label)
            print_json({"backup": str(destination)})
        elif args.command == "restore":
            store.layout.ensure_directories()
            generation = store.layout.restore_backup(
                args.backup_dir,
                max_schema_version=CURRENT_SCHEMA_VERSION,
            )
            print_json({"restored_generation": generation})
        elif args.command == "export":
            destination = export_profile(
                store,
                args.destination,
                include_episodes=args.include_episodes,
            )
            print_json({"export": str(destination)})
        elif args.command == "import":
            imported = import_profile(
                store.home,
                args.bundle,
                target_profile=args.target_profile,
                activate_assets=args.activate_assets,
            )
            print_json(imported.status())
        elif args.command == "memory":
            if args.memory_command == "add":
                memory_id = store.add_memory(args.text, kind=args.kind, scope=args.scope)
                print_json({"memory_id": memory_id})
            elif args.memory_command == "delete":
                print_json({"deleted": store.delete_memory(args.id)})
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
            if args.episode_command == "delete":
                print_json({"deleted": store.delete_episode(args.id)})
            else:
                print_json(
                    [
                        episode_view(row, include_content=args.include_content)
                        for row in store.list_episodes(limit=args.limit)
                    ]
                )
        elif args.command == "compact-deleted":
            store.compact_deleted_content()
            print_json({"compacted": True})
        elif args.command == "purge-profile":
            if not args.yes:
                raise ValueError("purge-profile requires --yes.")
            print_json(
                purge_profile(
                    store,
                    include_backups=args.include_backups,
                )
            )
        elif args.command == "evals":
            if args.eval_command == "list":
                print_json([dict(row) for row in store.list_evals()])
            else:
                eval_id = store.add_eval(
                    name=args.name,
                    query_terms=args.query,
                    required_any=args.require,
                    forbidden_any=args.forbid,
                    severity=args.severity,
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
