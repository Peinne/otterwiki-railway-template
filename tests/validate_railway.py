#!/usr/bin/env python3
"""Bounded HTTPS checks for an authorized Otter Wiki 2.26.3 pilot.

Uses only the Python standard library. Credentials are read from a local,
owner-only 0600 JSON file: {"email": "...", "password": "..."}. Never pass
credential values as command arguments. Keep credentials, state and reports
outside the repository or under its ignored test-results/ or tmp/ directory.

  python tests/validate_railway.py initial --base-url https://YOUR-DOMAIN \
    --credentials-file /private/admin.json \
    --state-file test-results/railway-state.json \
    --report test-results/railway-initial.json

After a separately authorized redeployment, repeat with mode `recheck` and
the same state file. Initial creates one uniquely named synthetic page and
one attachment. Recheck opens a new session, verifies those exact fixtures,
then edits only the synthetic page's revision number. No account changes,
password rotation, deletion, deployment, or cleanup are performed. Fixtures
remain available for inspection. A partially completed initial run must be
reviewed; it is never retried over its existing state file automatically.

Passing these HTTP checks does not establish which image or volume Railway
deployed, nor prove backup restoration or template installation. Record the
deployment identity separately. Reports contain fixed labels and booleans,
never credentials, URLs, response bodies, cookies or fixture contents.
"""

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


REQUEST_TIMEOUT = 30
HEALTH_TIMEOUT = 120
MAX_BODY = 2 * 1024 * 1024
REPOSITORY = Path(__file__).resolve().parents[1]


class ValidationFailure(Exception):
    """Only fixed, non-sensitive codes may be supplied by this module."""


def origin_of(url):
    try:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or
                parts.username is not None or parts.password is not None or
                any(ord(char) < 33 or char == "\\" for char in url)):
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = 443 if parts.port is None else parts.port
        if not 0 < port < 65536:
            raise ValueError
        return parts.scheme, host, port
    except (TypeError, ValueError, UnicodeError):
        raise ValidationFailure("invalid_https_origin") from None


def canonical_origin(url):
    scheme, host, port = origin_of(url)
    parts = urllib.parse.urlsplit(url)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValidationFailure("base_url_must_be_an_https_origin")
    authority = "[" + host + "]" if ":" in host else host
    if port != 443:
        authority += ":" + str(port)
    return scheme + "://" + authority


class FixedOriginRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 3

    def __init__(self, origin):
        self.origin = origin_of(origin)

    def validate(self, url):
        try:
            same_origin = origin_of(url) == self.origin
        except ValidationFailure:
            same_origin = False
        if not same_origin:
            raise ValidationFailure("redirect_or_request_left_https_origin")

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # This runs before urllib constructs or sends the redirected request.
        self.validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, origin):
        self.origin = canonical_origin(origin)
        self.guard = FixedOriginRedirect(self.origin)
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            self.guard, urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path, data=None, content_type=None, *, timeout=REQUEST_TIMEOUT):
        url = urllib.parse.urljoin(self.origin + "/", path)
        self.guard.validate(url)
        headers = {"Referer": self.origin + "/-/login",
                   "User-Agent": "OtterWiki-Railway-Pilot-Validation/1"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            try:
                response = self.opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                self.guard.validate(response.geturl())
                body = response.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    raise ValidationFailure("response_exceeded_size_limit")
                return response.code, body, response.geturl()
        except (urllib.error.URLError, OSError, TimeoutError):
            raise ValidationFailure("https_request_failed") from None

    def token(self):
        status, body, _ = self.request("/-/login")
        token = re.search(rb'name="csrf-token" content="([^"\r\n]+)"', body)
        if status != 200 or token is None:
            raise ValidationFailure("csrf_token_unavailable")
        return token.group(1).decode("ascii")

    def post(self, path, fields, *, csrf=True):
        fields = dict(fields)
        if csrf:
            fields["csrf_token"] = self.token()
        return self.request(path, urllib.parse.urlencode(fields).encode(),
                            "application/x-www-form-urlencoded")

    def upload(self, page, filename, content):
        boundary = "otterwiki-probe-" + secrets.token_hex(16)
        fields = {"csrf_token": self.token(), "message": "Synthetic deployment validation"}
        parts = []
        for name, value in fields.items():
            parts.append(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" +
                          name + "\"\r\n\r\n" + value + "\r\n").encode())
        parts.append(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; "
                      "filename=\"" + filename + "\"\r\nContent-Type: text/plain\r\n\r\n").encode()
                     + content + b"\r\n")
        parts.append(("--" + boundary + "--\r\n").encode())
        return self.request("/" + page + "/attachments", b"".join(parts),
                            "multipart/form-data; boundary=" + boundary)


