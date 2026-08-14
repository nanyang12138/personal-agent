# Personal Agent

A user-owned Rules, Skills, Memory, and Evals layer for existing AI engines.

```text
normal prompt
  -> bounded task-relevant personal context
  -> Claude Code / Cursor executes normally
  -> optional local episode and Eval processing
  -> reviewed candidate learning
```

The model is replaceable. User assets, versions, migrations, backups, and
portable exports remain local.

## Project status

Version `0.2.0` is an alpha production-foundation refactor.

It is intentionally conservative:

- episode capture is off;
- correction staging is off;
- automatic promotion is off;
- Claude Auto Memory takeover is opt-in;
- response-text Evals are advisory;
- internal Hook errors fail open.

Do not use this alpha for secrets, regulated data, customer data, or unapproved
employer-confidential material.

## Current capabilities

- Immutable built-in Rules/Skills plus user-owned overlays.
- Isolated profiles and hashed workspace namespaces.
- Versioned SQLite state generations.
- Ordered migrations with `PRAGMA user_version` and migration journal table.
- Pre-migration SQLite online backup.
- Integrity verification and future-schema refusal.
- Atomic active-generation switching and backup restore.
- Claude Code `UserPromptSubmit` and `Stop` adapters.
- Safe Hook `command + args` installation with version gating.
- Atomic Claude settings update and product-owned uninstall.
- Bounded context assembly.
- Redacted, opt-in episode capture.
- Manual candidate approval.
- Advisory/required/blocking Eval severities.
- Logical profile export/import with checksums.
- Concurrency, migration, privacy, install, recovery, and portability tests.

See [architecture](docs/architecture.md), [upgrades](docs/upgrades.md), and
[security policy](SECURITY.md).

## Install for development

```powershell
git clone https://github.com/nanyang12138/personal-agent.git
cd personal-agent
py -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

Initialize:

```powershell
personal-agent init
personal-agent status
personal-agent doctor
```

## Claude Code adapter

Claude Code `>=2.1.196` is required.

For an actual installation, use a stable tool environment:

```powershell
uv tool install .
```

Previewing an install performs no writes:

```powershell
personal-agent install-claude --dry-run
```

Install Hooks without changing Claude Auto Memory:

```powershell
personal-agent install-claude
```

An editable project `.venv` is rejected as an unstable Hook target. During
local development only:

```powershell
personal-agent install-claude --allow-unstable-hook-executable
```

Opt in to using the profile's local Memory directory as Claude Auto Memory:

```powershell
personal-agent install-claude --enable-auto-memory
```

Restart Claude Code. Normal prompts then receive bounded Rules, relevant active
Memory, matching Skills, and applicable Eval obligations. No slash command is
required.

Remove only Personal Agent's Claude settings while preserving data:

```powershell
personal-agent uninstall-claude
```

The installer refuses unsupported Claude Code versions and never prints the
rest of the user's settings, which may contain credentials.

## Profiles

Use isolated profiles:

```powershell
personal-agent --profile personal init
personal-agent --profile work init
```

Profiles have separate Rules, Skills, Memory, Evals, candidates, and queries.
Workspace-scoped Memory uses a canonical path hash instead of persisting the
raw workspace path.

## Configure capture and learning

The profile manifest is:

```text
~/.personal-agent/user/profiles/<profile>/manifest.json
```

Safe defaults:

```json
{
  "capture": {
    "episodes": false,
    "mode": "metadata",
    "preview_chars": 0,
    "retention_days": 30
  },
  "learning": {
    "stage_corrections": false,
    "auto_promote_after": 0,
    "auto_promote_kinds": []
  }
}
```

Supported capture modes:

- `metadata`: digests and lifecycle metadata only;
- `redacted`: bounded redacted previews;
- `full`: full text only when explicitly enabled; detected secrets still
  prevent raw persistence.

Automatic promotion remains disabled. The engine may stage a candidate only
when correction staging is enabled; active Memory still requires approval.

## Manage assets

```powershell
personal-agent memory add "Prefer conclusions before detail." --kind preference
personal-agent memory list
personal-agent memory delete 3

personal-agent candidates list
personal-agent candidates approve 4
personal-agent candidates reject 5

personal-agent evals list
personal-agent evals add verify-code `
  --query fix --query 修复 `
  --require test --require 测试
```

Response-text Evals are advisory only. Required/blocking Evals must use
structured tool or test evidence and are not yet exposed by the alpha CLI.

Inspect episodes without revealing stored content:

```powershell
personal-agent episodes --limit 10
```

Displaying previews or full captured content is explicit:

```powershell
personal-agent episodes --include-content
```

After deleting sensitive rows, compact the local database:

```powershell
personal-agent compact-deleted
```

Delete a profile from every retained state generation:

```powershell
personal-agent --profile personal purge-profile --yes
```

Also remove it from managed physical backups:

```powershell
personal-agent --profile personal purge-profile --include-backups --yes
```

Personal Agent cannot discover or delete export bundles copied outside its
home directory.

## Backup, restore, and portability

Create a verified physical recovery snapshot:

```powershell
personal-agent backup --label before-upgrade
```

Restore into a new state generation:

```powershell
personal-agent restore <backup-directory>
```

Export a logical profile bundle; episodes are excluded by default:

```powershell
personal-agent export profile.pa-bundle
```

Import into a new profile without overwriting existing data:

```powershell
personal-agent import profile.pa-bundle --target-profile imported
```

Imported Rules, Skills, Memory, and Evals are quarantined by default. For a
bundle you created and reviewed yourself:

```powershell
personal-agent import profile.pa-bundle `
  --target-profile imported `
  --activate-assets
```

Checksums detect corruption, not authorship. Bundles are not encrypted.

## Run tests

```powershell
python -m compileall -q src
python -m unittest discover -s tests -v
python -m pip check
```

CI runs on Windows, macOS, and Linux with Python 3.11 and 3.13.

## Upgrade model

During alpha, use an external isolated installer such as `uv tool` or `pipx`,
then follow [the documented safe upgrade flow](docs/upgrades.md).

Data migrations use cloned generations and preserve the previous generation.
Fully atomic runtime and data upgrades, automatic post-upgrade rollback, and a
stable launcher are not yet claimed.

## Privacy boundary

- Local state is readable by the current operating-system user.
- Context selected for Claude/Cursor is processed by that provider.
- Local ownership is not provider-invisible inference.
- Never place passwords, API keys, private keys, customer data, or unapproved
  employer data in Rules, Skills, Memory, Evals, or captured episodes.
- Keep personal and employer-managed Agent profiles separate.
