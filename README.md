# Otter Wiki on Railway

A private Markdown wiki with Git history, attachments and persistent storage.
This wrapper deploys [Otter Wiki](https://github.com/redimp/otterwiki) with one
application service and one volume. It keeps the upstream application intact and
creates the first administrator before starting the web server.

**Published on Railway on 2026-11-15.**
[Deploy Otter Wiki — Private Wiki](https://railway.com/deploy/otter-wiki-private-wiki).

## What you get

- Markdown pages with a web editor and Git-backed history.
- File attachments stored alongside your wiki data.
- An initial administrator with a password generated for each installation.
- Pages, editing and attachments restricted to approved accounts by default.
- Public registration disabled, with access managed by the administrator.
- SQLite and Git storage in one persistent volume, without a separate database
  service or an external AI API.

The application has an HTTPS address. Authentication protects the wiki content;
the administrator can change access preferences inside Otter Wiki.

## Install and create the first administrator

1. Open the Railway template and choose the workspace where you will operate the
   wiki. Confirm that it contains one application service and one volume mounted
   at `/app-data`.
2. Enter an email address you control in `BOOTSTRAP_ADMIN_EMAIL`. Leave the
   password generator in place: Railway creates a unique 64-character hexadecimal
   value for `BOOTSTRAP_ADMIN_PASSWORD`. There is no shared default password.
3. Deploy and wait for successful startup and the `/-/healthz` readiness check.
   HTTPS routing must target port `8080`.
4. Retrieve the generated password privately from the service's Railway variables
   and store it in your password manager. Open the wiki's HTTPS address and log in
   with the email you supplied. Manage subsequent user and password changes
   through Otter Wiki.
5. Create a disposable page and attachment. Confirm that a logged-out browser
   cannot read either and that registration is disabled. Redeploy on the same
   volume, log in again and verify the exact page and attachment contents.
6. Delete **both** `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` from
   Railway's stored service variables. Apply the changes and redeploy on the same
   volume. Confirm that your existing login, page and attachment still work.

The wrapper removes the bootstrap credentials from the web-server process, but
it does not delete the stored Railway variables for you. Removing them after
setup prevents an accidentally empty replacement volume from being initialized
as a new wiki with old setup credentials.

Keep your working administrator credentials securely. Changing either bootstrap
variable does not reset a password or transfer ownership of an existing wiki.

## Configuration

| Setting | Value or behavior |
| --- | --- |
| Services | One application service, one replica |
| Build | Repository `Dockerfile` |
| Persistent storage | One volume mounted at `/app-data` |
| Port | `PORT=8080`; public HTTPS routing targets the same port |
| Readiness | `/-/healthz`; a 5-minute retry window was observed in template build logs |
| Restart policy | On failure, maximum 3 retries |
| Read, write and attachment access | `APPROVED` |
| Registration | `DISABLE_REGISTRATION=true`, `AUTO_APPROVAL=false` |
| Session cookie | `SESSION_COOKIE_SECURE=true` for HTTPS |
| Upload request limit | `NGINX_MAX_UPLOAD=16M` |
| Crawlers | `ROBOTS_TXT=disallow`; authentication provides the actual access control |

Bootstrap completes before the web server starts. A fresh volume requires valid
administrator inputs; missing credentials stop startup. An existing valid
administrator and their password are preserved across restarts, including when
bootstrap variables are absent or changed.

Saved application preferences can override environment variables on an existing
or restored volume. Check the effective read, write, attachment and registration
settings inside the application after a restore.

## Persistence and recovery

The `/app-data` volume contains accounts, application preferences, settings, the
Markdown repository, Git history and attachments. Keep it attached when
redeploying. Deleting a page does not necessarily remove its content from Git
history.

Follow [the recovery guide](docs/RECOVERY.md) to export and restore the complete
data set. The export uses SQLite's Online Backup API and checks the stability of
the other persistent files. Use a quiet period without edits, uploads, account
changes, Git synchronization or deployments. This checked copy is not an atomic
snapshot of the whole volume.

Restore into a separate empty volume, keep the destination offline until
restoration succeeds, then verify login, Git history, exact page and attachment
contents, permissions and a new edit. Keep the original volume unchanged while
testing recovery.

Backups contain account password hashes and secret application settings. Store
them privately; never commit them, serve them from the wiki or attach them to a
public issue or CI artifact. Keep a separate record of the deployed source
revision, image digest and service configuration.

The template does not schedule backups or provide automatic disaster recovery.
Keep a verified backup before updating the pinned image.

## Validation

The following sanitized reports describe controlled checks of this wrapper:

| Area | Evidence |
| --- | --- |
| Actual container startup | [Container validation](docs/validation/container-validation.json): full startup, real HTTP, authentication, password changes, restart and an offline restore into another Docker volume. |
| Persistence | [Railway redeployment check](docs/validation/railway-recheck.json): existing administrator, exact page and attachment contents preserved without bootstrap variables. |
| Fresh template installation | [Initial HTTPS check](docs/validation/template-initial.json): 21 checks passed with a new volume and wiki account. |
| Controlled export | [Railway export](docs/validation/railway-backup.json): SQLite integrity, source stability, private archive creation and matching download checksum. |
| Independent recovery | [Recovery record](docs/validation/railway-recovery.json) and [restored-copy HTTPS check](docs/validation/railway-restore-https.json): original administrator, exact page and attachment, privacy and a new edit verified after normal startup on another volume. |

See [tests/README.md](tests/README.md) for reproduction instructions and the limits
of each test. These historical reports describe checks performed before
publication. The container CI run belongs to the private development repository.
The checks overlap and their counts must not be added as independent scenarios.

The reports do not establish sustained uptime, performance under load, a fixed
monthly price or a guaranteed recovery time. A deployment healthcheck confirms
readiness; it is not continuous availability monitoring.

## Hosting and maintenance

Railway hosting and storage charges depend on your plan and actual usage. The
template does not configure a monthly spending cap. Review the cost controls
available to your account and monitor storage and resource usage.

One replica does not provide high availability, and redeployments can interrupt
access. Review updates deliberately and verify them against a separate copy of
your data before changing a working installation.

Current release: **Otter Wiki 2.27.0**. See the [maintenance record](docs/MAINTENANCE-2.27.0.md) for the existing-data upgrade checks and update procedure.

The upstream image is pinned to a release and immutable digest:

```text
redimp/otterwiki:2.27.0@sha256:9cf1d76c6bff097ff924404aa5a149b235b15aaa05553870e96504cd1d92752b
```

When reporting a problem, include the application version, the failed step and
sanitized diagnostic details. Do not include credentials, session cookies, wiki
contents or backups. Separate deployment-wrapper problems from issues in the
upstream application.

## License and attribution

This is an independent deployment wrapper, not an official upstream or
Railway-verified template. See [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Otter Wiki remains the upstream
project. The otter illustration is by Christy Presler and is used under
[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/).
