#!/usr/bin/env bash
# Run only against the local, disposable image built from this repository.
# Requires Docker and Python 3 on the host. No repository secrets are needed.
set +x
set -euo pipefail
python3 - "$@" <<'PY_CONTAINER_SMOKE'
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

parser = argparse.ArgumentParser(description="Exercise the real Otter Wiki container over loopback HTTP.")
parser.add_argument("--image", default="otterwiki-railway:smoke")
parser.add_argument("--upgrade-from-image", help="Optional prebuilt 2.26.3 wrapper for a same-volume upgrade check")
parser.add_argument("--report", type=Path, default=Path("test-results/container-smoke.json"))
args = parser.parse_args()
report_path = args.report.resolve()
os.umask(0o077)
base = "http://127.0.0.1:8080"
marker = "container-smoke-72fe32"
page_content = "# Container smoke\n\n" + marker + "\n"
attachment_name = "fixture (draft).txt"
attachment_path = "/ContainerSmoke/a/" + urllib.parse.quote(attachment_name, safe="")
attachment = b"Disposable container attachment 3295ad.\n"
target_version = "2.27.0"
previous_version = "2.26.3"
upgrade = {"requested": bool(args.upgrade_from_image), "status": "not_run",
           "from_version": previous_version, "to_version": target_version}
checks = []
containers = []
volumes = []
stage = "prerequisites"
credential_values = []
started = time.monotonic()
last_command_failure = None
diagnostics = []


class SmokeFailure(Exception):
    pass


def require(condition, label):
    if not condition:
        raise SmokeFailure(label)
    checks.append(label)
    print("PASS: " + label, flush=True)


def classify_output(text):
    """Return fixed diagnostic labels, never a substring of process output."""
    signatures = (
        ("port is already allocated", "host port already allocated"),
        ("address already in use", "address already in use"),
        ("permission denied", "permission denied"),
        ("operation not permitted", "operation not permitted"),
        ("no such file or directory", "required file or executable missing"),
        ("exec format error", "executable format or architecture mismatch"),
        ("invalid mount config", "Docker rejected the mount configuration"),
        ("oci runtime create failed", "OCI runtime could not create the process"),
        ("modulenotfounderror:", "Python module import failed"),
        ("importerror:", "Python import failed"),
        ("syntaxerror:", "Python syntax error"),
        ("operationalerror:", "database operational error"),
        ("gitcommanderror:", "Git operation failed"),
        ("dubious ownership", "Git rejected repository ownership"),
        ("nginx: [emerg]", "nginx configuration or startup failed"),
        ("nginx: configuration file /etc/nginx/nginx.conf test is successful", "nginx configuration check passed"),
        ("spawned: 'uwsgi'", "supervisord started uWSGI"),
        ("spawned: 'nginx'", "supervisord started nginx"),
        ("exited: uwsgi", "uWSGI exited"),
        ("exited: nginx", "nginx exited"),
    )
    lowered = text.lower()
    return [label for signature, label in signatures if signature in lowered]


