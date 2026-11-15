# Reproduce the validation checks

The Python suites exercise the native Otter Wiki CLI, SQLite, Git and Flask test client. The container suite exercises the actual image startup, nginx/uWSGI, real loopback HTTP and Docker named volumes. The HTTPS helper has checked the principal Railway pilot before and after redeployment without bootstrap variables, and a fresh installation from the saved template. A separate exporter has been tested locally and on Railway. The first scheduled Work observation also completed; it is operational evidence, not another application test suite.

## Application and bootstrap suites

Use an isolated environment with Git and Python available. These Python suites do not launch Docker or a production HTTP server.

From this repository's root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'git+https://github.com/redimp/otterwiki.git@8036a4cd2c4de5f955c4496c2e1028c0daa0d5f1'
.venv/bin/python tests/validate_bootstrap.py --report test-results/bootstrap-validation.json
.venv/bin/python tests/validate_app.py --report test-results/application-validation.json
bash -n docker/start.sh
```

The bootstrap suite uses temporary local fixtures and deliberately calls the Python test entry point with the mount requirement disabled for those fixtures. A separate negative check confirms that the production mount guard rejects an ordinary directory. This is not evidence that an actual Railway mount works.

Credentials are synthetic and generated at runtime. The tests check that they are absent from captured output. Never run these scripts against a customer volume or supply production credentials.

## Container suite

Use a host with Python 3, a working Docker daemon and port 8080 available. From the repository root:

```bash
docker build --tag otterwiki-railway:smoke .
bash tests/container-smoke.sh --image otterwiki-railway:smoke --report test-results/container-smoke.json
```

The script uses disposable containers and named volumes, generated credentials, and HTTP requests through the real nginx/uWSGI stack. It exercises first initialization, private access, administrator password changes, restart without bootstrap credentials, offline restore into a different volume, and failure handling. No repository secrets or Railway credentials are needed.

## Railway HTTPS pilot helper

`validate_railway.py` uses the Python standard library to check one authorized HTTPS origin. It refuses cross-origin redirects and HTTPS downgrades before following them. Credentials come from an owner-only `0600` JSON file containing `email` and `password`; never pass their values in command arguments or commit that file. Use distinct credential, state and report files outside the repository or in its ignored `test-results/` directory.

After the deployment has reached a successful terminal state and its generated HTTPS domain is available:

```bash
python3 tests/validate_railway.py initial \
  --base-url https://YOUR-VERIFIED-PILOT-DOMAIN \
  --credentials-file /private/admin.json \
  --state-file test-results/railway-state.json \
  --report test-results/railway-initial.json
