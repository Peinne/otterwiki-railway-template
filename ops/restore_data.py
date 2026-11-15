#!/usr/bin/env python3
"""Restore a checked export into an EMPTY, INDEPENDENT, OFFLINE test volume.

PRECONDITION: the operator has verified the destination is not the live volume
and no application process can read or write it until this program succeeds.
This script cannot establish those platform facts and never stops a service.
The destination must already exist, containing only an optional empty lost+found.

Example (Python 3.11+, Git required):
  python3 /tmp/restore_data.py --archive /tmp/recovery.tar.gz \
    --destination /app-data --sha256 EXPECTED_ARCHIVE_SHA256

Only archives made by backup_data.py are supported. The expected SHA-256 must
come from the trusted exporter report. Hash/format/path/integrity checks precede
publication. Numeric owners, modes and timestamps are retained; insufficient
permissions fail the restore. No archive content or account information is logged.

Publication into a mounted directory uses multiple renames and is NOT atomic.
SQLite is published last. If publication fails, a private incomplete marker and
partial target may remain: KEEP THE TARGET OFFLINE and inspect it; never retry over
that occupied target. Validation failures clean only this run's private staging.
There is no automatic removal of existing destination data or restore rollback.
"""

import argparse
import configparser
from decimal import Decimal
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


CHUNK = 1024 * 1024
MAX_ENTRIES = 100000
MARKER = ".otterwiki-restore-incomplete"
STAGING_PREFIX = ".otterwiki-restore-"


class RestoreFailure(Exception):
    """Only fixed safe error labels may be used."""


def check_deadline(deadline):
    if time.monotonic() > deadline:
        raise RestoreFailure("restore_deadline_exceeded")


def empty_destination(destination, allowed=()):
    for path in destination.iterdir():
        if path.name in allowed:
            continue
        if path.name != "lost+found" or path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise RestoreFailure("destination_must_be_empty")


def archive_identity(stream):
    info = os.fstat(stream.fileno())
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def unpack_gzip(stream, tar_path, max_bytes, deadline):
    # Materializing a bounded plain tar verifies the gzip CRC/trailer before any
    # destination extraction and avoids relying on tarfile's early end markers.
    limit = max_bytes + MAX_ENTRIES * 1024 + CHUNK
    total = 0
    with gzip.GzipFile(fileobj=stream, mode="rb") as compressed, open(tar_path, "xb") as output:
        os.chmod(tar_path, 0o600)
        while True:
            check_deadline(deadline)
            block = compressed.read(CHUNK)
            if not block:
                break
            total += len(block)
            if total > limit:
                raise RestoreFailure("archive_exceeded_size_limit")
            output.write(block)


def validate_members(archive, max_bytes, deadline):
    members, total = {}, 0
    for member in archive:
        check_deadline(deadline)
        name = member.name
        if member.isdir() and name.endswith("/"):
            name = name[:-1]
        parts = name.split("/")
        if (not parts or parts[0] != "app-data" or any(part in ("", ".", "..") for part in parts)
                or "\\" in name or "\0" in name or len(name) > 4096 or name in members):
            raise RestoreFailure("archive_contains_unsafe_or_duplicate_path")
        if not (member.isdir() or member.isreg() or member.issym()) or member.issparse():
            raise RestoreFailure("archive_contains_unsupported_file_type")
        if member.uid < 0 or member.gid < 0 or member.mode < 0 or member.mode & ~0o7777:
            raise RestoreFailure("archive_contains_invalid_metadata")
        if len(parts) > 1 and (parts[1] == MARKER or parts[1].startswith(STAGING_PREFIX)):
            raise RestoreFailure("archive_contains_restore_control_files")
        if name.startswith("app-data/lost+found/"):
            raise RestoreFailure("archived_lost_found_must_be_empty")
        if name in {"app-data/db.sqlite-wal", "app-data/db.sqlite-shm", "app-data/db.sqlite-journal"}:
            raise RestoreFailure("archive_contains_live_sqlite_sidecars")
        if member.issym():
            target = member.linkname
            if not target or target.startswith("/") or "\\" in target or "\0" in target:
                raise RestoreFailure("archive_contains_unsafe_symlink")
        if member.isreg():
            total += member.size
            if member.size < 0 or total > max_bytes:
                raise RestoreFailure("archive_exceeded_size_limit")
        members[name] = member
        if len(members) > MAX_ENTRIES:
            raise RestoreFailure("archive_exceeded_entry_limit")
    for name, member in members.items():
        parts = name.split("/")
        for length in range(1, len(parts)):
            parent = members.get("/".join(parts[:length]))
            if parent is None or not parent.isdir():
                raise RestoreFailure("archive_parent_is_not_a_directory")
    required = {"app-data": "directory", "app-data/settings.cfg": "file",
                "app-data/db.sqlite": "file", "app-data/repository": "directory",
                "app-data/repository/.git": "directory",
                "app-data/repository/.git/objects": "directory"}
    for name, kind in required.items():
        member = members.get(name)
        if member is None or (kind == "file" and not member.isreg()) or (kind == "directory" and not member.isdir()):
            raise RestoreFailure("archive_is_not_a_complete_otterwiki_export")
    if "app-data/lost+found" in members and not members["app-data/lost+found"].isdir():
        raise RestoreFailure("archived_lost_found_must_be_empty")
    return members, total