def command(arguments, *, timeout=60, check=True, input_text=None, operation="container command"):
    global last_command_failure
    try:
        result = subprocess.run(arguments, input=input_text, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        last_command_failure = {"operation": operation, "timed_out": True}
        raise SmokeFailure(operation + " exceeded its deadline") from None
    # Never forward arbitrary process output, command arguments or HTTP bodies.
    if any(value in result.stdout or value in result.stderr for value in credential_values):
        raise SmokeFailure("a generated password appeared in command output")
    if check and result.returncode:
        last_command_failure = {"operation": operation, "exit_code": result.returncode,
                                "signals": classify_output(result.stdout + "\n" + result.stderr)}
        raise SmokeFailure(operation + " failed")
    return result


def docker(*arguments, **kwargs):
    operation = {"info": "Docker daemon query", "image": "Docker image inspection",
                 "volume": "Docker volume operation", "run": "container creation",
                 "exec": "container probe", "inspect": "container state query",
                 "logs": "container log read", "stop": "container stop",
                 "rm": "container removal"}.get(arguments[0], "Docker command")
    return command(["docker", *arguments], operation=operation, **kwargs)


BOOTSTRAP_MESSAGES = {
    "The persistent data directory is unavailable.",
    "Mount a persistent volume at /app-data before starting.",
    "A data path is a symlink; inspect the volume before starting.",
    "The upstream entrypoint has not initialized the data volume.",
    "Storage configuration must point to the persistent data volume.",
    "The application is not using the expected persistent storage.",
    "The bootstrap recovery state must be a regular file.",
    "The bootstrap recovery state is invalid; inspect the volume.",
    "Existing users have no usable administrator; recover access with the native CLI.",
    "First initialization requires private access and disabled registration; check database preferences too.",
    "BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD are required for first initialization.",
    "BOOTSTRAP_ADMIN_EMAIL must be a valid email address.",
    "The application password policy is invalid.",
    "The bootstrap password must meet the password policy, contain at least 16 characters, and have no line breaks.",
    "Resume initialization with the original bootstrap email; the recovery state is preserved.",
    "Administrator verification failed; the recovery state is preserved.",
    "Another bootstrap process is using this data volume.",
    "The persistent volume could not be read or written.",
}
for native_step in ("configuration check", "user listing", "administrator creation", "password assignment"):
    for ending in (" could not complete.", " failed; no server was started.", " returned an unexpected result."):
        BOOTSTRAP_MESSAGES.add("The native " + native_step + ending)
SAFE_LOG_LINES = {"Bootstrap stopped: " + message for message in BOOTSTRAP_MESSAGES} | {
    "Private wiki initialized. Administrator credentials were not logged.",
    "Existing administrator preserved. Existing application preferences are unchanged.",
}


def container_diagnostics(name):
    """Inspect only typed state fields and exact approved log messages."""
    diagnostic = {}
    try:
        result = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", name],
                                text=True, capture_output=True, timeout=10)
        state = json.loads(result.stdout) if result.returncode == 0 else {}
        allowed_states = {"created", "restarting", "running", "removing", "paused", "exited", "dead"}
        diagnostic["state"] = state.get("Status") if state.get("Status") in allowed_states else "unavailable"
        if type(state.get("ExitCode")) is int:
            diagnostic["exit_code"] = state["ExitCode"]
        if type(state.get("OOMKilled")) is bool:
            diagnostic["oom_killed"] = state["OOMKilled"]
        diagnostic["runtime_error_signals"] = classify_output(str(state.get("Error", "")))
    except Exception:
        diagnostic["state"] = "inspection unavailable"
    try:
        result = subprocess.run(["docker", "logs", "--tail", "160", name],
                                text=True, capture_output=True, timeout=10)
        text = result.stdout + "\n" + result.stderr
        if any(value in text for value in credential_values):
            diagnostic["logs"] = "suppressed because a generated credential was detected"
        else:
            diagnostic["approved_log_messages"] = sorted({line.strip() for line in text.splitlines()
                                                         if line.strip() in SAFE_LOG_LINES})
            diagnostic["log_signals"] = classify_output(text)
            diagnostic["unrecognized_log_lines_omitted"] = sum(
                bool(line.strip()) and line.strip() not in SAFE_LOG_LINES for line in text.splitlines())
    except Exception:
        diagnostic["logs"] = "inspection unavailable"
    return diagnostic


def scan_logs(name):
    result = docker("logs", name, check=False)
    # command() checks the captured logs without ever printing them.
    return result.returncode == 0


def stop_remove(name):
    docker("stop", "--time", "20", name, timeout=30)
    scan_logs(name)
    docker("rm", "--volumes", name)
    containers.remove(name)


def launch(name, volume, env_file=None, *, image=None):
    containers.append(name)
    arguments = ["run", "--detach", "--name", name,
                 "--publish", "127.0.0.1:8080:8080", "--env", "PORT=8080",
                 "--mount", "type=volume,source=" + volume + ",target=/app-data"]
    if env_file is not None:
        # Only the path is an argument; generated credentials enter as env vars.
        arguments += ["--env-file", str(env_file)]
    arguments.append(image or args.image)
    docker(*arguments)


def disposable_run(name, *arguments, input_text=None):
    # Track helpers too, so a timeout cannot leave an attached volume behind.
    containers.append(name)
    docker("run", "--rm", "--name", name, *arguments, input_text=input_text)
    containers.remove(name)


