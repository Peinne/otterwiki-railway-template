#!/usr/bin/env python3
"""Reproduce the 22 local backup-export assertions with synthetic data only.

Requires Python 3.12+ and Git on PATH; no installed Otter Wiki or Docker is needed.
Python 3.12 is used for safe tar extraction in the test. The exporter itself also
runs on Python 3.11. Temporary fixtures are removed after the run. No Railway,
network request, real credentials or existing wiki data are accessed.

  python tests/validate_backup.py --report docs/validation/backup-local.json
"""

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
EXPORTER = PROJECT / "ops" / "backup_data.py"


class CheckFailure(Exception):
    pass


def validate(results):
    def check(condition, label):
        results.append({"check": label, "passed": bool(condition)})
        if not condition:
            raise CheckFailure(label)

    def git(*args):
        return subprocess.run(["git", *args], check=True, capture_output=True, timeout=15)

    code = EXPORTER.read_text()
    ast.parse(code, filename=str(EXPORTER))
    # Use the same dictionary as the functions' globals so the two deliberate
    # copy-time mutations below patch the exporter, without changing its file.
    module = {"__name__": "backup_fixture", "__file__": str(EXPORTER)}
    exec(compile(code, str(EXPORTER), "exec"), module)

    with tempfile.TemporaryDirectory(prefix="otterwiki-backup-test-") as directory:
        work = Path(directory)
        source = work / "source"
        repository = source / "repository"
        repository.mkdir(parents=True)
        git("init", "-q", "-b", "main", str(repository))
        (source / "settings.cfg").write_text("SECRET_KEY = 'synthetic-not-a-real-secret'\n")
        (source / "settings.cfg").chmod(0o640)
        (source / ".railway-bootstrap.lock").write_bytes(b"")
        (source / ".railway-bootstrap.pending.json").write_text(
            '{"version":1,"email":"test@example.invalid"}\n')
        (repository / "page.md").write_text("# Synthetic fixture\n\nPersistence probe\n")
        (repository / "page").mkdir()
        attachment = b"synthetic attachment\x00\x01\xff"
        (repository / "page" / "fixture.bin").write_bytes(attachment)
        (repository / "link.md").symlink_to("page.md")
        git("-C", str(repository), "add", ".")
        git("-C", str(repository), "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "commit", "-qm", "Synthetic fixture")

        writer = sqlite3.connect(source / "db.sqlite")
        try:
            if writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise CheckFailure("synthetic_wal_setup_failed")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, email TEXT, "
                           "password_hash TEXT, is_admin INTEGER)")
            writer.execute("INSERT INTO user VALUES (1, ?, ?, 1)",
                           ("test@example.invalid", "synthetic-hash"))
            writer.commit()
            check((source / "db.sqlite-wal").stat().st_size > 0, "fixture has live WAL")
            output = work / "capture.tar.gz"
            result = module["export_data"](source, output)
            check(result["status"] == "success", "WAL export succeeds")
            check(stat.S_IMODE(output.stat().st_mode) == 0o600, "archive is 0600")
            check(hashlib.sha256(output.read_bytes()).hexdigest() == result["archive_sha256"],
                  "archive checksum matches")
            restore = work / "isolated"
            restore.mkdir()
            with tarfile.open(output) as archive:
                names = archive.getnames()
                check("app-data/settings.cfg" in names and
                      "app-data/.railway-bootstrap.pending.json" in names,
                      "settings and wrapper marker retained")
                check("app-data/repository/.git/HEAD" in names, "Git history retained")
                check(all("app-data/" + sidecar not in names for sidecar in
                          ("db.sqlite-wal", "db.sqlite-shm", "db.sqlite-journal")),
                      "live SQLite sidecars excluded")
                check(archive.getmember("app-data/settings.cfg").mode == 0o640,
                      "source permissions retained")
                check(archive.getmember("app-data/settings.cfg").uid ==
                      (source / "settings.cfg").stat().st_uid, "numeric owner retained")
                archive.extractall(restore, filter="data")
            restored = restore / "app-data"
            database = sqlite3.connect((restored / "db.sqlite").as_uri() + "?mode=ro", uri=True)
            try:
                check(database.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
                      "restored SQLite integrity")
                check(database.execute("SELECT email,password_hash,is_admin FROM user").fetchall()
                      == [("test@example.invalid", "synthetic-hash", 1)], "WAL account data retained")
            finally:
                database.close()
            check((restored / "repository" / "page" / "fixture.bin").read_bytes() == attachment,
                  "attachment bytes retained")
            check((restored / "repository" / "link.md").is_symlink(),
                  "relative internal symlink retained")
            fsck = subprocess.run(["git", "-C", str(restored / "repository"), "fsck", "--full"],
                                  capture_output=True, timeout=15)
            check(fsck.returncode == 0, "restored Git integrity")

            digest_before = hashlib.sha256(output.read_bytes()).hexdigest()
            try:
                module["export_data"](source, output)
            except module["BackupFailure"] as error:
                check(str(error) == "destination_already_exists", "existing destination refused")
            else:
                raise CheckFailure("existing_output_was_not_refused")
            check(hashlib.sha256(output.read_bytes()).hexdigest() == digest_before,
                  "existing archive unchanged")
            try:
                module["export_data"](source, source / "inside.tar.gz")
            except module["BackupFailure"] as error:
                check(str(error) == "destination_must_be_outside_source",
                      "destination inside source refused")
            else:
                raise CheckFailure("destination_inside_source_was_not_refused")

            original_copy = module["copy_tree"]

            def changed_file(src, dst, entries, budget):
                original_copy(src, dst, entries, budget)
                (src / "repository" / "page.md").write_text("# Changed while exporting\n")

            with patch.dict(module, {"copy_tree": changed_file}):
                try:
                    module["export_data"](source, work / "changed-file.tar.gz")
                except module["BackupFailure"] as error:
                    check(str(error) == "source_changed_during_export",
                          "concurrent file change refused")
                else:
                    raise CheckFailure("concurrent_file_change_was_not_refused")
            check(not (work / "changed-file.tar.gz").exists(),
                  "file-change failure publishes no archive")

            def changed_database(src, dst, entries, budget):
                original_copy(src, dst, entries, budget)
                writer.execute("INSERT INTO user VALUES (2, ?, ?, 0)",
                               ("another@example.invalid", "synthetic-other"))
                writer.commit()

            with patch.dict(module, {"copy_tree": changed_database}):
                try:
                    module["export_data"](source, work / "changed-db.tar.gz")
                except module["BackupFailure"] as error:
                    check(str(error) == "source_changed_during_export",
                          "concurrent WAL commit refused")
                else:
                    raise CheckFailure("concurrent_database_change_was_not_refused")
            check(not (work / "changed-db.tar.gz").exists(),
                  "database-change failure publishes no archive")
        finally:
            writer.close()
        check(not list(work.glob(".otterwiki-export-*")), "private temporary data cleaned up")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", type=Path, help="Optional path for the sanitized JSON report")
    args = parser.parse_args()
    results = []
    failure = None
    try:
        validate(results)
    except CheckFailure as error:
        failure = str(error)
    except Exception:
        failure = "local_backup_validation_failed"
    report = {"status": "failed" if failure else "passed",
              "python": ".".join(map(str, sys.version_info[:3])),
              "scope": "synthetic local SQLite WAL, Git, export and isolated directory restore",
              "recorded_date": datetime.now(timezone.utc).date().isoformat(),
              "assertions": len(results), "results": results, "failure": failure,
              "limitations": ["No real wiki data or credentials accessed",
                              "No Docker, Railway or external network call",
                              "Synthetic directory restoration does not establish deployed application restoration",
                              "Concurrent file and database commits are injected during copy; no atomic snapshot claim"]}
    if args.report:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n")
        except OSError:
            report["status"], report["failure"] = "failed", "sanitized_report_write_failed"
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