def extract_checked(archive, tree, members, deadline):
    tree.mkdir(mode=0o700)
    for name, member in sorted(members.items()):
        check_deadline(deadline)
        relative = name.removeprefix("app-data").lstrip("/")
        path = tree / relative
        if not relative:
            continue
        if member.isdir():
            path.mkdir(mode=0o700)
        elif member.issym():
            os.symlink(member.linkname, path)
        else:
            reader = archive.extractfile(member)
            if reader is None:
                raise RestoreFailure("archive_file_could_not_be_read")
            copied = 0
            with reader, open(path, "xb") as output:
                os.chmod(path, 0o600)
                while True:
                    check_deadline(deadline)
                    block = reader.read(CHUNK)
                    if not block:
                        break
                    copied += len(block)
                    output.write(block)
            if copied != member.size:
                raise RestoreFailure("archive_contains_truncated_file")
    # Lexical link checks alone miss chains containing '..'. Resolve the complete
    # staged link graph now; extraction never traversed a link as a parent.
    for name, member in members.items():
        if member.issym():
            path = tree / name.removeprefix("app-data/")
            try:
                if not path.resolve(strict=False).is_relative_to(tree):
                    raise RestoreFailure("archive_contains_unsafe_symlink")
            except (OSError, RuntimeError):
                raise RestoreFailure("archive_contains_unsafe_symlink") from None