def wait_health(name, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = docker("inspect", "--format", "{{.State.Status}}", name).stdout.strip()
        if state in ("exited", "dead"):
            raise SmokeFailure("container exited before health became ready")
        try:
            with urllib.request.urlopen(base + "/-/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(1)
    raise SmokeFailure("bounded health deadline exceeded")


class Client:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, data=None, content_type=None):
        headers = {"Referer": base + "/-/login"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(base + path, data=data, headers=headers)
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, response.read(), response.geturl()

    def token(self):
        _, body, _ = self.request("/-/login")
        match = re.search(rb'name="csrf-token" content="([^"]+)"', body)
        if not match:
            raise SmokeFailure("actual HTTP form did not provide a CSRF token")
        return match.group(1).decode()

    def post(self, path, fields, *, csrf=True):
        fields = dict(fields)
        if csrf:
            fields["csrf_token"] = self.token()
        return self.request(path, urllib.parse.urlencode(fields).encode(),
                            "application/x-www-form-urlencoded")

    def login(self, email, password):
        _, body, _ = self.post("/-/login", {"email": email, "password": password})
        return b"You logged in successfully" in body


def verify_http(credentials, phase, *, initialize=False):
    anonymous = Client()
    status, body, url = anonymous.request("/Home")
    require(status == 200 and urllib.parse.urlsplit(url).path == "/-/login",
            phase + ": anonymous page request reaches login")
    if initialize or phase == "upgrade":
        status, _, _ = anonymous.post("/-/register", {
            "name": "Disposable Signup", "email": "signup@example.invalid",
            "password1": "unused-registration-test", "password2": "unused-registration-test"})
        require(status == 400, phase + ": registration refused with a valid CSRF token")
        status, _, _ = Client().post("/-/login", {
            "email": credentials["email"], "password": credentials["initial"]}, csrf=False)
        require(status == 400, phase + ": login without CSRF token refused")

    admin = Client()
    password = credentials["initial"] if initialize else credentials["changed"]
    require(admin.login(credentials["email"], password), phase + ": administrator HTTP login works")
    status, body, _ = admin.request("/-/admin/user_management")
    require(status == 200 and credentials["email"].encode() in body,
            phase + ": administrator management works")

    if initialize:
        status, body, _ = admin.post("/ContainerSmoke/save", {
            "content": page_content,
            "commit": "Create disposable container test page"})
        require(status == 200 and marker.encode() in body, phase + ": page saved through real HTTP")
        boundary = "otterwiki-smoke-" + secrets.token_hex(12)
        fields = {"csrf_token": admin.token(), "message": "Disposable container attachment"}
        parts = []
        for key, value in fields.items():
            parts.append(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + key +
                          "\"\r\n\r\n" + value + "\r\n").encode())
        parts.append(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; "
                      "filename=\"" + attachment_name + "\"\r\nContent-Type: text/plain\r\n\r\n").encode()
                     + attachment + b"\r\n")
        parts.append(("--" + boundary + "--\r\n").encode())
        status, _, _ = admin.request("/ContainerSmoke/attachments", b"".join(parts),
                                    "multipart/form-data; boundary=" + boundary)
        require(status == 200, phase + ": attachment with spaces and parentheses uploaded through HTTP")
    if initialize or phase == "upgrade":
        anonymous.post("/ContainerSmoke/save", {
            "content": "UNAUTHORIZED REPLACEMENT", "commit": "Unauthorized test request"})

    status, body, _ = admin.request("/ContainerSmoke")
    require(status == 200 and marker.encode() in body and b"UNAUTHORIZED REPLACEMENT" not in body,
            phase + ": saved page retained and unauthorized write excluded")
    status, body, _ = admin.request("/ContainerSmoke/source?raw")
    source = re.sub(rb"\n\[\]: # \([^\r\n]*\)\Z", b"", body)
    require(status == 200 and source == page_content.encode(), phase + ": exact Markdown source retained")
    status, body, _ = admin.request(attachment_path)
    require(status == 200 and body == attachment,
            phase + ": attachment bytes retained through a percent-encoded HTTP path")
    status, body, url = anonymous.request("/ContainerSmoke")
    require(status == 200 and urllib.parse.urlsplit(url).path == "/-/login" and marker.encode() not in body,
            phase + ": page content remains private behind login")
    status, body, url = anonymous.request(attachment_path)
    require(status == 200 and urllib.parse.urlsplit(url).path == "/-/login" and body != attachment,
            phase + ": attachment remains private behind login")

    if initialize:
        _, body, _ = admin.post("/-/settings", {
            "password1": credentials["changed"], "password2": credentials["changed"]})
        require(b"Your password was updated successfully" in body,
                phase + ": administrator changes password through HTTP")
    require(Client().login(credentials["email"], credentials["changed"]),
            phase + ": changed password works in a new HTTP session")
    require(not Client().login(credentials["email"], credentials["initial"]),
            phase + ": original bootstrap password no longer works")
    if phase == "restore":
        require(not Client().login(credentials["replacement_email"], credentials["replacement"]),
                "restore: changed bootstrap variables do not create an account")
    return admin


