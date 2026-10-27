# Scheduled public source diagnostics

The independent **Scheduled public source diagnostics** workflow observes Novel
Phoenix and NovelPing, exactly the two disabled references in the existing
inactive-source diagnostic CLI. Both remain `enabled=False` in production.
It runs on explicit `ubuntu-26.04` with Python 3.12 and read-only repository
permission. The production monitor, state writer, watchdog, enabled sources,
authority decisions and notification behavior are unchanged.

After merge to the default branch, the cron expression `17 3,15 * * *` requests
execution twice daily, at 03:17 and 15:17 UTC. GitHub controls scheduling and may
delay or omit scheduled runs; these are requested times, not delivery promises.
The workflow is also available under **Actions → Scheduled public source
diagnostics → Run workflow**. Its only optional input is `reference_chapter`,
an independently confirmed chapter number. Existing strict validation rejects
invalid input before source requests. Scheduled runs supply no reference and
never fetch WebNovel, Telegram or a reference chapter from another service.

Open a run's Summary and job logs to view the existing structured diagnostic
output. It identifies the trigger, UTC execution time, requested runner and
observed OS, checked revision, exact sources, HTTP status when available, parser
classification, trusted candidate number, reference comparison and whether
observations were inconclusive. No raw HTML, chapter text or arbitrary URLs are
retained. GitHub controls log and summary retention; this is not a permanent
history database. Compare retained runs manually when needed.

HTTP access and parsing are separate outcomes. HTTP 403/429/server errors,
network timeouts, TLS errors, redirects rejected by transport policy, challenge
pages, empty listings, unavailable parsers and contradictory chapter evidence
remain diagnostic observations. HTTP 200 alone proves no chapter candidate.
Each source is checked even if its peer returns an expected failure. Existing
bounded retrieval, retries, parser validation and optional chapter-jump checks
are reused without additional request loops. A stalled first request can delay
the sequential second check; the 15-minute job limit can terminate an incomplete
run. Infrastructure and internal diagnostic errors fail the CLI/workflow;
ordinary external source failures produce warnings and structured observations.

A candidate is evidence accepted by the existing parser for that one page and
run. Without a reference, jump comparison is explicitly not performed. Results
do not establish sustained source compatibility or suitability for production.
They never activate sources, notify, write production state, or feed evidence
into the monitor. There is no artifact synchronization, cross-run cache or
automatic historical comparison. The manual runner-comparison workflow remains
available separately and is unchanged.

To stop automated observations while retaining manual execution, remove the
`schedule` block from `.github/workflows/scheduled-public-diagnostics.yml`.
Alternatively, disable this workflow in GitHub Actions (which also disables its
manual runs), or remove that file. None of these actions affects production.
