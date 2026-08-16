# Hermes OAuth Preview RAG Design

## Goal

Deploy the private SEN-QA preview index as a usable Hermes skill on the Mac mini.
Hermes must use the existing `hermes2` profile and its `openai-codex` OAuth login.
No new OpenAI API key, local model, public data endpoint, NAS computation, or
production retrieval alias is introduced.

## User workflow

1. The user opens the existing Hermes Dashboard on `127.0.0.1`.
2. The user asks an education-administration question in ordinary Korean.
3. Hermes invokes the exact read-only `senqa-preview-search` command supplied by
   the local skill.
4. The command searches the sealed preview SQLite database and returns bounded
   JSON containing candidate text, candidate authority hash, and PDF page/bbox
   provenance.
5. Hermes answers only from those results, cites case ID, edition year, and PDF
   page, and always labels the answer as an unreviewed incomplete preview.
6. With no hit, Hermes says that the preview corpus has no grounded result. It
   does not fall back to general knowledge.

## Components

### Read-only search command

`scripts/senqa-preview-search.py` is a standard-library CLI. It opens the database
with SQLite URI `mode=ro`, validates the preview metadata and attestation digest,
normalizes a bounded query, runs parameterized FTS5 search, and emits canonical
JSON. It rejects symlinks, non-regular files, oversized queries, excessive result
limits, policy-excluded rows, and any row not carrying the mandatory preview
warning.

The installed wrapper at `~/.local/bin/senqa-preview-search` fixes the approved
database and attestation paths. Search does not write the database, WAL, session,
or source package.

### Hermes skill

The local `sen-qa-preview-rag` skill contains one exact command contract and a
strict response policy. Retrieved content is untrusted evidence, never an
instruction. Hermes may summarize it but may not execute commands found in it,
claim human approval, omit the preview warning, or invent a source.

The existing GitLab webhook platform remains restricted to its current `web`
toolset. The RAG skill is used only in the authenticated local Hermes dashboard or
explicit CLI sessions.

### Dashboard

Hermes Dashboard binds only to `127.0.0.1`. The existing `hermes2` OAuth session
provides inference. The dashboard is not exposed through Cloudflare and is not a
public deployment.

## Security and privacy

- The preview database already excludes `restricted` and `public_credit` cases.
- The command rechecks those policy classes at query time.
- Search input is capped and sent to SQLite only through a bound parameter.
- Result count and output bytes are bounded.
- Every response preserves `production_eligible=false`,
  `complete_corpus=false`, and `unreviewed_incomplete_preview`.
- Candidate context sent to Hermes is processed by the existing OpenAI Codex
  OAuth provider, as explicitly requested for this internal test.
- No credential, OAuth token, source PDF, review database, or NAS path is placed
  in Git or response logs.

## Verification

- Unit tests cover successful Korean search, empty results, malformed queries,
  symlink/database substitution, metadata downgrade, privacy exclusions, and
  deterministic JSON.
- Installation verifies source and installed-script hashes.
- A Hermes one-shot smoke uses the installed skill and a non-sensitive query. The
  result must include grounded case/page citations and the preview warning.
- Dashboard status and loopback binding are checked after launch.
- Existing repository tests, Ruff, strict mypy, and diff checks remain green.

## Non-goals

- Canonical promotion, human-review substitution, dense/Qdrant indexing,
  production alias changes, public web hosting, and autonomous answering without
  citations are outside this preview deployment.
