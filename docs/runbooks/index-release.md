# Corpus index release runbook

This runbook stops immediately before any production mutation. It does not
authorize a NAS upload, container deployment, Qdrant alias change, or current
manifest replacement.

## Required operator-supplied values

Obtain these from the target NAS and the independent reviewers. Do not invent
or copy example values into production:

- distinct non-root ingestion, search, and evaluator UIDs;
- the reviewer group GID and the current reviewer's real UID;
- digest-qualified ingestion, indexer, backup, and permission-probe images;
- the independently reviewed canonical registry and review decision snapshot;
- the public 140-question development set, public 60 blind questions, and the
  private 60 blind labels approved by SMEs;
- the offline attestation public key and an operator-only signing key path.

Copy `config/storage-policy.toml.example` to an operator-managed path, replace
every placeholder with observed NAS values, and validate it with:

```bash
uv run python -m src.cli storage-policy-env --policy /operator/storage-policy.toml
```

The command emits only shell-quoted UID/GID/root fields. Any malformed,
symlinked, FIFO, root, shared-UID, relative-root, or overlapping-root policy is
rejected.

## Start a release

Create an immutable environment only after the three real roots already exist:

```bash
uv run python -m src.cli start-release \
  --source-root /REAL/SOURCE \
  --artifact-root /REAL/ARTIFACTS \
  --private-eval-root /REAL/PRIVATE-EVAL \
  --env-file /REAL/ARTIFACTS/active-release.env
set -a
. /REAL/ARTIFACTS/active-release.env
set +a
```

Set the digest-qualified image variables and policy path, then run:

```bash
SEN_QA_STORAGE_POLICY=/operator/storage-policy.toml \
SEN_QA_PERMISSION_PROBE_IMAGE='IMAGE:TAG@sha256:...' \
bash scripts/verify-storage-permissions.sh
```

Before running it, an administrator creates five value-free, zero-content ACL
probe files: `.sen-qa-permission-probe` in source and private-eval, plus
`.sen-qa-ingestion-probe`, `.sen-qa-canonical-permission-probe`, and
`.sen-qa-review-permission-probe` in artifacts. Apply the same owner/group ACLs
that the corresponding production paths will use.

The four probes mount the roots read/write so Docker does not mask an unsafe
host ACL. They enforce: ingestion source read/no-write and artifact write;
search canonical read/no-write with no review/private-label access; reviewer
review read/write with no private-label access; evaluator private-label
read/no-write. An unexpected success is a failure, just like an unexpected
denial.

## Build status and human checkpoint

`build-corpus.sh` runs source verification and all 1,877 approved extraction
pages in network-disabled, read-only containers. It intentionally exits with
code 3 and `candidate_review_bridge_required` after metadata extraction. The
current parser APIs provide candidates in memory, but no reviewed production
driver yet serializes privacy/quality decisions, role-source authority,
CanonicalReviewRegistry, and ReviewStore in one externally pinned transaction.
Treating metadata as canonical would bypass the review boundary.

Likewise, `build-indexes.sh` first requires the canonical DB and an independent
`review-ready.attestation.json`. It builds the lexical candidate through the
real atomic builder, then exits with `dense_index_driver_required` until the
offline BGE-M3/Qdrant job writes a count-and-vector-hash attestation without
changing the production alias.

These code gaps are pre-deployment blockers, not operator override points.

## Evaluation and verification

`evaluate-release.sh` fails unless both checked-in public gold files and the
private blind label file exist. It currently stops at
`retrieval_evaluation_driver_required`; no synthetic labels or partial question
set may satisfy this gate. `verify-release.sh` similarly requires canonical,
index, and evaluation evidence and never promotes an alias.

Only after a real evaluator driver produces canonical `release-evidence.json`
with all ingestion/retrieval/privacy and latency gates may
`verify-release.sh` create and minisign the verification attestation. The attestation
release ID and bundle SHA must exactly match the isolated restore attestation.

## Promotion and reconciliation

`src.release.promote_release` creates a pending manifest, performs the alias
transition, and atomically replaces `current.json`; a manifest failure attempts
an alias rollback. `promote-release.sh` remains fail-closed with
`qdrant_alias_broker_required` because Qdrant does not expose a true conditional
alias CAS by itself. Deployment needs a service-owned exclusive broker/lock and
startup reconciliation that fails readiness whenever alias and current manifest
disagree. Do not replace this with a read-then-write HTTP sequence.