def git_head(name):
    value = docker("exec", "--user", "www-data", name, "git", "-C",
                   "/app-data/repository", "rev-parse", "HEAD").stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise SmokeFailure("Git head probe returned an unexpected result")
    return value


RUNTIME_CHECK = r'''
import os
from pathlib import Path
import pwd
import sqlite3
import sys
from otterwiki import __version__
assert __version__ == sys.argv[1]
assert sys.version_info[:2] == (3, 11)
uid = pwd.getpwnam("www-data").pw_uid
for name in ("settings.cfg", "db.sqlite", "repository", "repository/.git"):
    assert (Path("/app-data") / name).stat().st_uid == uid
assert b"supervisord" in Path("/proc/1/cmdline").read_bytes()
environment = Path("/proc/1/environ").read_bytes().split(b"\0")
assert not any(item.startswith((b"BOOTSTRAP_ADMIN_EMAIL=", b"BOOTSTRAP_ADMIN_PASSWORD="))
               for item in environment)
workers = []
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        fields = dict(line.split(":", 1) for line in (entry / "status").read_text().splitlines()
                      if ":" in line)
    except (FileNotFoundError, ProcessLookupError):
        continue
    if fields.get("Name", "").strip().startswith("uwsgi"):
        workers.append(int(fields["Uid"].split()[1]))
assert workers and all(worker_uid == uid for worker_uid in workers)
with sqlite3.connect("file:/app-data/db.sqlite?mode=ro", uri=True) as database:
    rows = database.execute("SELECT is_admin,is_approved,email_confirmed FROM user").fetchall()
    assert rows == [(1, 1, 1)]
assert (Path("/app-data/repository") / "containersmoke.md").is_file()
'''

NO_MOUNT_CHECK = r'''
from pathlib import Path
import runpy
import tempfile
module = runpy.run_path("/opt/railway/bootstrap.py", run_name="test_mount_guard")
with tempfile.TemporaryDirectory(prefix="not-a-volume-") as directory:
    try:
        module["bootstrap"](Path(directory), require_mount=True, environ={})
    except module["BootstrapError"] as error:
        assert str(error) == "Mount a persistent volume at /app-data before starting."
    else:
        raise AssertionError("an ordinary directory bypassed the mount guard")
'''


