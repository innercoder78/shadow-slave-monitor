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
3. Select **Run workflow**, choose `main`, leave **diagnostic_mode** at `core`,
   and deliberately dispatch once for the authority/emergency comparison.
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

## Optional inactive-source investigation

`diagnostic_mode` defaults to `core`; the existing authority/emergency comparison,
fields, baseline handling and safety behavior above apply to that mode. Core mode
does not contact disabled references. Select `inactive_sources` for a separate
investigation of the three exact disabled references in `PUBLIC_SITES`. This mode
does not request the authority or emergency source, read a state snapshot, send
notifications or change source configuration. It refuses all retrieval if any
required reference is missing, duplicated or enabled. Arbitrary names and URLs
are not accepted. Disabled sources remain disabled after the test.

After independent review and merge, run the optional mode manually:

1. Open **Actions → Runner compatibility diagnostics → Run workflow**.
2. Choose branch `main` and set **diagnostic_mode** to `inactive_sources`.
3. Click **Run workflow** once.
4. Open **Compare (ubuntu-24.04)** and **Compare (ubuntu-26.04)**. In each job,
   inspect the summary and **Compare independent source checks without side effects**
   log. Compare the same source identities in the ordered `sources` array.
5. Confirm identical `runner.checkout_sha` values, then compare HTTP completion,
   status, transport classification, redirect rejection and parser outcomes.
   The shared `runner` object applies to every source in that job; requested label,
   actual OS version, image version, libraries and execution time distinguish the
   environments. Missing image metadata is explicitly `unavailable`.

Both jobs retain Python 3.12, the pinned actions, hash-locked dependencies,
immutable dispatch SHA, read-only permissions and `fail-fast: false`. Each source
gets one bounded fetch invocation at its configured listing URL, including only
the existing bounded retries and approved redirects. Existing HTTPS, allowed-host,
TLS, timeout, streaming-size and content-type policy is unchanged. No chapter
page is fetched, and no extra probes, bulk crawling or access workarounds occur.
One source's retrieval or parser failure does not stop the remaining checks.

HTTP and parser evidence are reported separately:

| Field / outcome | Meaning |
| --- | --- |
| `category: HTTP_ACCESSIBLE` | The bounded transport returned a document after policy checks. HTTP 200 is evidence of reachability only. |
| `HTTP_ERROR` | Observed failing status; access denial, rate limiting and server errors remain distinct classifications. |
| `NETWORK_ERROR` / `TLS_ERROR` | Timeout, connection/request failure, or certificate/handshake failure. |
| `HTTP_POLICY_ERROR` | HTTPS/host/redirect, content-type or size policy rejected retrieval. `redirect_rejected` explicitly identifies rejected redirects, including an observed 3xx status when available. |
| `http_completed` | Full bounded retrieval completed; observing HTTP 200 before a body/policy failure does not set this field. |
| `parser_available` / `parser_inspected` | A retained dedicated parser exists / was actually applied to the fetched HTML. |
| `parser_category: PARSER_CANDIDATE_FOUND` | A retained parser found a candidate that passed existing chapter range and canonical chapter URL validation. Only its number is reported in `chapter_candidate`; no title, URL or chapter body is emitted. |
| `PARSER_NO_TRUSTWORTHY_CANDIDATE` | No candidate survived those checks, or the parser rejected evidence. Counts, dates, ratings, pagination and generic numeric text are not chapter evidence. |
| `PARSER_UNAVAILABLE` | No dedicated parser exists for this identity; retrieval can still succeed. |
| `INTERNAL_ERROR` | Unexpected transport/diagnostic failure, or a separately reported parser crash. No raw exception or traceback is emitted. |
| `setup_error` | Required disabled identities could not be resolved safely; no sources were contacted. |

Novel Phoenix uses its retained latest-release candidate parser. NovelFire uses
its retained candidate parser plus existing canonical chapter URL validation;
a listing/count link alone is insufficient, even if the retained parser can
extract a number from it. NovelPing has no active dedicated parser and reports
`PARSER_UNAVAILABLE`. The ordinary enabled-source dispatcher is never called.

`additional_parser_work_required` is true for missing/unavailable/rejected parser
evidence, false when a trustworthy candidate was found, and null when an available
parser could not be inspected because retrieval failed. True calls for parser
investigation; it does not establish the reason evidence is absent. False only
means no additional work was identified by this single candidate examination.
`inconclusive` remains true unless a trustworthy candidate was found. False
describes this narrow observation, never production suitability. Parser failures
preserve successful HTTP status/completion fields. HTTP failures leave available
parsers uninspected. Expected failures or inconclusive results produce a warning;
setup/internal failures exit nonzero. A green job means the investigation finished.

HTTP access and parser compatibility are separate requirements. A single success
may reflect a temporary network condition. Different hosted jobs may have
different outbound IP addresses, but an Ubuntu runner label does not guarantee a
particular outbound IP. No public runner IP addresses are collected or logged.
None of these results activates a source or validates a complete production
integration. Production source activation requires a separate review.
