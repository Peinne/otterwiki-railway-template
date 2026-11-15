# Recovery drill — passed

The controlled recovery drill passed on 2026-11-15 for the pinned pilot
configuration. The template remains **DRAFT pending publication**; recovery
validation is complete. See the [sanitized Railway recovery report](validation/railway-recovery.json).

`ops/backup_data.py` prepares a **checked online copy**, not an atomic snapshot of
the whole volume. It does not stop, restart, reconfigure or restore the application.
Use a quiet window without edits, uploads, account/settings changes, Git sync or
deployments. A successful copy is not proof of a successful restoration.

## Export an existing instance

Run the script through an already authenticated service console or SSH session:

```sh
python3 /tmp/backup_data.py \
  --source /app-data \
  --output /tmp/otterwiki-recovery-2026-11-15.tar.gz
```

Use a fresh output name for each attempt. The parent directory must already exist
and be outside `/app-data`. The defaults limit execution to 120 seconds and data
to 256 MiB; SQLite's busy/retry callback also checks the deadline. A blocked OS I/O
operation can outlast a cooperative deadline. Existing output files are preserved.

The archive includes `settings.cfg`, the complete Git repository and attachments,
other persistent files (including wrapper markers), and a self-contained SQLite
backup containing accounts and preferences. SQLite is opened read-only and copied
with its Online Backup API; the live `db.sqlite`, WAL, SHM and rollback-journal
files are not copied as ordinary files. The script does not import Otter Wiki,
checkpoint the source database or change its journal mode.

Before publishing the archive, the script checks:

- SHA-256 contents, file modes, symlink targets and metadata before/after capture;
- the copied tree against the initial inventory;
- SQLite `data_version` using the same source connection, and database file identity;
- the backup database with `PRAGMA integrity_check`.

Source changes, external/absolute symlinks, special files, oversized data and
timeouts cause failure instead of a successful archive. Only our private temporary
files are cleaned up. These checks detect changes; they do not lock out every writer
or make the separate components an atomic snapshot. Avoid concurrent writes even
when the checker would usually detect them.

The result is a gzip tar archive with an `app-data/` root, file modes/timestamps and
numeric owners/groups. Temporary storage is private (0700); the archive is 0600.
JSON output contains fixed status fields and an archive checksum, never contents,
account hashes, settings, cookies or paths from the wiki.

Download the archive through the authenticated Files interface or supported
SSH/SFTP channel, then compare its SHA-256 with the reported archive checksum.
Treat the archive as a secret: it contains account password hashes and `SECRET_KEY`.
Never publish it, commit it, expose it through the wiki, or attach it to CI artifacts.
Keep a separate copy of the deployed commit, image digest and non-secret service
configuration; these are not all stored in `/app-data`.

## Offline restoration on an independent Railway volume

The pilot used an application export and
[ops/restore_data.py](../ops/restore_data.py), with Python 3.11 and Git already
available in the pinned image. It did not use Railway's native volume backup.
During this Trial pilot, the Backups panel required Pro to create a new native
backup; no upgrade was made for this recovery drill.

The operator must verify that the destination is independent and that no
application process can access it until restoration succeeds. The helper cannot
establish those platform facts and never stops a service.

1. Prepare a separate test service and a **new** volume mounted at `/app-data`.
   Read back the service and volume IDs to distinguish them from the live source;
   keep the original service, domain and mount untouched. The destination must
   contain nothing except an optional empty `lost+found`. Do not overlay existing
   data or initialize another administrator.
2. Configure the test service's Railway **Custom Start Command** for maintenance
   before starting the target. In this pilot, that command replaced the image's
   `ENTRYPOINT`; the upstream initialization and wrapper were not run. The
   maintenance process returned HTTP 200 only for `/-/healthz` and HTTP 503 for
   other paths. It was not a file server and did not expose the archive or volume.
   This health response established maintenance availability, not a working wiki.
3. Through the authenticated Console, verify that `/app-data` is the fresh mount,
   `lost+found` is empty, no nginx/uWSGI/supervisor process is active, and no
   `BOOTSTRAP_*` variable is present. Maintain this offline condition throughout
   restoration.
4. Use the authenticated Console Files interface to transfer the restore script
   and private archive into a private temporary directory outside `/app-data`.
   Retain archive mode `0600`. Compare the script's SHA-256 with the reviewed
   repository file, and the archive's SHA-256 with the trusted export report,
   after transfer. Do not serve either file through an application endpoint.
5. Run the helper with the expected archive digest. For example, after placing
   the files at these paths, substitute the actual trusted digest:

   ```sh
   python3 /tmp/restore_data.py \
     --archive /tmp/otterwiki-recovery-2026-11-15.tar.gz \
     --destination /app-data \
     --sha256 EXPECTED_SHA256_FROM_EXPORT_REPORT
   ```

   Require JSON `status: success`, verified SHA-256, SQLite and Git integrity
   `ok`, and preserved metadata. The helper validates a private staged copy,
   preserves numeric owners/modes/timestamps, handles empty `lost+found`, and
   publishes SQLite last. Publication uses multiple renames and is not atomic.
   On failure, keep the target offline; an incomplete marker or partially
   published data must be inspected, never overwritten by an automatic retry.