status = "failed"
failure = None
try:
    require(shutil.which("docker") is not None, "Docker CLI available")
    docker("info", timeout=15)
    docker("image", "inspect", args.image)
    if args.upgrade_from_image:
        docker("image", "inspect", args.upgrade_from_image)
    require(True, "Docker daemon and prebuilt image available")
    with tempfile.TemporaryDirectory(prefix="otterwiki-container-smoke-") as temporary:
        work = Path(temporary)
        namespace = "otterwiki-smoke-" + secrets.token_hex(6)
        credentials = {"email": "container-admin@example.invalid",
                       "replacement_email": "replacement-admin@example.invalid",
                       "initial": secrets.token_urlsafe(32), "changed": secrets.token_urlsafe(32),
                       "replacement": secrets.token_urlsafe(32)}
        credential_values = [credentials[key] for key in ("initial", "changed", "replacement")]
        original_env = work / "initial.env"
        changed_env = work / "replacement.env"
        original_env.write_text("BOOTSTRAP_ADMIN_EMAIL=" + credentials["email"] +
                                "\nBOOTSTRAP_ADMIN_PASSWORD=" + credentials["initial"] + "\n")
        changed_env.write_text("BOOTSTRAP_ADMIN_EMAIL=" + credentials["replacement_email"] +
                               "\nBOOTSTRAP_ADMIN_PASSWORD=" + credentials["replacement"] + "\n")
        for suffix in ("data", "restored", "empty"):
            volume = namespace + "-" + suffix
            docker("volume", "create", volume)
            volumes.append(volume)

        stage = "fresh container"
        current = namespace + "-fresh"
        launch(current, volumes[0], original_env)
        wait_health(current, 120)
        require(True, "fresh: port 8080 health responds through nginx/uWSGI")
        verify_http(credentials, "fresh", initialize=True)
        docker("exec", "--interactive", current, "/opt/venv/bin/python", "-", target_version,
               input_text=RUNTIME_CHECK)
        require(True, "fresh: Python 3.11, www-data ownership/workers and sanitized supervisor environment")

        stage = "restart without bootstrap credentials"
        stop_remove(current)
        current = namespace + "-restart"
        launch(current, volumes[0])
        wait_health(current, 60)
        verify_http(credentials, "restart")
        require(True, "restart: original volume works without bootstrap credentials")
        stop_remove(current)

        stage = "offline backup and restore"
        # The application is stopped. A tar archive captures the complete volume,
        # including settings, SQLite, repository/.git and attachments. It is never uploaded.
        disposable_run(namespace + "-backup", "--entrypoint", "/bin/tar",
               "--mount", "type=volume,source=" + volumes[0] + ",target=/source,readonly",
               "--mount", "type=bind,source=" + str(work) + ",target=/backup",
               args.image, "-C", "/source", "-cf", "/backup/data.tar", ".")
        disposable_run(namespace + "-extract", "--entrypoint", "/bin/tar",
               "--mount", "type=volume,source=" + volumes[1] + ",target=/restore",
               "--mount", "type=bind,source=" + str(work) + ",target=/backup,readonly",
               args.image, "-C", "/restore", "-xf", "/backup/data.tar")
        current = namespace + "-restore"
        launch(current, volumes[1], changed_env)
        wait_health(current, 60)
        verify_http(credentials, "restore")
        docker("exec", "--interactive", current, "/opt/venv/bin/python", "-", target_version,
               input_text=RUNTIME_CHECK)
        require(True, "restore: complete offline backup works on a new named volume")
        stop_remove(current)

        if args.upgrade_from_image:
            stage = "initialize previous version for upgrade"
            upgrade["status"] = "running"
            upgrade_volume = namespace + "-upgrade"
            docker("volume", "create", upgrade_volume)
            volumes.append(upgrade_volume)
            current = namespace + "-previous"
            launch(current, upgrade_volume, original_env, image=args.upgrade_from_image)
            wait_health(current, 120)
            verify_http(credentials, "upgrade source", initialize=True)
            docker("exec", "--interactive", current, "/opt/venv/bin/python", "-", previous_version,
                   input_text=RUNTIME_CHECK)
            require(True, "upgrade source: expected 2.26.3 runtime and existing administrator verified")
            previous_head = git_head(current)
            stop_remove(current)

            stage = "upgrade existing volume without bootstrap credentials"
            current = namespace + "-upgraded"
            launch(current, upgrade_volume)
            wait_health(current, 120)
            docker("exec", "--interactive", current, "/opt/venv/bin/python", "-", target_version,
                   input_text=RUNTIME_CHECK)
            require(True, "upgrade: expected 2.27.0 runtime starts on the existing volume without bootstrap credentials")
            admin = verify_http(credentials, "upgrade")
            require(git_head(current) == previous_head, "upgrade: Git head and existing history retained")
            docker("exec", "--user", "www-data", current, "git", "-C", "/app-data/repository",
                   "fsck", "--full", "--no-dangling")
            require(True, "upgrade: retained Git object graph passes integrity check")
            updated_content = page_content + "\nEdit after version upgrade.\n"
            status_code, _, _ = admin.post("/ContainerSmoke/save", {
                "content": updated_content, "commit": "Verify a new edit after version upgrade"})
            require(status_code == 200, "upgrade: existing administrator saves a new edit")
            status_code, body, _ = admin.request("/ContainerSmoke/source?raw")
            source = re.sub(rb"\n\[\]: # \([^\r\n]*\)\Z", b"", body)
            require(status_code == 200 and source == updated_content.encode(),
                    "upgrade: new edit is preserved exactly")
            require(git_head(current) != previous_head, "upgrade: new edit creates a Git revision")
            status_code, body, _ = admin.request(attachment_path)
            require(status_code == 200 and body == attachment, "upgrade: existing attachment survives the new edit")
            status_code, body, url = Client().request("/ContainerSmoke")
            require(status_code == 200 and urllib.parse.urlsplit(url).path == "/-/login"
                    and marker.encode() not in body, "upgrade: newly edited page remains private behind login")
            upgrade["status"] = "passed"
            stop_remove(current)

        stage = "missing first-run credentials"
        current = namespace + "-missing"
        launch(current, volumes[2])
        deadline = time.monotonic() + 45
        exited = False
        while time.monotonic() < deadline:
            state = docker("inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", current).stdout.strip()
            if state.startswith("exited "):
                require(state != "exited 0", "missing credentials: full entrypoint exits unsuccessfully")
                exited = True
                break
            try:
                with urllib.request.urlopen(base + "/-/healthz", timeout=1) as response:
                    raise SmokeFailure("server became reachable without first-run credentials")
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            time.sleep(1)
        require(exited, "missing credentials: failure is bounded and server never becomes ready")
        scan_logs(current)
        docker("rm", "--volumes", current)
        containers.remove(current)

        stage = "required mount guard"
        # The inherited VOLUME declaration creates an anonymous Docker volume
        # even without --mount. Therefore omission of --mount is NOT a no-mount
        # test. Invoke the production guard against a genuinely unmounted path.
        disposable_run(namespace + "-mount-check", "--interactive", "--entrypoint", "/opt/venv/bin/python",
               args.image, "-", input_text=NO_MOUNT_CHECK)
        require(True, "required mount: real image rejects an ordinary unmounted data directory")
        require(True, "generated passwords absent from all captured process and container logs")
        status = "passed"
