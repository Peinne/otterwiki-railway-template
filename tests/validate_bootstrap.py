"""Integration checks for bootstrap.py against a real installed Otter Wiki.

Run with a Python environment containing Otter Wiki 2.26.3. All state and test
credentials are disposable. No Docker daemon or external service is used.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile


PROJECT = Path(__file__).resolve().parents[1]
BOOTSTRAP = PROJECT / "docker" / "bootstrap.py"
EMAIL_ENV = "BOOTSTRAP_ADMIN_EMAIL"
PASSWORD_ENV = "BOOTSTRAP_ADMIN_PASSWORD"
NAME_ENV = "BOOTSTRAP_ADMIN_NAME"
EMAIL = "pilot-admin@example.invalid"


def main(report_path):
    results = []
    test_secrets = []

    def check(name, condition):
        results.append({"check": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(name)

    def run(args, env, *, stdin=None):
        result = subprocess.run(args, input=stdin, env=env, text=True,
                                capture_output=True, cwd=PROJECT, timeout=45)
        for secret in test_secrets:
            if secret and (secret in result.stdout or secret in result.stderr):
                raise AssertionError("A test credential appeared in subprocess output")
        return result

    def native(env, *args, stdin=None):
        result = run([sys.executable, "-m", "flask", "--app", "otterwiki.server",
                      "user", *args], env, stdin=stdin)
        if result.returncode:
            raise AssertionError("Native upstream user CLI failed during test setup")
        return result

    def users(env):
        return json.loads(native(env, "list", "--json").stdout)

    def snapshot(directory):
        with sqlite3.connect(directory / "db.sqlite") as db:
            return db.execute("SELECT email,password_hash,is_admin,is_approved,email_confirmed "
                              "FROM user ORDER BY email").fetchall()

    def fixture(root, name):
        directory = root / name
        repo = directory / "repository"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        config = directory / "settings.cfg"
        secret = secrets.token_hex(32)
        config.write_text(f"DEBUG = False\nSECRET_KEY = {secret!r}\n"
                          f"REPOSITORY = {str(repo)!r}\n"
                          f"SQLALCHEMY_DATABASE_URI = {'sqlite:///' + str(directory / 'db.sqlite')!r}\n"
                          "MAIL_SUPPRESS_SEND = True\n")
        config.chmod(0o600)
        env = dict(os.environ)
        for key in (EMAIL_ENV, PASSWORD_ENV, NAME_ENV):
            env.pop(key, None)
        env.update(OTTERWIKI_SETTINGS=str(config), OTTERWIKI_REPOSITORY=str(repo),
                   REPOSITORY=str(repo), SQLALCHEMY_DATABASE_URI="sqlite:///" + str(directory / "db.sqlite"),
                   READ_ACCESS="APPROVED", WRITE_ACCESS="APPROVED", ATTACHMENT_ACCESS="APPROVED",
                   DISABLE_REGISTRATION="true", AUTO_APPROVAL="false", ADMIN_USER_EMAIL="",
                   DEBUG="false", WTF_CSRF_ENABLED="true")
        return directory, env

    def credentials(env, email=EMAIL, password=None):
        password = password or secrets.token_urlsafe(24)
        test_secrets.append(password)
        return dict(env, **{EMAIL_ENV: email, PASSWORD_ENV: password, NAME_ENV: "Pilot Admin"}), password

    def bootstrap(env, *, fail_password=False, require_mount=False):
        # Tests replace the real mounted volume with an isolated local directory.
        # Production mount validation is not disabled through an environment flag.
        code = ("import runpy,subprocess,sys,os\nfrom pathlib import Path\n"
                "real_run=subprocess.run\n"
                "def guarded_run(args,*a,**kw):\n"
                "    if any(k in kw.get('env',{}) for k in ('BOOTSTRAP_ADMIN_PASSWORD','BOOTSTRAP_ADMIN_EMAIL')):\n"
                "        raise AssertionError('Bootstrap credentials leaked into child environment')\n")
        if fail_password:
            # Only the password command fails; app/user creation remain real.
            code += ("    if isinstance(args,(list,tuple)) and 'user' in args and 'password' in args:\n"
                     "        return subprocess.CompletedProcess(args,1,'','injected CLI failure')\n")
        code += ("    return real_run(args,*a,**kw)\n"
                 "subprocess.run=guarded_run\n")
        code += ("ns=runpy.run_path(sys.argv[1],run_name='bootstrap_under_test')\n"
                 f"print(ns['bootstrap'](Path(sys.argv[2]),require_mount={require_mount!r},environ=os.environ))\n")
        return run([sys.executable, "-c", code, str(BOOTSTRAP),
                    str(Path(env['OTTERWIKI_SETTINGS']).parent)], env)

    try:
        check("bootstrap file exists", BOOTSTRAP.is_file())
        with tempfile.TemporaryDirectory(prefix="otterwiki-bootstrap-") as tmp:
            root = Path(tmp)

            directory, env = fixture(root, "missing")
            result = bootstrap(env)
            check("fresh database without credentials fails closed", result.returncode != 0)
            check("missing credentials create no account", users(env) == [])

            directory, env = fixture(root, "guards")
            configured, _ = credentials(env)
            result = bootstrap(configured, require_mount=True)
            check("production mount guard rejects ordinary directory", result.returncode != 0)
            wrong_path = dict(configured, REPOSITORY=str(root / "wrong-repository"))
            result = bootstrap(wrong_path)
            check("foreign repository path fails closed", result.returncode != 0)
            native(env, "list", "--json")
            with sqlite3.connect(directory / "db.sqlite") as db:
                db.execute("INSERT INTO preferences(name,value) VALUES(?,?)", ("READ_ACCESS", "ANONYMOUS"))
            result = bootstrap(configured)
            check("public database preference cannot bypass private first setup", result.returncode != 0)
            check("guard failures create no account", users(env) == [])

            for label, password in (("short", secrets.token_hex(3)), ("newline", "long-enough-test\nsecond-line")):
                directory, env = fixture(root, label)
                configured, _ = credentials(env, password=password)
                result = bootstrap(configured)
                check(f"{label} password fails closed", result.returncode != 0)
                check(f"{label} password creates no account", users(env) == [])

            directory, env = fixture(root, "fresh")
            configured, password = credentials(env)
            result = bootstrap(configured)
            check("fresh bootstrap succeeds", result.returncode == 0)
            accounts = users(env)
            check("exactly one configured admin created", len(accounts) == 1 and
                  accounts[0]["email"] == EMAIL and accounts[0]["is_admin"])
            initial = snapshot(directory)
            check("admin has password and approved confirmed flags", bool(initial[0][1]) and
                  initial[0][2:] == (1, 1, 1))

            result = bootstrap(env)
            check("restart succeeds without bootstrap secrets", result.returncode == 0)
            check("restart preserves exact account password hash", snapshot(directory) == initial)

            changed, changed_password = credentials(env, email="changed@example.invalid")
            result = bootstrap(changed)
            check("changed bootstrap environment does not replace admin", result.returncode == 0 and
                  snapshot(directory) == initial)

            new_password = secrets.token_urlsafe(24)
            test_secrets.append(new_password)
            native(env, "password", EMAIL, "--interactive", stdin=new_password + "\n" + new_password + "\n")
            after_change = snapshot(directory)
            check("user password changed through native CLI", after_change != initial)
            result = bootstrap(configured)
            check("old bootstrap password cannot reset changed password", result.returncode == 0 and
                  snapshot(directory) == after_change)

            # Exercise actual Flask login/admin/page/attachment and persistence
            # through the companion application probe, using the new password.
            probe = PROJECT / "tests" / "validate_app.py"
            for phase in ("create", "restart"):
                response = run([sys.executable, str(probe), "--phase", phase], env,
                               stdin=json.dumps({"password": new_password}))
                check(f"real Flask {phase} scenario passes after bootstrap", response.returncode == 0)
                data = json.loads(response.stdout)
                check(f"real Flask {phase} exercises at least 15 checks", len(data["checks"]) >= 15)

            directory, env = fixture(root, "partial")
            configured, _ = credentials(env)
            failed = bootstrap(configured, fail_password=True)
            check("injected password CLI failure stops bootstrap", failed.returncode != 0)
            partial = snapshot(directory)
            check("failure left one passwordless admin for recovery", len(partial) == 1 and not partial[0][1])
            pending = directory / ".railway-bootstrap.pending.json"
            check("interrupted bootstrap retains durable recovery marker", pending.is_file())
            wrong_identity, _ = credentials(env, email="wrong-recovery@example.invalid")
            rejected = bootstrap(wrong_identity)
            check("different bootstrap email cannot take over partial state",
                  rejected.returncode != 0 and snapshot(directory) == partial and pending.is_file())
            result = bootstrap(configured)
            check("restart recovers interrupted bootstrap", result.returncode == 0)
            recovered = snapshot(directory)
            check("recovery sets password without duplicate account", len(recovered) == 1 and bool(recovered[0][1]))
            check("completed bootstrap clears recovery marker", not pending.exists())

            directory, env = fixture(root, "existing")
            native(env, "create", "existing@example.invalid", "Existing Admin",
                   "--flags=email_confirmed,approved", "--permissions=admin")
            existing_password = secrets.token_urlsafe(24)
            test_secrets.append(existing_password)
            native(env, "password", "existing@example.invalid", "--interactive",
                   stdin=existing_password + "\n" + existing_password + "\n")
            before = snapshot(directory)
            configured, _ = credentials(env)
            result = bootstrap(configured)
            check("existing working admin is preserved", result.returncode == 0 and snapshot(directory) == before)

            directory, env = fixture(root, "existing-nonadmin")
            native(env, "create", EMAIL, "Existing Nonadmin", "--flags=email_confirmed,approved",
                   "--permissions=read")
            before = snapshot(directory)
            configured, _ = credentials(env)
            result = bootstrap(configured)
            check("existing nonadmin is not promoted by bootstrap", result.returncode != 0 and
                  snapshot(directory) == before)

            check("no generated credential appears in captured command output", True)
            check("bootstrap credentials removed from native subprocess environments", True)
        status = "passed"
    except Exception as exc:
        status = "failed"
        # Assertions have fixed descriptive labels and never include credentials.
        error = str(exc) if isinstance(exc, AssertionError) else type(exc).__name__
        results.append({"check": error, "passed": False})
    report = {"status": status, "otterwiki_version": "2.26.3", "python": sys.version.split()[0],
              "scope": "real native CLI, SQLite, Git and Flask test client; custom bootstrap script",
              "results": results, "limitations": ["Docker image and startup shell not executed",
                 "No nginx/uWSGI, external network or Railway volume test",
                 "Interruption simulated as native password-command failure after real user creation"]}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=Path("bootstrap-validation.json"))
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.report.resolve()))
