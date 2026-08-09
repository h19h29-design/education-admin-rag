# Encrypted backup and isolated restore runbook

Backups are valid only on a different storage failure domain: an external disk,
another NAS, or an approved remote repository. A directory beneath source,
artifact, or private-eval storage is rejected.

## Offline keys

Generate the age identity and minisign secret key in the administrator's
offline key store. Keep both secret files outside Git, the NAS, container build
context, release artifacts, and backup media. The NAS receives only the public
age recipient and minisign public key for the duration needed by the operator.

Do not use `config/backup-recipients.txt.example` directly. Copy it outside the
repository and replace the comment with the reviewed public `age1...` recipient.
The backup script accepts the real path through
`SEN_QA_BACKUP_RECIPIENTS_FILE`.

The backup image contains byte-pinned age 1.3.1 and minisign 0.12 from
`config/backup-tools.lock.json`. It is Linux/amd64, digest-pinned, non-root, and
contains neither lock/build scripts nor Python application code at runtime.
Build it from a commit archive, not a working directory containing artifacts:

```bash
docker build --platform linux/amd64 --network default \
  -f docker/backup.Dockerfile -t education-admin-backup:RELEASE-COMMIT .
docker image inspect --format '{{index .RepoDigests 0}}' \
  education-admin-backup:RELEASE-COMMIT
```

Record the resulting digest-qualified reference as `SEN_QA_BACKUP_IMAGE`.

## Backup

The canonical SQLite file is copied through SQLite's online backup API. The
Qdrant snapshot, approved source manifest, model lock, and evaluation report are
copied from stable regular-file descriptors. The private blind labels are never
copied in plaintext; the pinned container writes only `blind-labels.age`.

```bash
export SEN_QA_BACKUP_IMAGE='IMAGE:TAG@sha256:...'
export SEN_QA_BACKUP_RECIPIENTS_FILE=/operator/public-age-recipients.txt
export SEN_QA_ATTESTATION_SECRET_KEY_FILE=/offline/minisign-release.key
bash scripts/backup-release.sh /EXTERNAL/BACKUP-ROOT
```

The exact six payloads are hashed into `bundle-manifest.json`. Extra files,
missing files, symlinks, non-regular files, byte changes, duplicate JSON keys,
or a noncanonical manifest fail verification. The manifest signature is stored
beside, not inside, the bundle to avoid a self-referential hash.

## Isolated restore

```bash
export SEN_QA_ATTESTATION_PUBLIC_KEY_FILE=/operator/minisign-release.pub
export SEN_QA_BACKUP_IDENTITY_FILE=/offline/education-admin-backup.agekey
bash scripts/restore-release.sh /EXTERNAL/BACKUP-ROOT
```

Restore verifies the minisign signature and rehashes every bundle byte before
decrypting. Blind labels go only to a new mode-0700
`private-eval/restore/$SEN_QA_RELEASE_ID` directory. Canonical SQLite and the
Qdrant snapshot go to a new artifact restore namespace. No current alias is
touched.

The script deliberately exits with `restore_evaluation_driver_required` after
materialization. A complete release requires the same 200-question evaluation
in that isolated namespace and a signed restore attestation with the exact
backup bundle SHA. Until that driver and the real SME gold set exist, promotion
is blocked.
