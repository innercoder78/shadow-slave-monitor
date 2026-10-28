# State schema retirement

The active schema no longer contains chapter-navigation cursors. A one-time
migration validates the previous inventory against an immutable Git revision,
then removes only the retired inventory entry's cursor and failure counter.
It parses the historical configuration as literal data; it never imports or
executes historical Python code. No historical source name or domain is kept
as an active compatibility alias.

The historical cursor must have exactly the old chapter/URL fields, a valid
chapter, an allowed HTTPS host, the historical series path, and a matching
numbered chapter slug. Unknown identities, malformed fields, contradictory URLs,
invalid counters, incompatible revisions, missing history, and disagreement
between the retained and historical inventory fail closed. Existing older
parser-revision resets retain their prior behavior; current suppression counters
for every remaining source are preserved.

Loading state is read-only, including diagnostics. The monitor records whether
retirement needs persistence, and saves the cleaned state even if that run makes
no operational changes. Once persisted, unchanged runs create no further writes.
A cleanup-only save preserves the operational timestamp. Chapter history, titles,
URLs, mode, target, pending notifications and retries, authority failure/success
cycles, emergency provenance, and other source counters retain their values.
The watchdog state and behavior are unchanged.

The writer normalizes both current state and artifacts before comparison and
writes. A fresh historical artifact can persist its cleaned state under the
existing base hash lease. A superseded artifact can only trigger cleanup of the
fresh repository state: it cannot replace newer state with its old snapshot.
Conflicting stale artifacts are rejected. The cursor-only reconciliation path
is removed, so no artifact can restore retired cursor data. After cleanup,
replaying an old artifact produces no repeated cleanup commit. Existing writer
push retries and concurrency remain unchanged.

Checkouts that load monitor state include Git history so the immutable schema
object is available, including after ordinary state-only commits. The dependency
is local Git, already required by the repository; migration performs no network
requests. A standalone or shallow checkout missing that object refuses legacy
state rather than guessing. Already cleaned current state does not need history.
All action and package dependency pins remain unchanged.

The tracked state change deletes only the obsolete cursor block. It does not
copy a replacement chapter snapshot. Development never writes production state.
If a concurrent production update causes a Git merge conflict, preserve the
newer production fields when resolving it; the validator can safely retire a
retained valid historical cursor at runtime. Never take the feature branch's
entire older state snapshot as a conflict resolution.
