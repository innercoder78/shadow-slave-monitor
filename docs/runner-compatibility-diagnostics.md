# Runner compatibility diagnostics

This is a manual, read-only investigation. It does not migrate production or
establish the cause of the October 26–27, 2026 incident. The historical authority
failure was an HTTPError with an unknown status. The emergency source returned
HTTP 200 with invalid evidence; the later ordinary source check exercised a
different parser. PR #82 corrected an independently reproducible emergency URL
validation defect. These observations do not establish an operating-system cause.

After independent review and merge:

1. Open the repository's **Actions** tab.
2. Select **Runner compatibility diagnostics**.
3. Select **Run workflow**, choose `main`, and deliberately dispatch once.
4. Inspect both **Compare (ubuntu-24.04)** and **Compare (ubuntu-26.04)** jobs.
   Open each job's summary and its diagnostic step log.

Do not run the production monitor to obtain comparison results. This workflow
has no automatic triggers, notification secrets, state artifact uploads or write
permissions. It has no production concurrency group and cannot cancel a monitor.
It does not download documents or chapter contents. Each source gets one normal
invocation using the existing bounded retry policy; there are no extra probes.

Both jobs check out the immutable dispatch SHA, including the same state bytes,
and run the same Python 3.12 module with the same hash-locked dependencies. Compare
`checkout_sha`, `state_snapshot_sha256` and the emergency `baseline` first. Runner
image, OS, Python, requests, urllib3, OpenSSL and UTC execution metadata help
identify environmental differences. Jobs can start at different times, and a
source can publish a new chapter between checks.

The authority check calls production `check_webnovel(WEBNOVEL_SOURCE)` and
`require_valid_webnovel_report()`. The emergency check calls production
`check_telegram_emergency(site, baseline)`, then validates report identity and its
canonical evidence URL with the same shared validator used by the monitor. It
does not call the ordinary parser or use the authority report as a target.
The snapshot is read once and validated by `validate_state()`; the baseline is
`max(latest_seen, latest_pending_chapter)` when pending, otherwise `latest_seen`.
Missing, malformed, invalid or uninitialized snapshots fail setup for the
emergency check. They do not suppress the authority check or initialize state.

The standalone, sequential process temporarily wraps the parser's fetch helper
to observe successful transport metadata, forwarding all arguments and the
returned document unchanged. It restores the helper and suppresses source logs
(including titles) during each check. Production code is unchanged. Outputs
contain fixed fields, validated chapter numbers, bounded versions, snapshot
digests and shared allowlisted diagnostics. No titles, raw exceptions, response
bodies, arbitrary URLs/headers or redirect destinations are printed. An HTTP
status is `unavailable` when it was not observed; status 200 alone never validates
a chapter. `http_completed` means the normal bounded fetch returned a document,
including all response policy checks and body streaming.

| Category | Interpretation |
| --- | --- |
| `SUCCESS_VALID_CHAPTER` | Valid authority chapter, or valid emergency evidence newer than baseline. |
| `SUCCESS_NO_ADVANCE` | Valid emergency evidence equal to or older than baseline; parser check succeeded. |
| `HTTP_ERROR` | Exact observed status, with access-denied (401/403), rate-limit (429), server-error (5xx) or other HTTP classification. No inference about blocking. |
| `NETWORK_ERROR` | Timeout, connection failure or another requests failure. A received status can coexist with an incomplete body read. |
| `TLS_ERROR` | Certificate or TLS handshake failure, classified before the broader connection-error type. |
| `HTTP_POLICY_ERROR` | Shared HTTPS/host/redirect, content-type or response-size rejection. |
| `PARSE_ERROR` | Retrieved document failed production chapter validation. Missing evidence, invalid evidence, ambiguous evidence and other shared parser codes stay distinct. |
| `SETUP_ERROR` | A valid read-only emergency baseline could not be established. |
| `INTERNAL_ERROR` | Unexpected diagnostic/checker failure. No raw exception or traceback is printed. |

The existing emergency parser deliberately groups contradictory evidence and
suspicious jumps under `PARSE_AMBIGUOUS_CHAPTER`. Empty, malformed, incomplete or
changed markup can produce `PARSE_NO_CHAPTER_LINKS`; it cannot establish why
evidence is missing. This diagnostic preserves those distinctions and limits;
it does not duplicate parsing to guess a narrower cause. The grouped-release
fixture, including document-only exact `?single` evidence, is covered offline;
a live response will exercise that path only if the source still serves it.

Each summary explicitly identifies source failures. Expected source failures
complete the diagnostic step with an Actions warning; a green job means the
investigation completed, not that both sources passed. Setup and internal errors
exit nonzero. Installation, runner provisioning and other workflow failures
remain failures. Always inspect both source result objects in both matrix jobs.

Interpret a single comparison cautiously:

- Authority failure on both runners with emergency success suggests a
  source-specific access or parsing issue, but does not prove its cause.
- Both sources failing on only one runner warrants environment/network
  investigation, using the exact error classes and statuses as evidence.
- Authority failure confined to one runner warrants controlled follow-up;
  time, egress routing and source policy can confound an OS correlation.
- Emergency HTTP 200 with parser rejection is a separate outcome from authority
  HTTP failure. Compare the corrected emergency parser on both runners; ordinary
  parsing success does not establish emergency parsing success.
- Both sources passing on both runners provides current compatibility evidence,
  not proof about the historical failure or future availability.

A subsequent production migration requires a separately reviewed decision,
successful production-equivalent checks on 26.04, equivalent baseline/revision,
understood material environment differences, and no unexplained 26.04-only
regression. A single successful mocked test run or live snapshot is insufficient
to explain an intermittent incident. Source-access differences, missing metadata,
setup/internal failures, parser rejections or inconsistent timing require further
investigation before migration. Keep production on Ubuntu 24.04 in this PR.