6. After successful offline restoration, clear **only** the maintenance Custom
   Start Command and redeploy the same pinned application image/commit. Clearing
   the override restores normal image entrypoint/startup behavior. Retain private
   access settings and keep bootstrap credentials absent; the existing administrator
   must be preserved. Observe deployment success and the application's native
   health response before testing the wiki.
7. In a new HTTPS session, verify the existing account login, private access,
   synthetic page and attachment, then a small edit on the restored copy. This
   is a separate acceptance gate from offline file/database integrity. Record
   sanitized evidence separately from the secret archive.
8. End the temporary test's compute usage and retain only the recovery material
   that is needed, within the approved budget and authorized cleanup scope.

Do not use `supervisorctl stop uwsgi` as a backup pause: the upstream image's
supervisor listener reacts to stopped/exited processes by terminating supervisor.
Do not use `.git/index.lock` or an invented `flock` as a global wiki lock; page and
attachment writes do not participate in such a lock.

## Validation scope

The exporter is intended for the pinned Otter Wiki 2.26.3 data layout.

### Reproducible local validation

[tests/validate_backup.py](../tests/validate_backup.py) reproduces the original
22 assertions without accessing real wiki data or Railway. It requires Python
3.12+ and Git on PATH, with no additional Python packages or Docker daemon:

```sh
python tests/validate_backup.py --report docs/validation/backup-local.json
```

The [sanitized local report](validation/backup-local.json) records the passing
checks: a synthetic Git repository, SQLite in WAL mode, private output, restoration
into a separate directory, overwrite refusal, and file/database source-change
refusal. An independent exporter code review found no additional blocker for this
scope. This local directory test is not a deployed application restoration.

[tests/validate_restore.py](../tests/validate_restore.py) exercises the separate
offline restore helper with Python 3.11+ and Git:

```sh
python tests/validate_restore.py --report docs/validation/restore-local.json
```

Its [sanitized report](validation/restore-local.json) records **14 passing
assertions**, run locally with Python 3.12.14. They cover the SQLite/Git/page/
attachment roundtrip, owners/modes/timestamps, empty `lost+found`, preservation
of the original Git configuration, and refusal of occupied destinations,
incorrect checksums, path traversal, escaping symlink chains, special files and
corrupt gzip data. No real backup or remote service was used in those tests.

### Observed Railway export

On 2026-11-15 the deployment operator ran the exporter on Railway with Python 3.11.
It reported success with 70 entries, 70,726 uncompressed bytes, a 17,136-byte
archive, and 0.05 seconds elapsed. SQLite integrity was `ok`; source stability
checks passed. The archive was downloaded through the authenticated interface
and retained privately. Its contents and private storage location are not part
of this repository.

### Observed offline Railway restoration

On 2026-11-15 the deployment operator restored the verified archive onto a fresh,
independent Railway volume while the target remained in maintenance. The Console
checks established the empty destination and absence of application processes
and bootstrap credentials. The restore helper ran with Python 3.11 and reported:

```json
{
  "status": "success",
  "archive_sha256_verified": true,
  "entries": 70,
  "uncompressed_bytes": 70726,
  "sqlite_integrity": "ok",
  "git_integrity": "ok",
  "metadata_preserved": true,
  "application_started": false,
  "elapsed_seconds": 0.04
}
```

### Observed normal startup and HTTPS verification

After the temporary Custom Start Command was removed, the normal deployment
reached `SUCCESS` at **2026-11-15T19:02:58.081Z**, using the restored volume and
without bootstrap variables. The [HTTPS recheck report](validation/railway-restore-https.json)
records **24 passing checks in 157.46 seconds**.

A fresh session accepted the original administrator credentials. The original
synthetic page source and attachment bytes matched the saved fixture exactly;
anonymous access remained blocked and registration remained disabled. CSRF and
session-cookie checks passed. A small edit on the restored copy was saved and
verified, and its page and attachment remained private afterward.

Railway read-back confirmed that the original pilot service, active deployment
and volume were unchanged. The [recovery report](validation/railway-recovery.json)
records this isolation alongside the offline restoration and normal restart.

| Acceptance gate | Observed status |
| --- | --- |
| Railway application export and authenticated download | **PASS** |
| Files restored onto a fresh, independent Railway volume | **PASS** |
| Restored SQLite/Git integrity and metadata preservation | **PASS** |
| Normal wiki startup after removing the maintenance override | **PASS** |
| Existing administrator login and application behavior over HTTPS on the restored copy | **PASS** |
| Original pilot service, active deployment and volume preserved | **PASS** |

**The independent restored wiki passed both offline integrity checks and the
HTTPS application recheck.** This controlled exercise does not provide automated
backups or automated disaster recovery. The observed durations are not recovery
time or availability guarantees. Temporary compute cleanup is handled separately
from the recovery acceptance result; publication remains pending.

References: [Python SQLite backup](https://docs.python.org/3.11/library/sqlite3.html#sqlite3.Connection.backup),
[SQLite Online Backup API](https://sqlite.org/backup.html),
[Railway volume files](https://docs.railway.com/cli/volume),
[Railway SSH/SFTP](https://docs.railway.com/cli/ssh).
