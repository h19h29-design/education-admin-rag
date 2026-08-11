# Hermes OAuth preview RAG

This runbook deploys the incomplete SEN-QA preview index to the existing
`hermes2` profile. Hermes remains the only answer-generation layer and uses the
already authenticated `openai-codex` OAuth provider. No OpenAI API key, Ollama,
NAS compute, public listener, or production alias is required.

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

Confirm the selected profile before installation:

```sh
hermes --profile hermes2 auth status openai-codex
hermes --profile hermes2 config get model.provider
hermes --profile hermes2 config get model.default
```

Run the repository installer with absolute paths to the preview database,
attestation, and independently recorded attestation SHA-256. It installs only:

- `~/.local/bin/senqa-preview-search`
- `~/.config/senqa-preview-rag/config.json`
- `~/.hermes/profiles/hermes2/skills/sen-qa-preview-rag/SKILL.md`

The installer rejects mismatched authority, symlinks, and conflicting existing
files. An exact reinstall is idempotent.

## Use

Start the existing loopback-only Hermes dashboard:

```sh
hermes --profile hermes2 dashboard --host 127.0.0.1 --no-open
```

Ask a source-grounded question and explicitly invoke the `sen-qa-preview-rag`
skill if Hermes does not select it automatically. Do not expose the dashboard
on a public interface.

## Promotion boundary

This preview is for user evaluation only. Do not promote it to the canonical
RAG alias until the quarantine-resolution and human review contracts are
complete. Installing this skill never changes source artifacts, review state,
NAS data, or release aliases.