except Exception as error:
    # SmokeFailure is constructed only from fixed test labels/operation names.
    # All other exception details, HTTP bodies and raw logs remain suppressed.
    failure = {"stage": stage, "type": type(error).__name__,
               "reason": str(error) if isinstance(error, SmokeFailure) else "unexpected exception; raw details omitted"}
finally:
    if upgrade["status"] == "running":
        upgrade["status"] = "failed"
    for name in list(containers):
        if status != "passed":
            diagnostics.append(container_diagnostics(name))
        try:
            scan_logs(name)
        except Exception:
            status = "failed"
            failure = failure or {"stage": "log confidentiality check", "type": "SmokeFailure",
                                  "reason": "captured log confidentiality check failed"}
        try:
            subprocess.run(["docker", "rm", "--force", "--volumes", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except Exception:
            status = "failed"
            failure = failure or {"stage": "container cleanup", "type": "SmokeFailure"}
    for name in volumes:
        try:
            cleanup = subprocess.run(["docker", "volume", "rm", "--force", name],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            if cleanup.returncode:
                raise SmokeFailure("volume cleanup failed")
        except Exception:
            status = "failed"
            failure = failure or {"stage": "volume cleanup", "type": "SmokeFailure"}
    report = {"status": status, "target_version": target_version, "upgrade": upgrade,
              "checks": checks, "failure": failure,
              "failed_command": last_command_failure, "container_diagnostics": diagnostics,
              "elapsed_seconds": round(time.monotonic() - started, 2),
              "scope": "built image, inherited entrypoint, nginx/uWSGI, real loopback HTTP and Docker named volumes",
              "limitations": ["No Railway deployment, networking, TLS, billing or marketplace installation",
                              "No load or sustained resource-usage measurement",
                              "Inherited Docker VOLUME creates an anonymous mount when --mount is omitted; the no-mount negative check uses an ordinary directory",
                              "Offline tar restore only; no live SQLite backup test",
                              "Special attachment filename check covers upload/download HTTP paths, not editor JavaScript link generation"]}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print("Container smoke result: " + status + "; completed checks: " + str(len(checks)), flush=True)
    if failure:
        print("Failed stage: " + failure["stage"] + " (" + failure["type"] + ")", file=sys.stderr)
        print("Failure reason: " + failure.get("reason", "cleanup failed"), file=sys.stderr)
        print("Safe diagnostics: " + json.dumps({"failed_command": last_command_failure,
                                                "containers": diagnostics}), file=sys.stderr)
sys.exit(0 if status == "passed" else 1)
PY_CONTAINER_SMOKE