def private_json(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
                    (hasattr(os, "getuid") and info.st_uid != os.getuid())):
                raise ValidationFailure("private_file_requires_owner_and_mode_0600")
            text = stream.read(16385)
        if len(text) > 16384:
            raise ValidationFailure("private_file_exceeded_size_limit")
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValidationFailure("private_json_must_be_an_object")
        return value
    except (OSError, ValueError, UnicodeError):
        raise ValidationFailure("private_json_could_not_be_read") from None


def local_path(value):
    path = Path(value).expanduser().absolute()
    # Do not resolve away a symlink to a sensitive input or overwrite target.
    if path.is_symlink():
        raise ValidationFailure("local_file_must_not_be_a_symlink")
    resolved = path.resolve()
    if resolved.is_relative_to(REPOSITORY):
        relative = resolved.relative_to(REPOSITORY)
        if not relative.parts or relative.parts[0] not in ("test-results", "tmp"):
            raise ValidationFailure("local_file_inside_repository_must_be_ignored")
    return path


def write_json(path, value, *, exclusive=False):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
        if exclusive:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".railway-validation-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise ValidationFailure("local_json_write_failed") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class Checks:
    def __init__(self):
        self.results = []

    def require(self, condition, label):
        self.results.append({"check": label, "passed": bool(condition)})
        if not condition:
            raise ValidationFailure(label)


def wait_health(client, checks):
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        try:
            status, _, url = client.request("/-/healthz", timeout=min(
                REQUEST_TIMEOUT, max(0.1, deadline - time.monotonic())))
            if status == 200 and urllib.parse.urlsplit(url).path == "/-/healthz":
                checks.require(True, "https_native_health_responds")
                return
        except ValidationFailure as error:
            if str(error) != "https_request_failed":
                raise
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(2, remaining))
    checks.require(False, "https_native_health_responds")


def denied(response):
    status, _, url = response
    return status in (401, 403) or (
        status == 200 and urllib.parse.urlsplit(url).path == "/-/login")


def verify_access(origin, credentials, checks):
    anonymous = Client(origin)
    checks.require(denied(anonymous.request("/Home")), "anonymous_page_requires_login")
    # Empty passwords can never create a user even if registration is enabled.
    status, body, _ = anonymous.post("/-/register", {
        "name": "Synthetic validation", "email": "probe@example.invalid",
        "password1": "", "password2": ""})
    checks.require(status == 400 and b"Registration is disabled" in body,
                   "registration_is_disabled_with_valid_csrf")
    status, _, _ = Client(origin).post("/-/login", {
        "email": credentials["email"], "password": credentials["password"]}, csrf=False)
    checks.require(status == 400, "login_without_csrf_is_rejected")
    admin = Client(origin)
    status, body, _ = admin.post("/-/login", {
        "email": credentials["email"], "password": credentials["password"]})
    checks.require(status == 200 and b"You logged in successfully" in body,
                   "fresh_session_admin_login_with_csrf")
    session = [cookie for cookie in admin.cookies if cookie.name == "session"]
    checks.require(bool(session), "session_cookie_is_present")
    checks.require(all(cookie.secure for cookie in session), "session_cookie_is_secure")
    attributes = [{name.lower(): value for name, value in cookie._rest.items()}
                  for cookie in session]
    checks.require(all("httponly" in item for item in attributes),
                   "session_cookie_is_httponly")
    checks.require(all(str(item.get("samesite", "")).lower() in ("lax", "strict")
                       for item in attributes), "session_cookie_samesite_is_lax_or_strict")
    status, body, url = admin.request("/-/admin/user_management")
    checks.require(status == 200 and urllib.parse.urlsplit(url).path == "/-/admin/user_management"
                   and credentials["email"].encode() in body, "admin_management_is_accessible")
    return admin


def fixture(state):
    identifier = state["fixture_id"]
    page = "RailwayProbe-" + identifier
    filename = "fixture-" + identifier + ".txt"
    content = ("# Railway deployment validation\n\nSynthetic fixture " + identifier +
               "\n\nRecheck revision: " + str(state["revision"]) + "\n")
    attachment = ("Otter Wiki Railway synthetic attachment\n" + identifier + "\n").encode()
    return page, filename, content, attachment


