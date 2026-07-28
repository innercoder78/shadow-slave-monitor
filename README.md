# SHADOW SLAVE NEW CHAPTER MONITOR

A small automated monitor that checks for new chapters of the web novel *Shadow Slave* and sends a notification to your phone when they become available.

I find it much better than repeatedly refreshing several websites whenever new chapters are expected.

## HOW DOES IT WORK?

The main workflow is triggered every five minutes through cron-job.org.

While waiting for a new chapter, the monitor checks WebNovel approximately every 20 minutes. When WebNovel confirms a new chapter, the monitor begins checking public sources until that exact chapter becomes available.

Once the chapter is found, the monitor sends a notification through ntfy and returns to watching WebNovel.

If an ntfy notification fails, it is saved and retried later. The monitor continues checking for newer chapters in the meantime and can combine several pending chapters into one notification.

A separate watchdog runs through GitHub Actions approximately every 90 minutes. It sends an error notification only if the main monitor has not completed successfully for at least five hours.

## PHONE APP

The ntfy app is available for both Android and iOS:

* [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
* [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)

The app is free and open source. Subscribe to the ntfy topic used by the repository to receive notifications.
If you're a Shadow Slave fan and you want the ntfy topic I'm using to get notified when a new chapter comes out, message me. Otherwise, Fork this repo and set up your own ntfy topic.

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
