# Isolated public evidence integration

The production monitor stays on `ubuntu-24.04`. Its eleven enabled public
sources retain their exact configuration, order, requests, suppression and
recovery policy. WebNovel authority checks and Telegram emergency detection
also stay there. The two locally disabled identities explicitly listed in
`ISOLATED_PUBLIC_SOURCE_NAMES` can participate only through the read-only
`ubuntu-26.04` evidence worker. Their `PUBLIC_SITES.enabled` flags stay false;
the ordinary dispatcher and manual diagnostics cannot activate them.

The integration becomes available only after review and merge to `main`.
No implementation test dispatches workflows, requests live pages, delivers
notifications or changes checked-in production state.

## Execution and scheduling

The worker-only workflow listens for the monitor workflow's `in_progress`
event on `main`, verifies the parent workflow path and repository, and checks
out the parent's exact execution SHA. Its two concurrent source checks read
that snapshot and produce one bounded JSON artifact. There is no worker
notification secret, write permission, repository-state write, state writer,
or independent discovery target. No other source enters its dispatcher.

This is a coordinated evidence workflow, not another monitoring authority.
It intentionally runs outside the monitor workflow's concurrency group.
Putting an optional job in that group would let a queued runner hold the
entire workflow open, delaying later authority checks even without a `needs`
edge. The existing monitor concurrency group, cancellation policy, state-writer
dependency, writer concurrency group, and runner assignments remain intact.
Worker queueing, timeout, cancellation, setup failure and skipped runs cannot
hold those groups or affect the parent workflow's completion.

Only an initialized `watch_free_sites` snapshot with
`latest_seen < target_chapter <= latest_webnovel` permits worker requests.
First setup, `watch_webnovel`, and emergency operation do not request or consume
worker evidence. Normal initialization retains its existing history behavior.

After its existing local public requests finish, the monitor makes one bounded,
opportunistic GitHub API lookup. It inspects at most five recent worker runs at
its exact checkout SHA, looking for its own parent-run/attempt artifact name.
It never polls for worker completion. The lookup has a twelve-second monotonic
budget, two-second connection/read timeouts, bounded streamed responses, and
no retries. It does not run before scheduled WebNovel checks. An existing local
report above the target still uses the existing authority recheck path.

**Timing limitation:** if runner queueing, setup, source retries, or artifact
publication finish after aggregation, the two extra sources do not participate
in that parent run. Even a successful artifact is never reused in a later run.
The next normal poll starts a fresh opportunity with its own provenance. Fast
local checks or slow hosted worker startup may therefore reduce participation;
live observation is needed to measure this. No authority window or polling
policy is expanded to compensate. Parent checkout synchronization can also
advance beyond the dispatch SHA; that safely excludes the older worker.

## Evidence and trust boundary

The only permitted payload is schema version 1: an exact envelope and exactly
one result for each allowlisted identity. Unknown fields, sources, duplicate
identities or JSON keys, nonfinite numbers, malformed JSON and payloads over
16 KiB fail closed. ZIP input is limited to 32 KiB and one `evidence.json`
member; paths are never extracted. Decompressed content has an independent
16 KiB limit. GitHub responses and the candidate payload are untrusted input.

Each envelope binds repository, actual checkout SHA, **parent** workflow run ID
and attempt, SHA-256 of the exact state bytes, mode, authoritative target and
latest confirmed WebNovel chapter. Parent provenance is distinct from the
worker workflow's own run ID. The consumer compares it with its own current
context and rejects state/target/history changes before use. Observation and
production timestamps must be UTC, no more than ten minutes old and no more
than five seconds in the future; an observation cannot postdate production.

Download URLs are constructed from verified repository and numeric artifact IDs.
Only one HTTPS signed redirect to the configured GitHub artifact storage host
families is accepted. The GitHub API token is never forwarded to that host.
Unexpected storage hosts safely disable this optional integration. No signed
URL, credential, raw HTML, chapter text or arbitrary exception string is logged.

The worker reuses dedicated latest-release parsers and the bounded HTTP client,
requiring visible exact series identity for both sources. It rejects every
redirect, including same-host redirects, rather than changing series context.
Canonical individual chapter paths, visible numbers, title attributes and
source-specific title/slug policies must agree. Missing required titles,
contradictory links/titles/numbers, challenges, listings, counts, pagination and
unrelated text cannot supply candidates. Duplicate latest-release URLs claiming
the same chapter must agree. Optional titles are not fetched from detail pages;
there is no enrichment failure capable of erasing valid number/URL evidence.

The consumer independently revalidates source identity, canonical chapter URL,
chapter range and source title policy. A supplied target title must also agree.
A worker observation above the confirmed target is classified separately and
contains no candidate number or URL. Candidate evidence above WebNovel's
confirmation is rejected. Worker results cannot open or advance targets,
advance WebNovel, alter authority failure cycles, activate emergency detection,
or deliver notifications on their own. Job success, HTTP 200 and artifact
existence are never acceptance criteria.

## Aggregation, failure recovery and persistence

Validated reports join the existing aggregation before all-sources-failed
health decisions. They retain configured source priority and corroborate a
single existing notification/pending range. Only configured general source
URLs enter reports and pending state; notification formatting is unchanged.
Delivered targets are ineligible, and successful pending delivery returns
before evidence consumption. Existing peers can succeed without worker input;
one worker source can succeed when its peer or all local sources fail.

The optional additive `isolated_public_source_failures` map contains only the
two allowlisted identities and counters from one to four. A separate map is
necessary to preserve the ordinary dispatcher's disabled-source boundary and
the existing sources' failure history without changing their parser revision.
It uses the same four-failure saturation and deterministic five-minute recovery
window every thirty minutes. Actual HTTP, network, TLS, policy, parser and
internal source failures are classified separately. Setup, missing evidence,
artifact transport and integrity errors do not change source counters. Valid
recovery clears only that source's counter. Target-cycle completion clears the
worker map alongside the existing public watch-cycle counters.

Old state defaults to an empty map in memory. No schema/parser version bump or
production-state migration commit is needed: normal state comparison treats an
absent map and an empty map equivalently. Repeated observations, saturated
failures, suppressed checks, unavailable artifacts, and unchanged state do not
cause routine persistence. The existing monitor and state writer remain the
only decision and persistence authorities. A stale artifact with different
worker counters cannot bypass the base-hash lease through cursor reconciliation.

The watchdog still reads only the monitor workflow's existing result artifact.
Worker failures cannot create a parent outage or hide a failed monitor result.
The existing limitation around notification delivery followed by failed state
persistence remains: this integration cannot make those external effects atomic.

## Validation and live proof still required

Offline tests cover runner/configuration isolation, dedicated parser boundaries,
HTTP versus parser failures, independent partial success, artifact transport and
size bounds, exact provenance, stale/mutated state, timestamps, unsafe URLs,
unknown/duplicate fields and identities, target authority, first setup, ordering,
notification deduplication/general URLs, pending backoff, saturation/recovery,
quiet persistence, writer conflicts and watchdog interpretation. Workflow tests
cover queued, running, failed, skipped, cancelled and timed-out worker scenarios
and preserve the independent parent/writer job graph.

Synthetic fixtures and mocked workflow lifecycles do not prove current source
templates or successful Ubuntu 26.04 production operation. After the user reviews
and merges, live proof still requires observing the hosted image/event payload,
artifact storage redirects, exact parent/attempt correlation, timely artifact
arrival, unchanged and new release parsing, real failure/recovery, and normal
notification attribution/idempotency. No such live dispatch or delivery was
performed during implementation. Existing runner-comparison diagnostics remain
unchanged and are independent of production evidence acceptance.
