# Local preview RAG

This runbook deploys the incomplete SEN-QA preview index to a locally configured
answer-generation profile. The profile and authentication provider are supplied
outside Git. No API key, local model, NAS compute, public listener, or production
alias is required by this repository workflow.

## Safety status

The current preview is not a canonical release. It excludes restricted rows and
marks every result with both:

- `unreviewed_incomplete_preview`
- `production_eligible=false`

Hermes must repeat that limitation, ground answers only in returned rows, and
cite the case ID, edition year, and PDF page indexes. The local tool opens the
SQLite index read-only after verifying the independently recorded attestation
and database hashes.

## Install

Confirm the privately selected profile and provider before installation. Do not
record their names or authentication output in the repository:

```sh
<local-agent-command> auth status
<local-agent-command> config get model.provider
<local-agent-command> config get model.default
```

Run the repository installer with absolute paths to the preview database,
attestation, and independently recorded attestation SHA-256. It installs only:

- `~/.local/bin/senqa-preview-search`
- `~/.config/senqa-preview-rag/config.json`
- `<private-profile-root>/skills/sen-qa-preview-rag/SKILL.md`

The installer rejects mismatched authority, symlinks, and conflicting existing
files. An exact reinstall is idempotent.

## Use

Start the existing loopback-only local dashboard:

```sh
<local-agent-command> dashboard --host 127.0.0.1 --no-open
```

Ask a source-grounded question and explicitly invoke the `sen-qa-preview-rag`
skill if the executor does not select it automatically. Do not expose the dashboard
on a public interface.

## Promotion boundary

This preview is for user evaluation only. Do not promote it to the canonical
RAG alias until the quarantine-resolution and human review contracts are
complete. Installing this skill never changes source artifacts, review state,
NAS data, or release aliases.
