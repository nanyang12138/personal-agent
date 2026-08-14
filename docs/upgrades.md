# Upgrades, Backup, and Recovery

## Guarantees in 0.2 alpha

- User Rules, Skills, Evals, and Memory files are stored outside the package.
- Database schema changes run on a cloned generation.
- A verified physical snapshot is created before migration.
- The previous generation remains available after migration.
- Future schema versions are rejected.
- Claude settings changes use atomic replacement and can be uninstalled without
  deleting Personal Agent data.
- Portable exports import into a new profile and never overwrite an existing
  profile.

## Not yet guaranteed

- Atomic rollback of both Python runtime and data after an external
  `pipx upgrade` or `uv tool upgrade`.
- Signed update manifests and side-by-side runtime switching.
- Automatic rollback after a new runtime passes migration but later fails an
  application-level health check.
- Encrypted backup bundles.

These are release-blocking requirements for a stable 1.0 updater.

## Safe external upgrade

During alpha:

```text
personal-agent doctor
personal-agent backup --label before-upgrade
uv tool upgrade personal-agent
personal-agent doctor
```

On first execution, a new application version migrates a cloned state
generation if required.

If the new version fails before accepting new writes:

```text
personal-agent restore <backup-directory>
uv tool install personal-agent==<previous-version> --force
```

Never copy a live SQLite database without the online backup command.

## Logical portability

Export the active profile:

```text
personal-agent export profile.pa-bundle
```

Episodes are excluded by default:

```text
personal-agent export profile-with-episodes.pa-bundle --include-episodes
```

Import into a new isolated profile:

```text
personal-agent import profile.pa-bundle --target-profile imported
```

Imported executable assets are quarantined by default. `--activate-assets`
is an explicit trust decision for a bundle the user created and reviewed.
Exports are checksummed but not signed or encrypted. Treat them as sensitive
files.

## Recovery principles

- Restore always creates a new generation.
- Restore never overwrites an active generation.
- A failed checksum prevents restore.
- A damaged future schema is not guessed or repaired automatically.
- `doctor` is read-only.
- Auto-fix features must create and validate a backup before any write.
