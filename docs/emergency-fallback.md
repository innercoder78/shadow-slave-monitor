# Emergency chapter detection

Normal monitoring keeps WebNovel authoritative. Only failed scheduled checks in
`watch_webnovel` contribute to `webnovel_failures`; optional rechecks during
`watch_free_sites` and first setup do not contribute. An equal, trustworthy
WebNovel chapter is successful. Regression below WebNovel's own previous chapter
is a failure, even when emergency notification history is ahead.

The counter saturates at four. At four, Telegram is checked immediately in the
same run, then after each subsequent failed scheduled WebNovel check. Other
public sources are never dispatched by this path. There is no expiration or
total emergency chapter quota. The ordinary chapter-number bounds still apply.

The existing eight-minute window within each UTC 20-minute cycle remains intact.
Eligible runs may check WebNovel more than once in a cycle, allowing recovery
inside the window. `webnovel_failure_cycle` deduplicates failure counting and
emergency dispatch, not authority requests. `webnovel_success_cycle` records the
latest eligible cycle with a trustworthy success. Success resets the streak and
wins over every failure in that same cycle, in either order, including after a
process restart. A first observed fourth failure still activates immediately;
a later same-cycle success stops polling and resets the streak immediately.

Persisting a successful cycle costs one operational write per successful
20-minute cycle even when its chapter is unchanged. It is necessary because a
later independent process cannot otherwise distinguish an earlier success from
an entirely failed cycle. Duplicate successes in the same cycle do not write.
During outages, the existing failure marker likewise needs one write per failed
cycle even after the counter saturates. There are no routine five-minute timing
writes, and skipped/missing runs do not change either marker or the counter.
The markers survive recovery. The workflow's five-minute external trigger,
runners, permissions, concurrency groups, retries, and HTTP limits are unchanged.

A forced check can always attempt WebNovel. Its failure contributes only inside
an eligible, previously uncounted cycle with no recorded success. Forced
off-window failures never count
or poll Telegram; a successful forced check can recover immediately. Repeated
forced checks inside a claimed cycle cannot recount it or repoll Telegram.

Emergency parsing uses the configured channel's recognized message containers
and independently numbered canonical Telegra.ph links or linked numbered
document titles. It reuses the document/slug parsers and safe HTTP fetcher, without
fresh target/title inference, loose surrounding-text matches, document downloads,
or requests to external chapter links. Document and link numbers and normalized
titles must agree where both exist; contradictory or unsafe evidence fails
closed. One message may release multiple chapters: documents and links are
collected by chapter number, with independent evidence required for each chapter.
Duplicate matching evidence is harmless; conflicting titles, mislabeled chapter
links, unmatched unlinked documents, or a title moving between document/link
numbers reject the whole report. Properly paired chapters may share a title.
Title-only documents can corroborate only one uniquely numbered candidate;
title-only messages cannot authorize a chapter. Advances of more than
the existing 25-chapter suspicious-jump threshold require evidence for every
intervening chapter in that response. Repeated smaller verified advances are
unlimited throughout a long outage. If a large gap cannot be corroborated, the
monitor waits for sufficient evidence or recovery rather than guessing.

Old states migrate to zero failures and null emergency fields, including a null
successful-cycle marker. Both cycle markers reject booleans and out-of-range
integers; a recorded successful cycle cannot retain a failure streak from that
cycle or earlier. Active emergency
status is derived solely from the counter reaching four; retained historical
`emergency_provenance` does **not** activate polling. Provenance records source,
chapter, and canonical evidence URL, with strict types, bounds, and fields. It
supports the narrow exception allowing delivered/pending emergency chapters
above `latest_webnovel`; `latest_webnovel` is never advanced by Telegram.

Notifications use the existing format, Telegram identity, and configured general
source URL. Evidence URLs remain in state. Successful delivery alone advances
`latest_seen`; failures use the existing pending queue and backoff. New evidence
merges into an existing pending range without an extra delivery attempt or retry
timer reset. Recovery preserves pending delivery and provenance, even when
WebNovel is behind the delivered or pending chapter. WebNovel catchup cannot
renotify old chapters. Once it advances beyond delivered/pending history, normal
target-aware public monitoring resumes.

`emergency_last_check_ok` records whether the active fallback was usable. A
successful emergency check is `degraded`, with the WebNovel outage retained in
logs and `degraded_reasons`. Failed fallback checks and pending delivery failures
remain `failed`. Skipped runs retain the current cycle's degraded/failed status;
a success from an older cycle cannot stand in for a missing current check.
Existing watchdog handling already distinguishes these result labels and treats
degraded operation as functioning, while failed operation retains outage alerts.
No activation/continuation ntfy messages are introduced.

The state writer applies transitions through its existing base-hash lease.
Stale artifacts with different emergency history/provenance/success markers, or conflicting
same-chapter pending retry data, are rejected conservatively. Chapter maxima or
wall-clock timestamps cannot establish whether a counter reset supersedes a
failure, so these conflicts are not automatically merged. Cursor reconciliation
cannot bypass this rule. Unchanged operational state is not saved. Workflow
serialization and the existing checkout fence continue to protect side effects;
if persistence fails, the monitor cannot guarantee exactly-once delivery across
a later independent process, as with the existing notification mechanism.
