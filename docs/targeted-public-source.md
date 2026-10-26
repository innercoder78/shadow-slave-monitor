# Targeted public source verification

Rentry is **enabled** and remains last in source priority order. Its configured general
notification URL is `https://rentry.co/`, with allowed host `rentry.co`.
WebNovel remains authoritative: only a confirmed integer target in the existing
chapter range produces a request to `https://rentry.co/{N}ss`. Missing target
context, including first setup, makes no request. Navigation links are inspected
as structural evidence only; they are never followed or used to discover targets.

## Read-only live observations

On October 25, 2026, bounded HTTPS GETs without redirects reached all three
user-supplied pages. Each returned HTTP 200, an exact canonical link, and one
`.entry-text article` containing one direct `div`. The site warning was outside
the article. No chapter prose was logged, saved, or copied into fixtures.

| Candidate | Visible article heading | Document title | Article paragraphs | Article paragraph characters / words |
| --- | --- | --- | ---: | ---: |
| `/3224ss` | `h2`: `3224 First Thunder` | `3224 First Thunder` | 81 | 9,532 / 1,708 |
| `/3225ss` | `h2`: `3225 The Fourth Warrior` | `3225 The Fourth Warrior` | 135 | 11,645 / 2,114 |
| `/3500ss` | `h1`: `3500` | `3500` | 2 | 12 / 2 |

These observation counts include two navigation paragraphs per page. The
published examples contained substantial readable prose, and neither contained
an explicit `Shadow Slave` label. The placeholder contained only links labelled
`3499ss` and `3501ss`, with an empty heading permalink. After navigation removal
it had **zero meaningful prose, words, or paragraphs**. It had no publication
message; short-message and padded-message variants are synthetic tests.

The completed parser was also exercised directly against those live responses:
3224 and 3225 returned verified reports with the expected titles, while 3500
returned clean unavailability. This was a parser-only check, without the monitor,
state writes, or notifications. Expected titles came from the checked-in state
(`latest_title` for 3224 and `latest_webnovel_title` for 3225). A separate fresh
request to the authoritative WebNovel catalog returned **HTTP 403**, so fresh
authoritative title retrieval was not verified live in this session. The 3224
state title is a previously observed public title, not a new independent
authoritative confirmation. Automated title-corroboration tests use mocked
WebNovel target context; production supplies the title of its WebNovel-confirmed
target. Access observations describe this environment and time only.

## Published and verified

A report requires an exact canonical URL, exactly one visible numbered chapter
heading matching the confirmed target, and substantial meaningful article prose.
A visible title must agree with the authoritative expected title whenever one
is supplied. A conflicting document title, series label, chapter number,
canonical URL, extra heading, unsafe article link, or authentication/challenge
structure fails closed.

An exact rendered `Shadow Slave` or `Novel: Shadow Slave` label remains an
explicit identity route. In its absence, the page must have the observed
`article > div > h2` chapter-heading structure, a nonempty chapter title, and an
independent authoritative expected title matching after whitespace and case
normalization only. A bare number, URL suffix, HTTP 200, story character names,
or body length cannot establish novel identity. Missing expected title or
title-less populated pages without an explicit label remain errors.

Body validation retains at least 800 characters, 150 words, three distinct
paragraphs, 40 distinct words, and 65% alphabetic content. It considers visible
paragraph prose after the chapter heading, removes navigation and placeholder
messages, and counts repeated paragraphs once. Scripts, styles, hidden elements,
navigation/footer/aside containers, series labels, and text outside the article
cannot supply evidence. The published examples pass these checks after filtering.

## Not yet published versus invalid

Only a genuine HTTP 404 at the exact candidate is HTTP-level clean unavailability.
HTTP 200 is also unavailable when it matches the observed empty template:

- One canonical article with one `div` and a bare target `h1` or `h2`.
- Both exact adjacent chapter links, used only to recognize navigation.
- No contradictory heading, title, series label, or canonical URL.
- No unexpected markup or unwrapped text.
- Insufficient meaningful prose, with at most 200 characters and 40 words of
  ordinary short message text. Recognized publication messages such as
  `Coming soon` are excluded even when repeated or padded.

The short-message bounds are a conservative extension tested with synthetic
HTML; the live empty example contained no ordinary message. Short responses
without the observed structure, long unrelated prose, and malformed pages remain
errors. A page already carrying a chapter title but too little prose also remains
an error until a corresponding unpublished template is established by evidence.

Clean unavailability returns `None`, creates no report or notification, advances
no cursor, and accumulates no failures. Checks repeat on the next scheduled run.
It clears a prior failure entry when checked (including a recovery probe), and
otherwise causes no state changes or commits. Genuine errors retain existing
failure isolation, suppression, retry, and recovery handling. The read-only
diagnostic uses `TARGET_NOT_AVAILABLE` for either unavailable outcome and
`TARGET_CONTEXT_MISSING` when no confirmed target exists.

## Integration, tests, and limits

The existing HTTPS/host/path validation, no-redirect policy, response limits,
timeouts, and retries remain in force. The example 3500 is never requested in
production unless WebNovel confirms that exact target. Other public parsers,
source order and enablement, cadence, watchdog, failure revision, state schema,
state writer, and workflow configuration remain unchanged.

Verified reports use ordinary source aggregation and notification deduplication.
Notification titles, body structure, general source URLs, source ordering,
pending retries, and quiet unchanged-state persistence retain existing behavior.
The checked-in production state validates with the enabled configuration and is
not modified by tests.

Automated tests mock all HTTP requests and notification delivery. Minimal
synthetic fixtures model the observed structure, with invented body prose.
They cover both published chapter numbers, missing/contradictory identity and
title evidence, empty/short/padded placeholders, later population, hidden and
repeated content, body thresholds, unsafe URLs/redirects, HTTP/network errors and
retries, repeated clean checks without suppression or writes, first setup,
aggregation, notification URLs/deduplication, and pending delivery retries.
Future layout changes or a missing authoritative title can produce a safe parser
failure; these are not silently classified as publication lag.
