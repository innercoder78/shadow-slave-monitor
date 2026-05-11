# SHADOW SLAVE NEW CHAPTER MONITOR

A small automated monitor that checks for new chapters for the web novel called *Shadow Slave* and sends a notification on your phone when a new chapter becomes available.
I find it much better than spamming F5 on various websites whenever new chapters come out.

## HOW DOES IT WORK?

The workflow runs on GitHub Actions. It the primary website shows a new chapter, the monitor stops looking at it and switches to checking other websites until that chapter(s) becomes available. Once the new chapter is found on the free websites, it goes back to looking for new chapters on the main website.

When a new chapter is found, the monitor sends a notification through the ntfy app.

## APP NEEDED ON YOUR PHONE & SET UP

If you use Android:
https://play.google.com/store/apps/details?id=io.heckel.ntfy

If you use iOS:
https://apps.apple.com/us/app/ntfy/id1625396347

THE APP IS COMPLETELY FREE! NO SIGN UP INVOLVED!

If you know the topic, you can subscribe to it to receive notifications.

## REPOSITORY WITH AN EXPIRATION DATE

Once *Shadow Slave* finally comes to an end (and I’ve heard that this novel will conclude by the end of 2026 or early 2027), this repo will be abandoned, since there won’t be any reason to maintain it anymore, as no more chapters will be released. Moreover, the ability to check the chapters will expire Sunday, May 09, 2027 because that's the date when the token will expire (I will extend it if *Shadow Slave* isn't over by then, obviously).

## IF YOU DECIDE TO FORK

Things to keep in mind if you decide to fork this repo:

1) Go to the repository's "Settings," then go to "Secrets and variables," and then add a "New repository secret" and for the "Name" have it set as "NTFY_NEWCHAPTER" and in the "Secret" box set the name of the ntfy topic (you can make up any name in ntfy app, but it must match that name and it obviously can't be the same name as someone else's, so make it unique).
2) Same process but add "NTFY_ERROR_TOPIC" with the ntfy topic that is NOT THE SAME as the NTFY_NEWCHAPTER one! This one will be the one you'll get error messages (403, website layout change, etc.).
3) Go to the repository's Actions, and then click on the name of the project, and then "Run workflow" with "Branch: main"; otherwise, you won't get notifications in the ntfy app.
4) Set up an account on cron-job.org and use your GitHub token there and set everything up because the native GitHub Actions is horrible and, although they say it can run every 5 minutes, realistically only runs every 3-4 hours. To run every 5 minutes, you need an outside source telling it to run every 5 minutes, hence the need for the cron-job.org account.
