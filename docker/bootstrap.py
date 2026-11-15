#!/usr/bin/env python3
"""Initialize one private Otter Wiki before its web server can start.

Only the standard library is imported here. User changes go through Otter
Wiki's native Flask CLI. No password is written to arguments, files or logs.
Existing administrators and database preferences are preserved on restarts.
"""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


PENDING_NAME = ".railway-bootstrap.pending.json"
LOCK_NAME = ".railway-bootstrap.lock"
DATA_DIR = Path("/app-data")

# Ask the installed application for its effective configuration, after loading
# database preferences. The probe deliberately returns no secret settings.
CONFIG_PROBE = """
import json
from otterwiki.server import app, db, storage
with app.app_context():
    print(json.dumps({
        'database_driver': db.engine.url.drivername,
        'database_path': db.engine.url.database,
        'repository_path': storage.path,
        **{key: app.config[key] for key in (
            'READ_ACCESS', 'WRITE_ACCESS', 'ATTACHMENT_ACCESS',
            'DISABLE_REGISTRATION', 'AUTO_APPROVAL', 'PASSWORD_MIN_LENGTH'
        )},
    }))
"""


class BootstrapError(RuntimeError):
    """A safe, static explanation of why the server must not start."""


def is_mounted(path: Path) -> bool:
    """Recognize bind mounts too, which os.path.ismount may miss."""
    wanted = str(path.resolve())
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            mountpoint = line.split()[4]
            for escaped, char in (("\\040", " "), ("\\011", "\t"),
                                  ("\\012", "\n"), ("\\134", "\\")):
                mountpoint = mountpoint.replace(escaped, char)
            if mountpoint == wanted:
                return True
    except (OSError, IndexError):
        return False
    return False


def runtime_environment(data_dir: Path, environ: dict, require_mount: bool) -> dict:
    if data_dir.is_symlink() or not data_dir.is_dir():
        raise BootstrapError("The persistent data directory is unavailable.")
    if require_mount and not is_mounted(data_dir):
        raise BootstrapError("Mount a persistent volume at /app-data before starting.")
    for relative in ("settings.cfg", "repository", "db.sqlite"):
        if (data_dir / relative).is_symlink():
            raise BootstrapError("A data path is a symlink; inspect the volume before starting.")
    if not (data_dir / "settings.cfg").is_file() or not (data_dir / "repository/.git").is_dir():
        raise BootstrapError("The upstream entrypoint has not initialized the data volume.")
    expected = {
        "OTTERWIKI_SETTINGS": str(data_dir / "settings.cfg"),
        "OTTERWIKI_REPOSITORY": str(data_dir / "repository"),
        "REPOSITORY": str(data_dir / "repository"),
        "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(data_dir / "db.sqlite"),
    }
    if any(environ.get(key, value) != value for key, value in expected.items()):
        raise BootstrapError("Storage configuration must point to the persistent data volume.")
    child_env = dict(environ)
    child_env.update(expected)
    child_env.pop("BOOTSTRAP_ADMIN_PASSWORD", None)
    child_env.pop("BOOTSTRAP_ADMIN_EMAIL", None)
    return child_env


