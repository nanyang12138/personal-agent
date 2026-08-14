# Changelog

## 0.2.0 - Unreleased

### Changed

- Separate immutable built-in defaults from user-owned overlays.
- Store mutable state in versioned generations selected by an atomic pointer.
- Add ordered schema migrations, integrity checks, pre-migration snapshots,
  and future-schema refusal.
- Disable episode capture, correction staging, automatic promotion, Auto Memory
  takeover, and Eval enforcement by default.
- Add profile and workspace namespaces.
- Add bounded context assembly and context-boundary escaping.
- Make Hooks fail open on internal Personal Agent errors.
- Replace shell-form Hook commands with command/args handlers.
- Add Claude Code version gating, atomic settings writes, installation
  ownership, and uninstall support.
- Add backup/restore and logical profile export/import.
- Quarantine imported executable assets unless explicitly activated.
- Require export inventory to exactly match archive contents.
- Add full-profile privacy purge across retained generations and optional backups.
- Add redacted episode capture and privacy-preserving CLI episode output.
- Preserve response revisions and bind Eval selection to the submitted prompt.
- Add StopFailure episode closure.
- Add concurrent-write, migration, recovery, privacy, and portability tests.

### Security

- Reject known secret forms from Memory and candidates.
- Redact detected credentials before persisted episode previews.
- Reject symlinked settings and user Rule/Skill files.
- Prevent automatic model-driven promotion of active personal assets.

## 0.1.0 - 2026-08-13

Initial private MVP. This version was never published as a stable release and
must not be used as the basis for upgrade guarantees.
