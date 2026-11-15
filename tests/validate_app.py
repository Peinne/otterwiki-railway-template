"""Exercise an installed, unmodified Otter Wiki using native CLI and Flask.

This deliberately does not claim to test the image, nginx/uWSGI or Railway.
Each phase uses a new Python process and the same on-disk SQLite/Git data.
Passwords are random, passed through stdin, never written to the report.
"""

from __future__ import annotations

import argparse

import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
EMAIL = "pilot-admin@example.invalid"
CONTENT = "# Railway smoke test\n\nPersistence marker 4920ec87.\n"
ATTACHMENT = b"Disposable attachment fixture 32b139f0.\n"


def checked_run(args, *, env=None, input=None):
    result = subprocess.run(args, env=env, input=input, text=True,
                            capture_output=True, cwd=ROOT, timeout=90)
    if result.returncode:
        # All commands operate on synthetic test data. Do not include stdin.
        raise RuntimeError(f"Command failed ({result.returncode}): {args!r}\n"
                           f"{result.stdout}\n{result.stderr}")
    return result


def phase(name, password):
    from otterwiki import __version__
    from otterwiki.server import app, db, storage
    from otterwiki.auth import SimpleAuth

    checks = []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    check("installed version 2.26.3", __version__ == "2.26.3")
    check("private read/write/attachments configured",
          all(app.config[k] == "APPROVED"
              for k in ("READ_ACCESS", "WRITE_ACCESS", "ATTACHMENT_ACCESS")))
    check("registration disabled and automatic approval off",
          app.config["DISABLE_REGISTRATION"] is True
          and app.config["AUTO_APPROVAL"] is False)
    check("CSRF enabled", app.config["WTF_CSRF_ENABLED"] is True)
    check("debug remains disabled", app.config["DEBUG"] is False)

    anonymous = app.test_client()
    admin = app.test_client()

    def get(client, path, **kwargs):
        return client.get(path, base_url="https://localhost", **kwargs)

    def token(client):
        html = get(client, "/-/login", follow_redirects=True).get_data(as_text=True)
        match = re.search(r'name="csrf-token" content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing from actual rendered form")
        return match.group(1)

    def post(client, path, data, with_token=True, **kwargs):
        data = dict(data)
        if with_token:
            data["csrf_token"] = token(client)
        return client.post(path, data=data, base_url="https://localhost",
                           headers={"Referer": "https://localhost/-/login"},
                           **kwargs)

    response = get(anonymous, "/-/healthz")
    check("health endpoint works without login", response.status_code == 200)
    response = get(anonymous, "/Home")
    check("anonymous home request prompts for login",
          response.status_code == 302 and "/-/login" in response.headers.get("Location", ""))

    if name == "create":
        response = post(anonymous, "/-/register", {
            "name": "Unauthorized signup", "email": "stranger@example.invalid",
            "password1": "unused-test-only", "password2": "unused-test-only",
        })
        check("registration POST rejected with valid CSRF", response.status_code == 400)

        response = post(admin, "/-/login", {"email": EMAIL, "password": password},
                        with_token=False)
        check("login POST without CSRF rejected", response.status_code == 400)
        response = post(admin, "/-/login", {"email": EMAIL, "password": "wrong-password"},
                        follow_redirects=True)
        check("wrong password rejected",
              "Invalid email address or password" in response.get_data(as_text=True))

    response = post(admin, "/-/login", {"email": EMAIL, "password": password},
                    follow_redirects=True)
    check("native CLI password allows web login",
          "You logged in successfully" in response.get_data(as_text=True))
    response = get(admin, "/-/admin/user_management")
    check("bootstrapped admin can access user management",
          response.status_code == 200 and EMAIL in response.get_data(as_text=True))

    if name == "create":
        response = post(admin, "/RailwaySmoke/save", {"content": CONTENT,
                        "commit": "Create disposable pilot test page"}, follow_redirects=True)
        check("page created via actual save route",
              response.status_code == 200 and "4920ec87" in response.get_data(as_text=True))
        response = post(admin, "/RailwaySmoke/attachments", {
            "file": (io.BytesIO(ATTACHMENT), "fixture.txt"),
            "message": "Add disposable pilot test attachment",
        }, content_type="multipart/form-data", follow_redirects=True)
        check("attachment uploaded via actual route", response.status_code == 200)
        response = post(anonymous, "/RailwaySmoke/save", {
            "content": "UNAUTHORIZED OVERWRITE", "commit": "Unauthorized request",
        }, follow_redirects=True)
        check("anonymous page modification prevented",
              "UNAUTHORIZED OVERWRITE" not in (Path(storage.path) / "railwaysmoke.md").read_text())

    response = get(admin, "/RailwaySmoke")
    check("saved page content readable by admin",
          response.status_code == 200 and "4920ec87" in response.get_data(as_text=True))
    response = get(admin, "/RailwaySmoke/a/fixture.txt")
    check("saved attachment exact bytes readable by admin",
          response.status_code == 200 and response.data == ATTACHMENT)
    response = get(anonymous, "/RailwaySmoke", follow_redirects=True)
    check("saved page remains private", "4920ec87" not in response.get_data(as_text=True))
    response = get(anonymous, "/RailwaySmoke/a/fixture.txt", follow_redirects=True)
    check("saved attachment remains private", ATTACHMENT not in response.data)

    with app.app_context():
        users = SimpleAuth.User.query.all()
        check("only seeded admin exists", len(users) == 1 and users[0].email == EMAIL)
        user = users[0]
        check("admin permissions and confirmation persist",
              user.is_admin and user.is_approved and user.email_confirmed)
        hash_fingerprint = hashlib.sha256(user.password_hash.encode()).hexdigest()
        db.session.remove()
        db.engine.dispose()
    git_head = checked_run(["git", "-C", str(storage.path), "rev-parse", "HEAD"]).stdout.strip()
    git_count = int(checked_run(["git", "-C", str(storage.path), "rev-list", "--count", "HEAD"]).stdout)
    check("page and attachment are in Git history", git_count >= 3)
    return {"phase": name, "checks": checks, "git_head": git_head,
            "git_commit_count": git_count, "password_hash_fingerprint": hash_fingerprint}


def orchestrate(report_path):
    test_dir = Path(tempfile.mkdtemp(prefix="otterwiki-app-"))
    instance = test_dir / "original"
    repo = instance / "repository"
    repo.mkdir(parents=True)
    checked_run(["git", "init", "-q", "-b", "main", str(repo)])
    config = instance / "settings.cfg"
    secret_key = secrets.token_hex(32)
    config.write_text("DEBUG = False\n"
                      f"SECRET_KEY = {secret_key!r}\n"
                      f"REPOSITORY = {str(repo)!r}\n"
                      f"SQLALCHEMY_DATABASE_URI = {'sqlite:///' + str(instance / 'db.sqlite')!r}\n"
                      "MAIL_SUPPRESS_SEND = True\n")
    config.chmod(0o600)
    env = dict(os.environ, OTTERWIKI_SETTINGS=str(config), READ_ACCESS="APPROVED",
               WRITE_ACCESS="APPROVED", ATTACHMENT_ACCESS="APPROVED",
               DISABLE_REGISTRATION="true", AUTO_APPROVAL="false",
               ADMIN_USER_EMAIL="", DEBUG="false", WTF_CSRF_ENABLED="true",
               REPOSITORY=str(repo), SQLALCHEMY_DATABASE_URI="sqlite:///" + str(instance / "db.sqlite"))
    flask = [sys.executable, "-m", "flask", "--app", "otterwiki.server"]
    help_result = checked_run(flask + ["user", "--help"], env=env)
    (test_dir / "user-help.txt").write_text(help_result.stdout)
    checked_run(flask + ["user", "create", EMAIL, "Pilot Admin",
                "--flags=email_confirmed,approved", "--permissions=admin"], env=env)
    password = secrets.token_urlsafe(24)
    password_result = checked_run(flask + ["user", "password", EMAIL, "--interactive"],
                                  env=env, input=password + "\n" + password + "\n")
    if password in password_result.stdout or password in password_result.stderr:
        raise AssertionError("Native password command exposed password in captured output")
    (test_dir / "password-command-sanitized.txt").write_text(password_result.stdout + password_result.stderr)

    def run_phase(name, phase_env):
        result = checked_run([sys.executable, str(Path(__file__).resolve()), "--phase", name],
                             env=phase_env, input=json.dumps({"password": password}))
        return json.loads(result.stdout)

    initial = run_phase("create", env)
    restarted = run_phase("restart", env)
    assert initial["password_hash_fingerprint"] == restarted["password_hash_fingerprint"]
    assert initial["git_head"] == restarted["git_head"]

    # No process has the app or SQLite open here: this is an offline copy.
    backup = test_dir / "offline-backup"
    restored = test_dir / "restored"
    shutil.copytree(instance, backup)
    shutil.copytree(backup, restored)
    restore_env = dict(env, OTTERWIKI_SETTINGS=str(restored / "settings.cfg"),
                       REPOSITORY=str(restored / "repository"),
                       SQLALCHEMY_DATABASE_URI="sqlite:///" + str(restored / "db.sqlite"))
    restoration = run_phase("restore", restore_env)
    assert initial["password_hash_fingerprint"] == restoration["password_hash_fingerprint"]
    assert initial["git_head"] == restoration["git_head"]
    for item in (initial, restarted, restoration):
        item.pop("password_hash_fingerprint")
    report = {
        "status": "passed", "version": "2.26.3",
        "upstream_commit": "8036a4cd2c4de5f955c4496c2e1028c0daa0d5f1",
        "python": sys.version.split()[0],
        "native_password_stdin_does_not_echo_secret": True,
        "password_hash_unchanged_after_restart_and_restore": True,
        "git_head_unchanged_after_restart_and_restore": True,
        "offline_backup_restored_to_new_directory": True,
        "phases": [initial, restarted, restoration],
        "limitations": ["Flask test client, no real HTTP server or browser",
                        "No Docker, nginx, uWSGI, Railway networking or volume test",
                        "Restore uses environment path overrides for the new local directory",
                        "No upstream bootstrap wrapper or cost/load test"]
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    shutil.rmtree(test_dir)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--phase":
        payload = json.load(sys.stdin)
        print(json.dumps(phase(sys.argv[2], payload["password"])))
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("--report", type=Path, default=Path("application-validation.json"))
        arguments = parser.parse_args()
        orchestrate(arguments.report.resolve())
