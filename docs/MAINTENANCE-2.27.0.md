# Otter Wiki 2.27.0 maintenance

This release updates the pinned upstream application from 2.26.3 to 2.27.0.
The native application, startup wrapper, private-access defaults and `/app-data`
layout are preserved. The upstream [release notes](https://github.com/redimp/otterwiki/releases/tag/v2.27.0)
describe attachment-path protections, Git HTTP email-confirmation checks and an
attachment-link encoding fix. The upstream comparison adds no database-schema
migration; existing-data behavior is checked separately below.

## Image and source

```text
redimp/otterwiki:2.27.0@sha256:9cf1d76c6bff097ff924404aa5a149b235b15aaa05553870e96504cd1d92752b
```

Upstream source commit: `e5263912311058b13122bc35529eb4f928a67d13`.
The Docker Hub tag and registry index agreed on the multiarchitecture digest;
the downloaded manifest's SHA-256 also matched. Startup and bootstrap scripts
are unchanged from the previous release.

## Validation on 2026-11-15 (UTC)

| Check | Result and scope |
| --- | --- |
| Native application | [54 checks passed](validation/maintenance-2.27.0-application.json) under Python 3.12.14: fresh data, restart and offline restoration. |
| Administrator bootstrap | [34 checks passed](validation/maintenance-2.27.0-bootstrap.json), including interrupted initialization and preservation of an existing administrator and changed password. |
| Real containers | [82 checks passed in 38.86 seconds](validation/maintenance-2.27.0-container.json), using the pinned images, nginx/uWSGI, HTTP and Docker named volumes. |
| Previous-version upgrade | Included in the container result: data created under 2.26.3 survived startup under 2.27.0 on the same volume without bootstrap credentials. The changed password, exact Markdown and attachment, Git history and private access were retained. A new authenticated edit passed. |
| Fresh pilot export | [Checked online copy passed](validation/maintenance-2.27.0-backup.json); SQLite integrity and source stability passed, the downloaded checksum matched, and the archive was retained privately. |
| Railway pilot upgrade | [Deployment and actual 2.27.0 runtime confirmed](validation/maintenance-2.27.0-deployment.json), on the original volume with bootstrap variables absent. [24 HTTPS checks passed in 137.87 seconds](validation/maintenance-2.27.0-railway.json): existing login, exact page and attachment, privacy and a new private edit. |

The container workflow tested the same runtime files as this public release.
Their content-derived Git blob fingerprints are recorded in the reports for
comparison with the published files. Detailed internal build and deployment
references are retained privately. The original validation reports remain
historical evidence for 2.26.3; they have not been relabeled.

The test suites overlap and their check counts must not be added. The special
attachment filename covers HTTP upload/download paths; the editor's JavaScript
link insertion is not exercised. The fresh export was not itself restored to
another Railway service; an earlier checked export was successfully restored in
the [separate recovery drill](validation/railway-recovery.json). These checks do
not establish continuous uptime, load capacity or a recovery-time guarantee.

## Updating an existing installation

Updating this repository or the marketplace description changes the source
available to new deployments. It does not prove that third-party installations
have updated. Check your own deployed revision and actual application version.

1. Export your existing data privately during a quiet period and verify your
   recovery procedure on separate storage. Preserve the current deployment
   reference for rollback.
2. Review the Dockerfile change and use the tested 2.27.0 image pin. Keep the
   existing `/app-data` volume attached, with one replica and the usual startup.
3. Start the new revision. An initialized wiki does not need bootstrap variables
   reintroduced. Do not reset or recreate its administrator.
4. Check the actual running version, health route, existing login, exact pages
   and attachments, privacy and a controlled new edit. Retain the prior backup.

See [RECOVERY.md](RECOVERY.md) and [tests/README.md](../tests/README.md) for the
procedures and their limits. A single replica can be briefly unavailable during
redeployment. No paid hosting-plan change is required by this wrapper update.
