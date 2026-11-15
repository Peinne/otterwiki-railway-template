#!/usr/bin/env python3
"""Export an existing Otter Wiki data directory without stopping the application.

SQLite uses its Online Backup API through a read-only source connection. Other
files are copied with their modes, timestamps and symlink targets preserved;
numeric owners/groups are retained in the archive. Source and copied trees are
checked before publication. This is a checked online copy, NOT an atomic volume
snapshot or an exclusion of concurrent writers. Use a quiet window, without wiki
edits, uploads, administration, Git pushes/pulls or deployments.

The destination must be new and outside the source. The resulting tar.gz contains
app-data/ and is private (0600). It includes account password hashes and SECRET_KEY;
never put it in Git, logs, a public URL or a public artifact. Restore only into a
separate, empty test volume. This program does not restore or delete source data.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time


DATABASE = "db.sqlite"
DATABASE_FILES = {DATABASE, DATABASE + "-wal", DATABASE + "-shm", DATABASE + "-journal"}
CHUNK = 1024 * 1024
MAX_ENTRIES = 100000


class BackupFailure(Exception):
    """Messages are fixed safe codes, never file contents or exception details."""


class Budget:
    def __init__(self, seconds, max_bytes):
        self.deadline = time.monotonic() + seconds
        self.max_bytes = max_bytes

    def check(self):
        if time.monotonic() > self.deadline:
            raise BackupFailure("export_deadline_exceeded")


def identity(info):
    return {"dev": info.st_dev, "ino": info.st_ino, "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid, "gid": info.st_gid, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def digest_file(path, budget, expected=None, destination=None):
    budget.check()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    output = None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            before_stat = os.fstat(stream.fileno())
            before = identity(before_stat)
            if not stat.S_ISREG(before_stat.st_mode):
                raise BackupFailure("source_contains_unsupported_file_type")
            if expected is not None and before != {key: expected[key] for key in before}:
                raise BackupFailure("source_changed_during_export")
            if before["size"] > budget.max_bytes:
                raise BackupFailure("export_exceeded_size_limit")
            if destination is not None:
                output = open(destination, "xb")
            digest = hashlib.sha256()
            while True:
                budget.check()
                block = stream.read(CHUNK)
                if not block:
                    break
                digest.update(block)
                if output is not None:
                    output.write(block)
            if before != identity(os.fstat(stream.fileno())):
                raise BackupFailure("source_changed_during_export")
            return before, digest.hexdigest()
    finally:
        if output is not None:
            output.close()


def inventory(root, budget):
    entries = {}
    total = 0

    def visit(relative):
        nonlocal total
        budget.check()
        path = root / relative
        info = path.lstat()
        item = identity(info)
        if len(entries) >= MAX_ENTRIES:
            raise BackupFailure("export_exceeded_file_count_limit")
        if stat.S_ISDIR(info.st_mode):
            item["kind"] = "directory"
            entries[relative] = item
            with os.scandir(path) as directory:
                names = sorted(entry.name for entry in directory)
            for name in names:
                if not relative and name in DATABASE_FILES:
                    continue
                visit(relative + "/" + name if relative else name)
        elif stat.S_ISREG(info.st_mode):
            item, digest = digest_file(path, budget, item)
            item.update(kind="file", sha256=digest)
            total += item["size"]
            if total > budget.max_bytes:
                raise BackupFailure("export_exceeded_size_limit")
            entries[relative] = item
        elif stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            # Relative internal links remain meaningful in the isolated restore.
            if os.path.isabs(target) or not (path.parent / target).resolve().is_relative_to(root):
                raise BackupFailure("source_contains_nonportable_or_external_symlink")
            item.update(kind="symlink", target=target)
            entries[relative] = item
        else:
            raise BackupFailure("source_contains_unsupported_file_type")

    visit("")
    return entries, total


def comparable(entries):
    """Copy inodes, ctimes and owners differ; archive owners use source metadata."""
    keys = ("kind", "mode", "mtime_ns", "sha256", "target")
    return {name: {key: item[key] for key in keys if key in item}
            for name, item in entries.items()}


def copy_tree(source, target, entries, budget):
    target.mkdir(mode=0o700)
    for relative, item in sorted(entries.items()):
        budget.check()
        if not relative:
            continue
        destination = target / relative
        if item["kind"] == "directory":
            destination.mkdir(mode=0o700)
        elif item["kind"] == "symlink":
            os.symlink(item["target"], destination)
        else:
            _, digest = digest_file(source / relative, budget, item, destination)
            if digest != item["sha256"]:
                raise BackupFailure("source_changed_during_export")
    # Restore directory times last, after their children have been created.
    for relative in sorted(entries, key=lambda name: (name.count("/"), name), reverse=True):
        item = entries[relative]
        destination = target / relative
        if item["kind"] != "symlink":
            os.chmod(destination, item["mode"])
        os.utime(destination, ns=(item["mtime_ns"], item["mtime_ns"]), follow_symlinks=False)


def database_version(connection):
    return connection.execute("PRAGMA data_version").fetchone()[0]


def backup_database(connection, destination, budget):
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]

    def progress(status, remaining, total):
        budget.check()  # Also called for SQLITE_BUSY/LOCKED; avoid endless retries.
        if total * page_size > budget.max_bytes:
            raise BackupFailure("export_exceeded_size_limit")

    with sqlite3.connect(destination) as target:
        connection.backup(target, pages=256, progress=progress, sleep=0.05)
        budget.check()
        target.set_progress_handler(lambda: int(time.monotonic() > budget.deadline), 1000)
        if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupFailure("database_integrity_check_failed")
        # The destination must be self-contained, with no WAL needed on restore.
        if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise BackupFailure("backup_database_is_not_self_contained")
    target.close()


class BoundedReader:
    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget

    def read(self, size=-1):
        self.budget.check()
        return self.stream.read(size)


def make_archive(tree, destination, entries, database_info, budget):
    metadata = dict(entries)
    metadata[DATABASE] = dict(database_info, kind="file", size=(tree / DATABASE).stat().st_size)
    # mkstemp created the archive at 0600 inside a 0700 temporary directory.
    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for relative, item in sorted(metadata.items()):
            budget.check()
            info = tarfile.TarInfo("app-data" + ("/" + relative if relative else ""))
            info.mode, info.uid, info.gid = item["mode"], item["uid"], item["gid"]
            info.mtime = item["mtime_ns"] / 1_000_000_000
            if item["kind"] == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif item["kind"] == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, item["target"]
                archive.addfile(info)
            else:
                info.size = item["size"]
                with open(tree / relative, "rb") as stream:
                    archive.addfile(info, BoundedReader(stream, budget))
    with open(destination, "rb") as stream:
        os.fsync(stream.fileno())


def export_data(source, output, *, timeout_seconds=120, max_bytes=256 * 1024 * 1024):
    if timeout_seconds <= 0 or max_bytes <= 0:
        raise BackupFailure("invalid_export_limits")
    started = time.monotonic()
    budget = Budget(timeout_seconds, max_bytes)
    source = Path(source).resolve(strict=True)
    proposed = Path(output).absolute()
    output = proposed.parent.resolve(strict=True) / proposed.name
    if not source.is_dir() or output.is_relative_to(source):
        raise BackupFailure("destination_must_be_outside_source")
    if os.path.lexists(output):
        raise BackupFailure("destination_already_exists")
    database = source / DATABASE
    database_stat = database.lstat()
    if not stat.S_ISREG(database_stat.st_mode):
        raise BackupFailure("database_must_be_a_regular_file")
    if not (source / "settings.cfg").is_file() or not (source / "repository" / ".git").is_dir():
        raise BackupFailure("source_is_not_an_initialized_otterwiki_directory")
    database_info = identity(database_stat)
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.execute("PRAGMA query_only=ON")
        version = database_version(connection)
        before, byte_count = inventory(source, budget)
        with tempfile.TemporaryDirectory(prefix=".otterwiki-export-", dir=output.parent,
                                         ignore_cleanup_errors=True) as temporary:
            private = Path(temporary)
            tree = private / "app-data"
            copy_tree(source, tree, before, budget)
            copied, _ = inventory(tree, budget)
            if comparable(copied) != comparable(before):
                raise BackupFailure("copied_files_did_not_match_source_inventory")
            backup_database(connection, tree / DATABASE, budget)
            database_size = (tree / DATABASE).stat().st_size
            if byte_count + database_size > max_bytes:
                raise BackupFailure("export_exceeded_size_limit")
            after, _ = inventory(source, budget)
            if before != after or database_info != identity(database.lstat()) or version != database_version(connection):
                raise BackupFailure("source_changed_during_export")
            descriptor, archive_name = tempfile.mkstemp(prefix="archive-", suffix=".tar.gz", dir=private)
            os.close(descriptor)
            archive = Path(archive_name)
            make_archive(tree, archive, before, database_info, budget)
            _, archive_hash = digest_file(archive, budget)
            # Detect commits during compression as well; never publish a stale
            # success if the source moved during this invocation.
            final, _ = inventory(source, budget)
            if before != final or database_info != identity(database.lstat()) or version != database_version(connection):
                raise BackupFailure("source_changed_during_export")
            budget.check()
            result = {"status": "success", "consistency": "checked_online_copy_not_atomic_snapshot",
                      "database_integrity": "ok", "source_stability_checks": "passed",
                      "entries": len(before) + 1, "uncompressed_bytes": byte_count + database_size,
                      "archive_bytes": archive.stat().st_size, "archive_sha256": archive_hash,
                      "archive_mode": "0600", "elapsed_seconds": round(time.monotonic() - started, 2)}
            # Atomic no-overwrite publication on the same filesystem. A racing
            # existing destination is preserved; only our private temp is cleaned.
            os.link(archive, output)
            return result
    finally:
        connection.close()


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments; use --help.\n")


def main():
    parser = SafeParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="/app-data")
    parser.add_argument("--output", required=True, help="New private .tar.gz path outside the source, normally /tmp/...")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--max-bytes", type=int, default=256 * 1024 * 1024)
    args = parser.parse_args()
    try:
        result = export_data(args.source, args.output, timeout_seconds=args.timeout_seconds,
                             max_bytes=args.max_bytes)
    except BackupFailure as error:
        result = {"status": "failed", "failure": str(error)}
    except KeyboardInterrupt:
        result = {"status": "failed", "failure": "export_interrupted"}
    except Exception:
        result = {"status": "failed", "failure": "export_io_or_database_failure"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
