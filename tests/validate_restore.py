#!/usr/bin/env python3
"""Local synthetic recovery checks; Python 3.11+ and Git, no external service.

  python tests/validate_restore.py --report docs/validation/restore-local.json

All archives, SQLite databases, Git repositories and destinations are temporary.
No real backup or credentials are read. This verifies the offline restore helper,
not a running Otter Wiki or a Railway deployment.
"""

import argparse
import ast
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]


class CheckFailure(Exception):
    pass


def load_script(name):
    path = ROOT / "ops" / name
    text = path.read_text()
    ast.parse(text, filename=str(path))
    module = {"__name__": "recovery_fixture", "__file__": str(path)}
    exec(compile(text, str(path), "exec"), module)
    return module


def validate(results):
    def check(value, label):
        results.append({"check": label, "passed": bool(value)})
        if not value:
            raise CheckFailure(label)

    def git(*args):
        return subprocess.run(["git", *args], check=True, capture_output=True, timeout=15)

    exporter = load_script("backup_data.py")
    restorer = load_script("restore_data.py")
    restore_data, failure = restorer["restore_data"], restorer["RestoreFailure"]
    with tempfile.TemporaryDirectory(prefix="otterwiki-restore-test-") as temporary:
        work = Path(temporary)
        source, target = work / "source", work / "isolated-target"
        repository = source / "repository"
        repository.mkdir(parents=True)
        (source / "lost+found").mkdir(mode=0o700)
        settings = source / "settings.cfg"
        settings.write_text("SECRET_KEY = 'synthetic-test-value-only'\n")
        settings.chmod(0o640)
        os.utime(settings, (1700000000, 1700000000))
        if os.geteuid() == 0:
            os.chown(settings, 33, 33)
            os.chown(source / "lost+found", 33, 33)
        (source / ".railway-bootstrap.lock").write_bytes(b"")
        (repository / "home.md").write_text("# Offline restore fixture\n")
        (repository / "home").mkdir()
        attachment = b"synthetic binary attachment\x00\xff"
        (repository / "home" / "fixture.bin").write_bytes(attachment)
        git("init", "-q", "-b", "main", str(repository))
        git("-C", str(repository), "add", ".")
        git("-C", str(repository), "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "commit", "-qm", "Synthetic recovery fixture")
        db = sqlite3.connect(source / "db.sqlite")
        try:
            db.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, is_admin INTEGER)")
            db.execute("INSERT INTO user VALUES (1, 1)")
            db.commit()
        finally:
            db.close()
        archive = work / "backup.tar.gz"
        exported = exporter["export_data"](source, archive)
        target.mkdir()
        (target / "lost+found").mkdir(mode=0o700)
        result = restore_data(archive, target, exported["archive_sha256"])
        check(result["status"] == "success" and result["sqlite_integrity"] == "ok"
              and result["git_integrity"] == "ok", "offline SQLite and Git restore succeeds")
        db = sqlite3.connect((target / "db.sqlite").as_uri() + "?mode=ro", uri=True)
        try:
            check(db.execute("SELECT id,is_admin FROM user").fetchall() == [(1, 1)],
                  "restored database retains synthetic account")
        finally:
            db.close()
        check((target / "repository" / "home" / "fixture.bin").read_bytes() == attachment
              and (target / "repository" / "home.md").read_bytes() == (repository / "home.md").read_bytes(),
              "page and attachment bytes retained")
        check(git("-C", str(target / "repository"), "rev-parse", "HEAD").stdout ==
              git("-C", str(repository), "rev-parse", "HEAD").stdout, "Git history retained")
        original, restored = settings.stat(), (target / "settings.cfg").stat()
        check((stat.S_IMODE(restored.st_mode), restored.st_uid, restored.st_gid, restored.st_mtime_ns)
              == (stat.S_IMODE(original.st_mode), original.st_uid, original.st_gid, original.st_mtime_ns),
              "modes owners and timestamps retained")
        check((target / "lost+found").is_dir() and not any((target / "lost+found").iterdir())
              and (target / "lost+found").stat().st_uid == (source / "lost+found").stat().st_uid,
              "existing empty lost+found handled without replacement data")
        check((target / "repository" / ".git" / "config").read_bytes() ==
              (repository / ".git" / "config").read_bytes(), "original Git configuration preserved")
        check(not (target / restorer["MARKER"]).exists() and
              not list(target.glob(restorer["STAGING_PREFIX"] + "*")), "successful restore leaves no staging marker")

        sentinel = work / "occupied"
        sentinel.mkdir()
        (sentinel / "keep.txt").write_bytes(b"existing synthetic data")
        try:
            restore_data(archive, sentinel, exported["archive_sha256"])
        except failure as error:
            check(str(error) == "destination_must_be_empty" and
                  (sentinel / "keep.txt").read_bytes() == b"existing synthetic data",
                  "occupied destination refused and preserved")
        else:
            raise CheckFailure("occupied_destination_was_not_refused")

        def rejected(path, code, label):
            destination = work / (label.replace(" ", "-") + "-target")
            destination.mkdir()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            try:
                restore_data(path, destination, digest)
            except failure as error:
                check(str(error) == code and not any(destination.iterdir()), label)
            except gzip.BadGzipFile:
                check(code == "corrupt_gzip" and not any(destination.iterdir()), label)
            else:
                raise CheckFailure("unsafe_archive_was_not_refused")

        empty = work / "wrong-hash-target"
        empty.mkdir()
        try:
            restore_data(archive, empty, "0" * 64)
        except failure as error:
            check(str(error) == "archive_sha256_mismatch" and not any(empty.iterdir()),
                  "SHA mismatch refused before extraction")
        else:
            raise CheckFailure("incorrect_hash_was_not_refused")

        def variant(name, extras):
            path = work / name
            with tarfile.open(archive, "r:gz") as original_tar, tarfile.open(path, "w:gz") as changed:
                for member in original_tar:
                    reader = original_tar.extractfile(member) if member.isreg() else None
                    try:
                        changed.addfile(member, reader)
                    finally:
                        if reader is not None:
                            reader.close()
                for member, content in extras:
                    changed.addfile(member, io.BytesIO(content) if content is not None else None)
            return path

        traversal = tarfile.TarInfo("app-data/../escaped.txt")
        traversal.size = 1
        rejected(variant("traversal.tar.gz", [(traversal, b"x")]),
                 "archive_contains_unsafe_or_duplicate_path", "path traversal refused without target writes")
        directory = tarfile.TarInfo("app-data/dir")
        directory.type = tarfile.DIRTYPE
        inside = tarfile.TarInfo("app-data/dir/b")
        inside.type, inside.linkname = tarfile.SYMTYPE, ".."
        escaping = tarfile.TarInfo("app-data/a")
        escaping.type, escaping.linkname = tarfile.SYMTYPE, "dir/b/.."
        rejected(variant("link-chain.tar.gz", [(directory, None), (inside, None), (escaping, None)]),
                 "archive_contains_unsafe_symlink", "escaping symlink chain refused and staging removed")
        special = tarfile.TarInfo("app-data/pipe")
        special.type = tarfile.FIFOTYPE
        rejected(variant("special.tar.gz", [(special, None)]),
                 "archive_contains_unsupported_file_type", "special file refused without target writes")
        corrupt = work / "corrupt.tar.gz"
        data = bytearray(archive.read_bytes())
        data[-8] ^= 1  # Corrupt CRC, while passing the digest of these exact bytes.
        corrupt.write_bytes(data)
        rejected(corrupt, "corrupt_gzip", "corrupt gzip refused despite matching supplied digest")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    results, failure = [], None
    try:
        validate(results)
    except CheckFailure as error:
        failure = str(error)
    except Exception:
        failure = "local_restore_validation_failed"
    report = {"status": "failed" if failure else "passed", "python": ".".join(map(str, sys.version_info[:3])),
              "recorded_date": datetime.now(timezone.utc).date().isoformat(),
              "scope": "synthetic offline restore into separate local directories",
              "assertions": len(results), "results": results, "failure": failure,
              "limitations": ["No real backup, credentials or pilot data accessed",
                              "No Docker, Railway or network operation",
                              "No running application login or runtime persistence verification",
                              "Publication is multiple renames; offline independent target is an operator prerequisite"]}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