def verify_fixture(admin, state, checks):
    page, filename, content, attachment = fixture(state)
    marker = ("Synthetic fixture " + state["fixture_id"]).encode()
    status, body, _ = admin.request("/" + page)
    checks.require(status == 200 and marker in body,
                   "synthetic_page_is_readable")
    status, body, _ = admin.request("/" + page + "/source?raw")
    # Upstream appends a source-reference Markdown comment to raw responses.
    source = re.sub(rb"\n\[\]: # \([^\r\n]*\)\Z", b"", body)
    checks.require(status == 200 and source == content.encode(),
                   "synthetic_page_source_matches_local_state")
    status, body, _ = admin.request("/" + page + "/a/" + filename)
    checks.require(status == 200 and body == attachment, "synthetic_attachment_bytes_match")
    anonymous = Client(admin.origin)
    response = anonymous.request("/" + page)
    checks.require(denied(response) and marker not in response[1],
                   "synthetic_page_is_private")
    response = anonymous.request("/" + page + "/a/" + filename)
    checks.require(denied(response) and response[1] != attachment,
                   "synthetic_attachment_is_private")


def validate(args, checks):
    origin = canonical_origin(args.base_url)
    credentials = private_json(args.credentials_file)
    checks.require(all(isinstance(credentials.get(name), str) and credentials[name]
                       and "\n" not in credentials[name] and "\r" not in credentials[name]
                       for name in ("email", "password")), "credential_file_has_required_fields")
    if args.mode == "initial":
        checks.require(not args.state_file.exists(), "initial_state_does_not_already_exist")
        state = {"version": 1, "origin": origin, "fixture_id": secrets.token_hex(12),
                 "revision": 0, "initial_complete": False}
    else:
        state = private_json(args.state_file)
        checks.require(state.get("version") == 1 and state.get("origin") == origin
                       and state.get("initial_complete") is True
                       and isinstance(state.get("fixture_id"), str)
                       and re.fullmatch(r"[0-9a-f]{24}", state["fixture_id"]) is not None
                       and type(state.get("revision")) is int and 0 <= state["revision"] < 1000,
                       "state_is_complete_and_matches_https_origin")
    wait_health(Client(origin), checks)
    admin = verify_access(origin, credentials, checks)
    page, filename, content, attachment = fixture(state)
    if args.mode == "initial":
        status, _, _ = admin.request("/" + page)
        checks.require(status == 404, "unique_synthetic_page_does_not_exist")
        write_json(args.state_file, state, exclusive=True)
        status, _, _ = admin.post("/" + page + "/save", {
            "content": content, "commit": "Create synthetic deployment validation page"})
        checks.require(status == 200, "synthetic_page_was_created")
        status, _, _ = admin.upload(page, filename, attachment)
        checks.require(status == 200, "synthetic_attachment_was_uploaded")
        verify_fixture(admin, state, checks)
        state["initial_complete"] = True
    else:
        verify_fixture(admin, state, checks)
        # Never overwrite a page whose source no longer matches our fixture.
        # No user edits are merged or discarded automatically.
        state["revision"] += 1
        _, _, updated_content, _ = fixture(state)
        status, _, _ = admin.post("/" + page + "/save", {
            "content": updated_content, "commit": "Recheck synthetic deployment validation page"})
        checks.require(status == 200, "synthetic_page_small_edit_was_saved")
        verify_fixture(admin, state, checks)
    write_json(args.state_file, state)
    checks.require(True, "local_fixture_state_was_saved")


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats invalid argument values in its output.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments; use --help.\n")


def main():
    parser = SafeParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("initial", "recheck"))
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--credentials-file", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    checks = Checks()
    started = time.monotonic()
    failure = None
    report_path = None
    try:
        args.credentials_file = local_path(args.credentials_file)
        args.state_file = local_path(args.state_file)
        args.report = local_path(args.report)
        paths = (args.credentials_file, args.state_file, args.report)
        if len({str(path.resolve()) for path in paths}) != 3:
            raise ValidationFailure("credentials_state_and_report_must_use_distinct_files")
        existing = [path for path in paths if path.exists()]
        if any(left.samefile(right) for index, left in enumerate(existing)
               for right in existing[index + 1:]):
            raise ValidationFailure("credentials_state_and_report_must_use_distinct_files")
        report_path = args.report
        validate(args, checks)
    except ValidationFailure as error:
        failure = str(error)
    except KeyboardInterrupt:
        failure = "validation_interrupted"
    except Exception:
        failure = "unexpected_validation_failure"
    report = {"mode": args.mode, "status": "failed" if failure else "passed",
              "checks": checks.results, "elapsed_seconds": round(time.monotonic() - started, 2),
              "failure": failure, "request_timeout_seconds": REQUEST_TIMEOUT,
              "health_timeout_seconds": HEALTH_TIMEOUT}
    if report_path is not None:
        try:
            write_json(report_path, report)
        except ValidationFailure:
            report["status"] = "failed"
            report["failure"] = "sanitized_report_could_not_be_written"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
