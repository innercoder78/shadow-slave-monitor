# Target-only publication verification

An existing predictable URL is not publication evidence. The paid catalog still
supplies the target number; the target-only public source never scans ahead.

The demonstrated design weakness was that a substantial, unlabeled chapter
needed an exact authoritative title before acceptance. A missing title therefore
blocked publication verification even when independent continuity was available.
The historical mismatch diagnostic does not establish which identity condition
failed, and today's edited pages cannot reconstruct the incident HTML.

Read-only structural inspection on November 7, 2026 confirmed two consecutive
published pages with a canonical URL, one article wrapper/div, a numbered `h2`,
paragraphs, and absolute adjacent chapter links. One forward label was still
only the destination's short identifier. No chapter prose is retained in fixtures.

For an unlabeled, title-less target, acceptance now requires all of the following:

1. The canonical target URL and matching visible `h2` number, in the existing
   strict article structure, with no contradictory title, series, or metadata.
2. The unchanged meaningful-prose checks: at least three distinct paragraphs,
   800 characters, 150 words, 40 distinct words, and 65% alphabetic characters.
   Navigation, hidden text, scripts, and recognized placeholder messages do not
   supply prose evidence.
3. Exact canonical adjacent navigation destinations on the target.
4. Trusted state identifying the immediately previous confirmed chapter and its
   title. Gaps, missing titles, and invalid numbers cannot establish continuity.
5. A separate direct, redirect-free request for that predecessor. It must pass
   the existing publication checks, have a title matching trusted state, and
   contain a visible link to the exact target URL. Visible link labels are ignored.

The predecessor cannot recursively borrow identity from another page. A missing,
malformed, contradictory, redirected, or unavailable predecessor fails closed.
Links alone cannot publish an empty page, and lengthy text plus a predictable URL
and number cannot establish series identity without the independent anchor.
This is continuity corroboration under the existing source trust model, not a
cryptographic guarantee of authorship or a comparison against the paid chapter's
full text. An altered trusted predecessor remains a limitation of that model.

Titled pages and pages with explicit series identity retain their existing
verification paths. The parser preserves an absent title as absent. After verified
publication, the monitor may use the already-confirmed paid-catalog target title
for the notification; it does not claim that title was read from the public page.
Notification formatting, aggregation, pending retries, and deduplication remain
unchanged. A later title edit cannot itself trigger another chapter notification.

Recognized empty and short placeholder templates already return normal waiting
results; their classification is unchanged. Unfamiliar or contradictory pages
remain failures. Four consecutive failures still suppress a source outside the
five-minute windows beginning every thirty minutes. Content becoming valid while
suppressed may therefore wait for the next recovery window (and an actual monitor
run in that window). Successful verification clears the count normally.

Parser revision 9 removes only the changed source's counts when migrating from
revisions 7/8, preserving other counts and delivery history. Older revisions keep
their previous reset behavior. No production state file is edited by this change.

Synthetic regression coverage lives in `tests/test_targeted_public_source.py`.
All delivery tests use mocks; development must not send production notifications.
