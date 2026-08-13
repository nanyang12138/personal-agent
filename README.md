# Personal Agent Workflow

A user-owned Rules, Skills, Memory, and Evals layer for existing AI coding agents.

The model is a replaceable engine. The durable assets remain local:

```text
normal prompt
  -> relevant personal Rules, Memory, Skills, and Evals are injected
  -> Claude Code executes normally
  -> the completed turn is recorded as an episode
  -> explicit corrections become learning candidates
  -> repeated low-risk lessons may be promoted to local memory
```

The first adapter targets Claude Code because its `UserPromptSubmit` and `Stop`
hooks provide deterministic per-turn integration. Cursor support will reuse the
same package and state through a later MCP/SDK adapter.

## What this MVP does

- Creates a local Personal Agent package and SQLite state store.
- Keeps canonical Rules, Skills, Memory, Evals, and episodes outside model vendors.
- Injects relevant context before every Claude Code prompt through a hook.
- Uses Claude Code Auto Memory in a user-controlled local directory.
- Records completed turns automatically.
- Detects explicit corrections and preferences in later user prompts.
- Stages learning candidates and promotes repeated low-risk lessons.
- Selects task-relevant Skills and Evals.
- Runs response-level Evals after each turn.
- Preserves existing Claude Code settings and creates a timestamped backup.

## What this MVP does not claim

- It is not a new foundation model or a replacement for Claude Code/Cursor.
- Local state ownership does not make cloud inference provider-invisible.
- It does not store credentials or employer-confidential data.
- It does not automatically modify active Rules, Skills, permissions, or tools.
- Its first learning classifier is deliberately conservative and heuristic.

## Install

Use a personal Claude Code environment. Do not install this into an
employer-managed profile unless the organization explicitly approves it.

```powershell
cd "C:\path\to\personal-agent"
py -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

Initialize the local package:

```powershell
personal-agent init
personal-agent status
```

Preview the Claude Code settings changes:

```powershell
personal-agent install-claude --dry-run
```

Install the hooks and configure Auto Memory:

```powershell
personal-agent install-claude
```

Restart Claude Code. After installation, use Claude Code normally. No slash
command is required.

## Normal operation

For every main-session prompt, the `UserPromptSubmit` hook:

1. records the new turn;
2. detects whether it contains explicit correction/preference language;
3. retrieves relevant active memories;
4. selects relevant Skills;
5. injects applicable Eval obligations.

For every completed main-session response, the `Stop` hook:

1. records the final assistant response;
2. completes the episode;
3. runs relevant Evals;
4. optionally asks Claude to continue when enforcement is enabled.

Auto Memory remains managed by Claude Code in:

```text
~/.personal-agent/memory/
```

The Personal Agent runtime state is stored in:

```text
~/.personal-agent/state.db
```

## Inspect and manage learning

```powershell
personal-agent status
personal-agent episodes --limit 10
personal-agent candidates list
personal-agent memory list
personal-agent evals list
```

Approve or reject a staged candidate manually:

```powershell
personal-agent candidates approve 3
personal-agent candidates reject 4
```

Add an explicit memory:

```powershell
personal-agent memory add "Prefer conclusions before implementation detail." --kind preference
```

Build the context package for debugging:

```powershell
personal-agent context "Investigate this failure and verify the root cause."
```

## Learning policy

The default package follows these rules:

- Every turn may be recorded as an episode.
- Explicit corrections are staged as candidates.
- A similar low-risk preference or lesson repeated three times may be promoted.
- Rules, Skills, permissions, and external-action policies are never auto-promoted.
- Sensitive-looking content is rejected from memory and candidates.
- Active package files remain versionable and reviewable.

Change the policy in:

```text
~/.personal-agent/package/manifest.json
```

## Evals

Evals are selected by task terms and checked against the final response.
The MVP includes examples for:

- requiring validation evidence for implementation/fix tasks;
- requiring direct prior-art discussion for patent claims.

By default, failures are recorded but do not force another Agent turn. To make
failed Evals ask Claude to continue:

```powershell
$env:PERSONAL_AGENT_ENFORCE_EVALS = "1"
claude
```

Keep enforcement off until the Eval suite is calibrated; weak Evals can create
unnecessary loops.

## Privacy and work-data boundary

- Local Rules, Skills, Auto Memory, and episodes remain on the current machine.
- Any selected context injected into Claude is processed by the configured model provider.
- Never store passwords, API keys, private keys, customer data, unapproved
  employer code, logs, or internal processes in the personal package.
- Keep personal and employer-managed Claude Code environments separate.
- Store API credentials in environment variables or an approved secret manager,
  not in `settings.json`.

## Project status

Phase 1:

- [x] Local package and state store
- [x] Automatic Claude Code prompt/stop hooks
- [x] Rules, Skills, Memory, Evals context builder
- [x] Episode recording and candidate learning
- [x] Conservative Eval runner
- [ ] Cursor MCP adapter
- [ ] Cursor SDK deterministic adapter
- [ ] Learned local retrieval/ranking
- [ ] Cross-engine conformance tests
