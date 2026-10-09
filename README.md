# SHADOW SLAVE NEW CHAPTER MONITOR

A small automated monitor that checks for new chapters of the web novel *Shadow Slave* (but could easily be mofified to work with any other web novel) and sends a notification to your phone when they become available.

I find it much better than repeatedly refreshing several websites whenever new chapters are expected.

## HOW DOES IT WORK?

The main workflow is triggered every five minutes through cron-job.org.

While waiting for a new chapter, the monitor checks WebNovel approximately every 20 minutes. When the paid source confirms a new chapter, the monitor begins checking public free sources until that exact chapter becomes available.

Once the chapter is found, the monitor sends a notification through ntfy and returns to watching the paid source for new chapters.

If an ntfy notification fails, it is saved and retried later. The monitor continues checking for newer chapters in the meantime and can combine several pending chapters into one notification.

A separate watchdog runs through GitHub Actions approximately every 90 minutes. It sends an error notification only if the main monitor has not completed successfully for at least five hours.

## PHONE APP

The ntfy app is available for both Android and iOS:

* [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
* [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)

The app is free and open source. Subscribe to the ntfy topic used by the repository to receive notifications.
If you're a Shadow Slave fan and you want the ntfy topic I'm using to get notified when a new chapter comes out, tag me on X (formerly Twitter) or in here. Otherwise, Fork this repo and set up your own ntfy topic.

## REPOSITORY WITH AN EXPIRATION DATE

This is a temporary repository. Once *Shadow Slave* comes to an end, there will no longer be any new chapters to monitor, so the repository will be retired.

The GitHub token used by my cron-job.org task expires on May 9, 2027. I will extend it if the novel is still ongoing at that time.

## IF YOU DECIDE TO FORK

Keep the following in mind if you fork this repository:

1. Go to **Settings → Secrets and variables → Actions** and create a repository secret named `NTFY_NEWCHAPTER`.

   Set its value to a long, random ntfy topic name. Subscribe to the same topic in the ntfy app. Do not share this topic publicly.

2. Create another repository secret named `NTFY_ERROR_TOPIC`.

   Use a different long, random ntfy topic. This topic receives watchdog alerts when the monitor has not completed successfully for at least five hours.

3. Open the repository’s **Actions** tab, select **Shadow Slave chapter monitor**, and run it manually once on the `main` branch.

   The first run initializes the monitor without sending a new-chapter notification.

4. Create one cron-job.org task that triggers the **Shadow Slave chapter monitor** workflow every five minutes.

   GitHub’s native scheduler has not been reliable enough for frequent chapter checks, so an external trigger is used for the main monitor.

5. Do not create a second cron-job.org task for the watchdog.

   The **Shadow Slave monitor watchdog** workflow is scheduled through GitHub Actions.

## PUBLIC SOURCE DIAGNOSTICS

In **Actions**, select **Public source diagnostics**, click **Run workflow**, choose the
repository branch to check, and enter the exact public source name from
`src/shadow_slave_monitor/config.py` (including spaces and capitalization). The workflow
uses that selected checkout and makes a real, read-only source check, even when normal
monitor checks are suppressed. It reads the existing cursor and target context without
saving state, updating failure counters, or sending ntfy notifications. No notification
secrets or write permissions are required.

Read the `Diagnostic outcome` line in the check step's logs:

- `SOURCE_OK` means the existing parser produced a valid result.
- `HTTP_403` confirms that the server returned HTTP access denial; it does not establish
  why access was denied. `HTTP_429` means rate limiting. Other `HTTP_<status>` codes
  identify the actual returned status, including server failures such as `HTTP_503`.
- `NETWORK_TIMEOUT` and `NETWORK_CONNECTION_ERROR` identify transport failures.
  A status is omitted when no HTTP response status is available.
- `HTTP_UNSAFE_REDIRECT` and `HTTP_UNSUPPORTED_CONTENT_TYPE` identify HTTP policy failures.
- `PAGE_CHALLENGE_SUSPECTED` means an unusable page contained specific challenge
  indicators. This is evidence of a suspected challenge, not proof of bot blocking.
- `PARSE_NO_CHAPTER_LINKS`, `PARSE_NONCANONICAL_NEXT`, `PARSE_AMBIGUOUS_NEXT`,
  `PARSE_NONMONOTONIC_NEXT`, `PARSE_CHAPTER_MISMATCH`, and `PARSE_CHAPTER_INVALID`
  describe missing trustworthy links, rejected navigation, or chapter validation failures.
  `PARSE_OTHER` covers other parser failures. A parser error alone does not establish a
  site redesign or blocking.

Failure summaries include the stage, actual HTTP status, configured hostname, attempt
count, and bounded parser counters where available. Counter values saturate at 9999.
Response bodies, untrusted URLs, page titles, and arbitrary exception text are omitted.
Expected source failures are diagnostic outcomes and leave the workflow successful;
invalid source names, invalid state, dependency/setup failures, or internal errors fail
it. This workflow is independent of the scheduled monitor and watchdog.
