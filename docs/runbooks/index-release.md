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
- the SHA-256 of the final `review-ready.attestation.json`, captured after review;
- the exact offline BGE-M3 cache root, embedding lock fingerprint, and tokenizer
  runtime fingerprint bound to `uv.lock` plus the digest-qualified indexer image;
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
pages in network-disabled, read-only containers. It then validates the managed
annual JSONL layout, derives privacy/quality assessments, binds every raw role
span into a promotion envelope, and writes one owner-only
`CanonicalReviewRegistry` plus `ReviewStore`. It exits successfully at
`stage=review_pending`; it does not create canonical storage or an index.
Unknown files, quarantined source provenance, hash drift, an existing review
package, or malformed parser input fail closed without printing candidate text.

Likewise, `build-indexes.sh` first requires the canonical DB and an independent
`review-ready.attestation.json`. It builds the lexical candidate through the
real atomic builder, starts the digest-pinned isolated Qdrant service, verifies
the complete offline BGE-M3 cache, and builds a release-named dense candidate.
The job checks the exact point count and a deterministic vector sample against
what it just encoded, then binds those results to the physical canonical and
lexical database hashes in `index-attestation.json`. It never changes the
production alias.

Human review and the separately persisted ready attestation remain mandatory
pre-deployment checkpoints, not operator override points.

After every case is terminal, export the checkpoint and capture its hashes
without printing any candidate content:

```bash
python -m src.cli review export-ready \
  --package "$SEN_QA_ARTIFACT_ROOT/releases/$SEN_QA_RELEASE_ID/review" \
  --release-id "$SEN_QA_RELEASE_ID" \
  --registry-sha256 "$SEN_QA_REVIEW_REGISTRY_SHA256"
export SEN_QA_READY_ATTESTATION_SHA256="$(shasum -a 256 \
  "$SEN_QA_ARTIFACT_ROOT/releases/$SEN_QA_RELEASE_ID/review/review-ready.attestation.json" \
  | awk '{print $1}')"
```

An independent operator supplies `SEN_QA_REVIEW_REGISTRY_SHA256`,
`SEN_QA_EMBEDDING_MODEL_LOCK_SHA256`, `SEN_QA_RUNTIME_FINGERPRINT_SHA256`, and
the absolute, nonsymlink `SEN_QA_MODEL_CACHE_ROOT`. The runtime fingerprint is
the reviewed domain-separated SHA-256 over the exact `uv.lock` digest and the
digest portion of `SEN_QA_INDEXER_IMAGE`; do not substitute a hostname, tag, or
wall-clock value. Derive it with the checked-in implementation and independently
record the result:

```bash
export SEN_QA_RUNTIME_FINGERPRINT_SHA256="$(uv run python -c \
  'import os; from pathlib import Path; from src.corpus.chunking import tokenizer_runtime_fingerprint_sha256 as f; print(f(Path("uv.lock").read_bytes(), indexer_image_digest=os.environ["SEN_QA_INDEXER_IMAGE"].rsplit("@", 1)[1]))')"
```

The finalizer recomputes this value from `/work/uv.lock` inside the pinned
indexer image before reading the review package. For the first release only, explicitly set
`SEN_QA_INITIALIZE_ISSUANCE_GENESIS=1`; subsequent releases must reuse the
existing owner-only issuance registry and leave that variable unset. Then run:

```bash
bash scripts/finalize-corpus.sh
unset SEN_QA_INITIALIZE_ISSUANCE_GENESIS
```

`finalize-corpus.sh` revalidates the attested documents, manifest/page counts,
registry, terminal snapshot, every promotion envelope, role-source authority,
locked tokenizer bytes, and persistent issuance predecessor. It derives final
review-controlled Case fields and chunks internally, writes SQLite plus JSONL
through the atomic canonical builder, and stops at `stage=canonical_ready`.
It does not start Qdrant, build an index, upload anything, or mutate a
production alias. `build-indexes.sh` is the next explicit pre-deployment step.

## Evaluation and verification

`evaluate-release.sh` requires both checked-in public gold files, the private
blind label file, the canonical database, and exactly 200 owner-only JSONL
observations for ingestion, substring, lexical, dense, and hybrid retrieval at
`$SEN_QA_PRIVATE_EVAL_ROOT/observations/$SEN_QA_RELEASE_ID/`. Missing files stop
at `evaluation_observations_missing`. The evaluator rebinds every observation
to the reviewed gold labels and canonical source-span evidence, writes only
aggregate metrics, and fails when either release gate is red. Synthetic labels,
partial question sets, symlinks, or group-readable private observations cannot
satisfy this gate. `verify-release.sh` similarly requires canonical, index, and
evaluation evidence and never promotes an alias.

Only after the evaluator writes a green aggregate report and the index job
writes `index-attestation.json` may `verify-release.sh` proceed. It derives
`release-evidence.json` itself by rehashing the canonical manifest/database and
lexical index, checking dense counts/sample hash, rereading canonical
review/ingestion/privacy state, and validating the evaluation gates. A
caller-authored all-green evidence file is rejected. The script then creates and
minisign-signs the verification attestation. Its release ID and bundle SHA must
exactly match the isolated restore attestation.

## Promotion and reconciliation

`src.release.promote_release` creates a pending manifest, performs the alias
transition, and atomically replaces `current.json`; a manifest failure attempts
an alias rollback. `promote-release.sh` remains fail-closed with
`qdrant_alias_broker_required` because Qdrant does not expose a true conditional
alias CAS by itself. Deployment needs a service-owned exclusive broker/lock and
startup reconciliation that fails readiness whenever alias and current manifest
disagree. Do not replace this with a read-then-write HTTP sequence.
