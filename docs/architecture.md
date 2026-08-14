# Architecture

## Product boundary

Personal Agent is not a foundation model and not a new IDE Agent. It is a
user-owned capability package:

```text
Rules + Skills + Memory + Evals + Episodes
                    |
          engine-specific adapter
                    |
          Claude Code / Cursor / other engines
```

The engine performs reasoning and tool use. Personal Agent owns context
selection, portable assets, evaluation metadata, learning candidates, version
history, backup, migration, and user control.

## Storage layers

```text
~/.personal-agent/
  control/
    active.json
    install.json
  user/
    profiles/<profile>/
      manifest.json
      rules/
      skills/
      evals/
      memory/
  state/
    generations/<generation>/
      generation.json
      state.db
  backups/
  cache/
  logs/
  runtime/
```

- Built-in defaults ship in application code and are immutable.
- User files are overlays and are never overwritten by package upgrades.
- SQLite state lives in immutable generations selected by `active.json`.
- Cache and future indexes are disposable.
- Physical backups and logical exports are distinct formats.

## Version dimensions

- Application version: package SemVer.
- Layout version: directory structure.
- Data schema version: `PRAGMA user_version` plus `schema_migrations`.
- Export format version: portable bundle schema.
- Skill/eval format versions: independent of database schema.

Old applications must refuse write access to future schema versions.

## Upgrade transaction

When a schema upgrade is needed:

1. acquire the upgrade lock;
2. verify the active database;
3. create and checksum a physical backup;
4. clone the active database into a new generation using SQLite online backup;
5. run ordered migrations against the clone;
6. run SQLite integrity and foreign-key checks;
7. write the generation manifest;
8. atomically replace `active.json`;
9. retain the old generation and backup.

The active database is never migrated in place.

This protects user data migration, but full code-and-data atomic upgrades still
require a future stable launcher that manages side-by-side runtime versions.

## Learning lifecycle

```text
observed evidence
  -> quarantined candidate
  -> static validation
  -> eval
  -> shadow/canary (future)
  -> user-approved active asset
  -> superseded/revoked
```

Defaults:

- capture off;
- staging off;
- auto promotion off.

The model may propose a candidate. It cannot modify active Rules/Skills,
change hidden Evals, and approve its own promotion.

## Adapter boundary

Claude Code:

- `UserPromptSubmit` requests a bounded context package.
- `Stop` records an episode and runs configured Evals when capture is enabled.
- Auto Memory integration is optional.

Cursor:

- Native per-prompt context injection is not currently deterministic.
- The first adapter will expose the same context package through local MCP and
  an always-on Rule.
- A deterministic adapter requires Cursor SDK/ACP as an alternate entry point.

Adapters are projections. They are not the source of truth and can be rebuilt.
