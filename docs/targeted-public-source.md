# Targeted public source verification

Rentry is appended to the public source configuration with URL
`https://rentry.co/`, allowed host `rentry.co`, and **enabled=False**.
Existing source identities, URLs, order, notification formatting, timing,
failure revision, and persistent state schema are unchanged.

Read-only inspection on October 25, 2026 reached both user-supplied examples,
`https://rentry.co/3224ss` and `https://rentry.co/3225ss`, with HTTP 200 and
`text/html`. Both had an exact canonical link, one `.entry-text article`,
a bare numbered `h2`, and substantial readable paragraph content. A separate
site warning heading appeared outside the article. The first example had
81 paragraphs; the second had 135. Neither page contained an explicit
Shadow Slave identity. No complete chapter text is stored in this repository.

The observed layout is established, but safe chapter identity is not. The
numbered heading, URL suffix, document title, and story character names do
not independently establish the novel. The integration therefore remains
disabled. This access result describes this environment only.

Before enabling, inspect current example pages read-only and establish an
explicit rendered novel label inside the chapter article, alongside an
unambiguous target heading, exact canonical URL, substantial readable body,
and a chapter title consistent with the authoritative WebNovel title when
both titles are present. The current parser accepts an exact `Shadow Slave`
or `Novel: Shadow Slave` label; the inspected pages did not supply one.
Any additional supported identity structure needs independently observed
evidence and corresponding tests before the parser or configuration changes.
Do not enable merely because the URLs resolve or mocked tests pass.

Only a confirmed integer target in the configured chapter range produces a
candidate: `https://rentry.co/{N}ss`. There is no homepage discovery, next
chapter increment, navigation traversal, or source cursor. All redirects,
noncanonical paths, non-HTML responses, and oversized responses are rejected
using the bounded HTTP client. Missing target context performs no request.
Only a genuine HTTP 404 at that exact candidate returns a clean unavailable
result. It creates no report, notification, failure count, or cursor, and a
later run can detect publication. Other failures retain normal isolation,
suppression, retries, and recovery probes.

The manual diagnostic distinguishes `TARGET_CONTEXT_MISSING` from
`TARGET_NOT_AVAILABLE`. A diagnostic can inspect the disabled source, but
it still requires all parser evidence and never saves state or notifies.

Tests use minimal synthetic prose with the observed wrapper and numbered
heading structure. Positive fixtures deliberately add a novel label absent
from the live pages. They test the acceptance contract, not live availability.
Missing-label tests model the observed evidence gap and must fail closed.
Mocked delivery tests verify ordering, deduplication, pending retries, and
the existing general source URL in notifications. No live monitor run or
real notification is needed for validation.