```

The initial check verifies the native health route, private access, disabled registration, CSRF, administrator login and secure session-cookie attributes. It creates one uniquely named synthetic page and one tiny attachment, then checks that anonymous visitors cannot read them. It does not change credentials, deploy, delete data, or remove its fixtures.

After removing both stored bootstrap variables within the authorized deployment workflow and redeploying with the same persistent volume, run the same command with mode `recheck` and a different report path. Keep the original state file: the helper opens a fresh session, checks the existing fixture and attachment, and makes a small edit only if the page source still matches its synthetic content. A partially completed initial run or a manually changed fixture requires inspection before another attempt.

Requests have a 30-second timeout; the helper's health-readiness deadline is 120 seconds. That client deadline does not configure Railway. The principal pilot has a separately configured 120-second platform healthcheck timeout; a 5-minute retry window was observed in the saved template's build logs. The template composer does not expose this timeout field. Reports contain fixed check labels and booleans, without credentials, cookies, response bodies, fixture contents or URLs. Record the exact Railway deployment identity separately.

For a fresh saved-template test, deploy the template onto a new volume and use a separate administrator, credential file, state file and report. Run the helper in `initial` mode only after successful deployment. Do not copy the principal pilot's volume or test state. Confirm the generated password's format and difference from the prior pilot without recording either secret. This initial run verifies HTTPS application behavior; it does not by itself prove later persistence, restoration or source access from another Railway account. The helper verifies a running service; it does not deploy one.

## Controlled backup and recovery

The local exporter checks require Python 3.12+ and Git, with no Docker daemon or additional Python packages:

```bash
python3 tests/validate_backup.py --report test-results/backup-local.json
```

The [local report](../docs/validation/backup-local.json) covers a synthetic Git repository, SQLite WAL state, private output, restoration into another directory, overwrite refusal and source-change refusal. That directory restore is not a deployed Railway restoration.

Follow [RECOVERY.md](../docs/RECOVERY.md) for the authorized Railway export and independent-volume restoration procedure. The exporter opens the source SQLite database read-only, uses its Online Backup API, copies the other persistent files and checks source stability. It requires a quiet window and does not create an atomic volume snapshot. Never commit an archive or expose it as a public CI artifact; it contains authentication material and settings secrets.

## Observed validation

On 2026-11-14, the pinned upstream source passed 79 selected upstream tests and a separate 54-assertion create/restart/offline-restore scenario under Python 3.12.14. The custom bootstrap passed 34 integration controls, including two nested Flask phases with 38 assertions. These suites overlap and should not be presented as one count of independent scenarios.

The historical container CI run passed **43 checks in 27.54 seconds**, after a Dockerfile permissions fix. That run belongs to the private development repository and is not publicly accessible; it is not a CI run of the published source snapshot. This duration is the smoke-test execution time, not the duration of the complete workflow. The [sanitized container report](../docs/validation/container-validation.json) records the image build and runtime result, including Python 3.11, the full entrypoint, nginx/uWSGI, worker ownership, real HTTP and Docker named volumes. Restart without bootstrap credentials and restore into a different named volume both passed.

The workflow's PR head was `581cd538e5d2f1dec26f75d20b82e4af3e017260`. Its executed checkout `f63752305461d15876a4d31a71441dc3e6e820d0` and merged `main` commit `8ffc07c4f42eff6d80eacb5e15e5cf9236e66f5c` have the same Git tree, `7590990e725edee5e326906144b2453e311bf737`. That report and run identify the container-tested content; the separate Railway reports below record the deployed-service checks.

On 2026-11-15, the [initial Railway HTTPS run](../docs/validation/railway-initial.json) passed **21 checks in 98.68 seconds**. After both bootstrap variables were removed and the service was redeployed on the same volume, the [recheck](../docs/validation/railway-recheck.json) passed **24 checks in 132.16 seconds**. These runs cover administrator login, private access, session-cookie attributes, CSRF, disabled registration, and exact synthetic page and attachment contents. The recheck opened a fresh session with the same credentials, verified persistence, and saved a small private edit. Their checks overlap and must not be added as independent scenarios. These durations include client/network overhead and do not measure server request latency. The [deployment report](../docs/validation/railway-deployment.json) records platform configuration, bootstrap-variable removal and the distinction between the configured source pin and the commits reported by Railway.

The [fresh saved-template installation](../docs/validation/template-initial.json) then passed **21 HTTPS checks in 144.02 seconds**, using a new volume and wiki account with no copied pilot data. The generated password was confirmed to contain 64 hexadecimal characters and to differ from the principal-pilot password. The installation was made under the same Railway owner account; it does not establish private-source access for an unrelated account. This report is an initial run, not a same-instance persistence recheck.

The [Railway export report](../docs/validation/railway-backup.json) records a successful checked export under Python 3.11, SQLite integrity, source stability, private archive permissions and a matching checksum after download. The source service was neither stopped nor redeployed for the export.

The [Railway recovery report](../docs/validation/railway-recovery.json) records successful offline restoration onto a new, separate volume, with SQLite and Git integrity and preserved metadata. The original pilot remained unchanged. After removal of the temporary maintenance command, normal startup without bootstrap variables reached `SUCCESS` at **2026-11-15T19:02:58.081Z**. The [restored-copy HTTPS recheck](../docs/validation/railway-restore-https.json) passed **24 checks in 157.46 seconds**, verifying the original administrator credentials, exact synthetic page and attachment, private access, disabled registration and a new private edit. The destination remained offline during restoration. This controlled exercise is not an automated recovery service, an atomic restore or a recovery-time guarantee.

The [first Work monitor run](../docs/validation/work-monitor.json) completed with one healthy replica, no alerts and no failures. It reported HTTP response counts, made no corrective changes and did not inspect monetary usage or remaining credit. Its scheduled and actual start times differ; one completed run is not evidence of continuous monitoring or future execution success. Historical operational observations from the earlier deployment report are retained; personal monetary counters are omitted.

Sanitized reports are in [docs/validation](../docs/validation). The application report covers the underlying application; the bootstrap report covers the Python startup logic; the container report covers the built image. The container checks also overlap the earlier suites and must not be added to their counts as independent scenarios.

## Limits of the evidence

- The application restore copies data offline to another directory. The container restore uses a complete offline archive and a different Docker named volume. The later Railway export and independent-volume recovery supply separate evidence for a checked online copy followed by controlled offline restoration and normal application startup. Neither the export nor the multi-file restore is an atomic snapshot or transaction.
- The inherited Dockerfile declares a `VOLUME`, so omitting `--mount` creates an anonymous Docker volume. The negative mount test therefore uses an ordinary unmounted data directory; it does not prove that Railway has mounted persistent storage correctly.
- Railway HTTPS and persistence across one same-volume redeployment have passed for the principal pilot. A fresh saved-template installation and a separate controlled Railway recovery have also passed their respective checks. Enforced billing controls remain unvalidated. A third-party installation remains a future observation, not a requirement to create another Railway account or repeat the full suite before publication; the source-dependency checks are anonymous access to the authorized public repository and absence of creator-specific secrets or resource references in the template.
- These checks do not measure load handling or sustained resource usage. They do not establish an operating-cost or availability guarantee.

The source repository and [Railway template](https://railway.com/deploy/otter-wiki-private-wiki) were published on **2026-11-15**. The installation and recovery evidence above describes the previously tested runtime files and retains its historical scope; it does not establish continuous operation. See the [product README](../README.md) for installation and maintenance guidance.
