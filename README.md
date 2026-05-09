# Shadow Slave New Chapter Monitor

A small automated monitor that checks for new chapters for the web novel called *Shadow Slave* and sends a notification when one becomes available.

## Sources

Original release source:

- https://www.webnovel.com/book/shadow-slave_22196546206090805

Public chapter sources:

- https://lightnovelworld.org
- https://novelfire.net
- https://novelbin.com

## How It Works

The workflow runs on GitHub Actions. It first checks WebNovel for the latest official release. If WebNovel shows a new chapter, the monitor switches to checking the public chapter sites until that chapter becomes available.

When a new chapter is found, the monitor sends a notification through the ntfy app.

## Why Do This?

To get the brand-new chapters on WebNovel, you need to pay for them. Therefore, I made this quick script using Codex so people who love Shadow Slave but can’t afford to pay, or don’t want to pay, for the new chapters can still read this magnificent novel!

## Notes

This project was built with help from ChatGPT and Codex. It is mainly a personal tool, but other *Shadow Slave* readers may find it useful.

If you’re a fellow reader and want to receive a notification when a new chapter comes out, open your ntfy app and subscribe to the topic: ss-alerts-ghcode. 

## IF YOU DECIDE TO FORK

Things to keep in mind if you decide to fork this repo:
1) Go to the repository's "Settings," then go to "Secrets and variables," and then add a "New repository secret" and for the "Name" have it set as "NTFY_TOPIC" and in the "Secret" box set the name of the ntfy topic (you can make up any name in ntfy app, but it must match that name and it obviously can't be the same name as someone else's).
2) Go to the repository's Actions, and then click on the name of the project, and then "Run workflow" with "Branch: main"; otherwise, you won't get notifications in the ntfy app.