def verify_data(tree, deadline):
    database = sqlite3.connect((tree / "db.sqlite").as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        database.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RestoreFailure("restored_database_integrity_failed")
    finally:
        database.close()
    check_deadline(deadline)
    repository = tree / "repository"
    git_dir = repository / ".git"
    for relative in ("commondir", "objects/info/alternates", "objects/info/http-alternates", "shallow"):
        if os.path.lexists(git_dir / relative):
            raise RestoreFailure("repository_is_not_self_contained")
    if any((git_dir / "objects").rglob("*.promisor")):
        raise RestoreFailure("repository_is_not_self_contained")
    config_path = git_dir / "config"
    if config_path.is_symlink() or not config_path.is_file():
        raise RestoreFailure("repository_configuration_is_not_regular")
    original_config = config_path.read_bytes()
    config = configparser.RawConfigParser(strict=False, allow_no_value=True)
    config.read_string(original_config.decode("utf-8"))
    object_format = "sha1"
    for section in config.sections():
        if section.lower() == "extensions":
            object_format = config.get(section, "objectformat", fallback="sha1").lower()
    if object_format not in ("sha1", "sha256"):
        raise RestoreFailure("repository_object_format_is_not_supported")
    safe_config = "[core]\nrepositoryformatversion = " + ("1" if object_format == "sha256" else "0") + "\nbare = false\n"
    if object_format == "sha256":
        safe_config += "[extensions]\nobjectformat = sha256\n"
    # Verify the graph without archived include directives, skip lists, external
    # helpers or caller GIT_* variables. Restore the exact config before publishing.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
    try:
        config_path.write_text(safe_config)
        checked = subprocess.run(["git", "-c", "safe.directory=" + str(repository),
                                  "--git-dir=" + str(git_dir), "--work-tree=" + str(repository),
                                  "fsck", "--full", "--no-progress"], env=environment,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=max(0.1, min(30, deadline - time.monotonic())))
        if checked.returncode != 0:
            raise RestoreFailure("restored_git_integrity_failed")
    finally:
        config_path.write_bytes(original_config)


def apply_metadata(path, member):
    os.chown(path, member.uid, member.gid, follow_symlinks=False)
    if not member.issym():
        os.chmod(path, member.mode)
    # Preserve the archive's PAX timestamp precision, without a float round-trip.
    nanoseconds = int(Decimal(member.pax_headers.get("mtime", str(member.mtime))) * 1_000_000_000)
    os.utime(path, ns=(nanoseconds, nanoseconds), follow_symlinks=False)


def restore_data(archive_path, destination, expected_sha256, *, timeout_seconds=120,
                 max_bytes=256 * 1024 * 1024):
    """Caller MUST keep this independent destination offline until success."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) or timeout_seconds <= 0 or max_bytes <= 0:
        raise RestoreFailure("invalid_restore_arguments")
    started = time.monotonic()
    deadline = started + timeout_seconds
    destination = Path(destination).absolute()
    if destination.is_symlink() or not destination.is_dir():
        raise RestoreFailure("destination_must_be_an_existing_real_directory")
    destination = destination.resolve(strict=True)
    empty_destination(destination)
    source = Path(archive_path)
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    stage = None
    publishing = False
    try:
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RestoreFailure("archive_must_be_a_regular_file")
            identity = archive_identity(stream)
            if identity[2] > max_bytes + CHUNK:
                raise RestoreFailure("archive_exceeded_size_limit")
            digest = hashlib.sha256()
            while True:
                check_deadline(deadline)
                block = stream.read(CHUNK)
                if not block:
                    break
                digest.update(block)
            if digest.hexdigest() != expected_sha256.lower():
                raise RestoreFailure("archive_sha256_mismatch")
            stream.seek(0)
            with tempfile.TemporaryDirectory(prefix="otterwiki-restore-input-") as temporary:
                tar_path = Path(temporary) / "verified.tar"
                unpack_gzip(stream, tar_path, max_bytes, deadline)
                if identity != archive_identity(stream):
                    raise RestoreFailure("archive_changed_during_verification")
                with tarfile.open(tar_path, "r:") as archive:
                    members, total = validate_members(archive, max_bytes, deadline)
                    empty_destination(destination)
                    stage = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=destination))
                    tree = stage / "app-data"
                    extract_checked(archive, tree, members, deadline)
                    verify_data(tree, deadline)
                    for name in sorted(members, key=lambda value: (value.count("/"), value), reverse=True):
                        if name != "app-data":
                            apply_metadata(tree / name.removeprefix("app-data/"), members[name])
                    check_deadline(deadline)
                    empty_destination(destination, allowed=(stage.name,))
                    marker = destination / MARKER
                    marker_fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(marker_fd, "w") as marker_file:
                        marker_file.write('{"version":1,"status":"incomplete","keep_offline":true}\n')
                        marker_file.flush()
                        os.fsync(marker_file.fileno())
                    publishing = True
                    # The offline precondition prevents a racing writer. Existing
                    # targets are still checked; no replace/overwrite operation.
                    children = sorted(tree.iterdir(), key=lambda item: (item.name == "db.sqlite", item.name))
                    for child in children:
                        target = destination / child.name
                        if child.name == "lost+found" and target.exists():
                            if any(child.iterdir()) or target.is_symlink() or not target.is_dir() or any(target.iterdir()):
                                raise RestoreFailure("destination_must_be_empty")
                            apply_metadata(target, members["app-data/lost+found"])
                            child.rmdir()
                            continue
                        if os.path.lexists(target):
                            raise RestoreFailure("destination_changed_during_restore")
                        os.rename(child, target)
                    tree.rmdir()
                    stage.rmdir()
                    stage = None
                    apply_metadata(destination, members["app-data"])
                    marker.unlink()
                    # Removing the marker changed the root directory's timestamp.
                    apply_metadata(destination, members["app-data"])
                    publishing = False
                    return {"status": "success", "archive_sha256_verified": True,
                            "entries": len(members), "uncompressed_bytes": total,
                            "sqlite_integrity": "ok", "git_integrity": "ok",
                            "metadata_preserved": True, "application_started": False,
                            "elapsed_seconds": round(time.monotonic() - started, 2)}
    except BaseException:
        if publishing:
            # Do not destroy partially published data or imply rollback succeeded.
            raise RestoreFailure("restore_publication_incomplete_keep_target_offline") from None
        raise
    finally:
        if stage is not None and not publishing:
            shutil.rmtree(stage)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments; use --help.\n")


def main():
    parser = SafeParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--sha256", required=True, help="Expected digest from the trusted exporter report")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--max-bytes", type=int, default=256 * 1024 * 1024)
    args = parser.parse_args()
    try:
        result = restore_data(args.archive, args.destination, args.sha256,
                              timeout_seconds=args.timeout_seconds, max_bytes=args.max_bytes)
    except RestoreFailure as error:
        result = {"status": "failed", "failure": str(error)}
    except KeyboardInterrupt:
        result = {"status": "failed", "failure": "restore_interrupted_keep_target_offline"}
    except Exception:
        result = {"status": "failed", "failure": "restore_archive_io_or_integrity_failure"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
