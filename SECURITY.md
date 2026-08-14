# Security Policy

## Current status

Personal Agent is pre-release software. Do not use it to store credentials,
private keys, regulated personal data, customer data, or unapproved
employer-confidential material.

## Supported versions

Only the latest tagged release receives security fixes during the alpha period.

## Reporting a vulnerability

Do not open a public issue containing secrets, personal data, or exploit details.
Use the repository's private GitHub Security Advisory reporting flow.

Include:

- affected version and operating system;
- installation method;
- minimal reproduction without real secrets;
- impact and expected behavior;
- whether the issue affects Rules, Skills, Memory, Evals, Hooks, migration,
  backup, import/export, or the update path.

## Trust boundaries

- Local state is readable by the current operating-system user.
- Selected context injected into Claude/Cursor is processed by that provider.
- Rules, Skills, and Memory are untrusted content until reviewed and activated.
- Personal Agent Hooks are availability features and fail open on internal errors.
- Security policies for external side effects must remain in the host Agent,
  operating system, or an independently enforced Hook.

## Security defaults

- Episode capture is disabled.
- Correction staging is disabled.
- Automatic promotion is disabled.
- Auto Memory takeover is opt-in during Claude installation.
- Evals are advisory unless explicitly configured otherwise.
- Import targets a new profile and never overwrites an existing profile.
- Upgrades migrate a cloned data generation and preserve the previous generation.