def run_native(arguments: list, env: dict, *, input_text=None, label="command") -> str:
    try:
        result = subprocess.run(
            [sys.executable, *arguments], env=env, input=input_text,
            text=True, capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BootstrapError("The native " + label + " could not complete.") from None
    if result.returncode != 0:
        # Never forward arbitrary CLI output: a dependency may echo stdin.
        raise BootstrapError("The native " + label + " failed; no server was started.")
    return result.stdout


def native_json(arguments: list, env: dict, label: str):
    try:
        return json.loads(run_native(arguments, env, label=label))
    except (ValueError, TypeError):
        raise BootstrapError("The native " + label + " returned an unexpected result.") from None


def list_users(env: dict) -> list:
    users = native_json(
        ["-m", "flask", "--app", "otterwiki.server", "user", "list", "--json"],
        env, "user listing",
    )
    if not isinstance(users, list) or any(
        not isinstance(user, dict)
        or not isinstance(user.get("email"), str)
        or type(user.get("is_admin")) is not bool
        or type(user.get("has_password")) is not bool
        for user in users
    ):
        raise BootstrapError("The native user listing returned an unexpected result.")
    return users


def read_pending(data_dir: Path):
    path = data_dir / PENDING_NAME
    if path.is_symlink():
        raise BootstrapError("The bootstrap recovery state must be a regular file.")
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError
        if not isinstance(state.get("email"), str) or not state["email"]:
            raise ValueError
        return state
    except (OSError, ValueError):
        raise BootstrapError("The bootstrap recovery state is invalid; inspect the volume.") from None


def sync_directory(data_dir: Path):
    descriptor = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_pending(data_dir: Path, email: str):
    # Atomic, durable state before user creation. It never contains a password.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=data_dir, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "email": email}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, data_dir / PENDING_NAME)
        sync_directory(data_dir)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def clear_pending(data_dir: Path):
    path = data_dir / PENDING_NAME
    if path.is_symlink():
        raise BootstrapError("The bootstrap recovery state must be a regular file.")
    path.unlink(missing_ok=True)
    sync_directory(data_dir)


def initialize(data_dir: Path, environ: dict, child_env: dict) -> str:
    config = native_json(["-c", CONFIG_PROBE], child_env, "configuration check")
    if not isinstance(config, dict):
        raise BootstrapError("The native configuration check returned an unexpected result.")
    if (
        config.get("database_driver") != "sqlite"
        or config.get("database_path") != str(data_dir / "db.sqlite")
        or config.get("repository_path") != str(data_dir / "repository")
    ):
        raise BootstrapError("The application is not using the expected persistent storage.")

    users = list_users(child_env)
    if any(user["is_admin"] and user["has_password"] for user in users):
        clear_pending(data_dir)
        return "Existing administrator preserved. Existing application preferences are unchanged."

    state = read_pending(data_dir)
    if users and (
        state is None or len(users) != 1
        or users[0]["email"] != state["email"]
        or not users[0]["is_admin"] or users[0]["has_password"]
    ):
        raise BootstrapError("Existing users have no usable administrator; recover access with the native CLI.")

    # Database preferences override environment variables, including after a
    # restore. Check the effective values, not only the supplied environment.
    if (
        any(config.get(key) != "APPROVED" for key in
            ("READ_ACCESS", "WRITE_ACCESS", "ATTACHMENT_ACCESS"))
        or config.get("DISABLE_REGISTRATION") is not True
        or config.get("AUTO_APPROVAL") is not False
    ):
        raise BootstrapError("First initialization requires private access and disabled registration; check database preferences too.")

    email = environ.get("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    password = environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not email or not password:
        raise BootstrapError("BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD are required for first initialization.")
    if email.count("@") != 1 or any(char.isspace() for char in email):
        raise BootstrapError("BOOTSTRAP_ADMIN_EMAIL must be a valid email address.")
    try:
        minimum_length = max(16, int(config["PASSWORD_MIN_LENGTH"]))
    except (KeyError, ValueError, TypeError):
        raise BootstrapError("The application password policy is invalid.") from None
    if len(password) < minimum_length or "\n" in password or "\r" in password:
        raise BootstrapError("The bootstrap password must meet the password policy, contain at least 16 characters, and have no line breaks.")
    if state is not None and state["email"] != email:
        raise BootstrapError("Resume initialization with the original bootstrap email; the recovery state is preserved.")
    if state is None:
        write_pending(data_dir, email)

    cli = ["-m", "flask", "--app", "otterwiki.server", "user"]
    if not users:
        run_native(
            [*cli, "create", email, "Administrator",
             "--flags=email_confirmed,approved", "--permissions=admin"],
            child_env, label="administrator creation",
        )
    run_native(
        [*cli, "password", email, "--interactive"], child_env,
        input_text=password + "\n" + password + "\n", label="password assignment",
    )
    if not any(
        user["email"] == email and user["is_admin"] and user["has_password"]
        for user in list_users(child_env)
    ):
        raise BootstrapError("Administrator verification failed; the recovery state is preserved.")
    clear_pending(data_dir)
    return "Private wiki initialized. Administrator credentials were not logged."


def bootstrap(data_dir: Path = DATA_DIR, *, require_mount=True, environ=None) -> str:
    """The optional mount flag exists for isolated tests, not an environment override."""
    data_dir = Path(data_dir).absolute()
    environ = dict(os.environ if environ is None else environ)
    child_env = runtime_environment(data_dir, environ, require_mount)
    descriptor = os.open(data_dir / LOCK_NAME,
                         os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BootstrapError("Another bootstrap process is using this data volume.") from None
        return initialize(data_dir, environ, child_env)


if __name__ == "__main__":
    try:
        print(bootstrap(), flush=True)
    except (BootstrapError, OSError) as error:
        # OSError text may contain paths supplied by the environment; keep it
        # out of the logs, as well as captured subprocess stdout/stderr.
        message = str(error) if isinstance(error, BootstrapError) else "The persistent volume could not be read or written."
        print("Bootstrap stopped: " + message, file=sys.stderr, flush=True)
        sys.exit(1)
