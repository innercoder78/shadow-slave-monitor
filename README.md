# Shadow Slave New Chapter Monitor

A small automated monitor that checks for new chapters of *Shadow Slave* and sends a notification when one becomes available.

The monitor uses WebNovel as the original release source (since that’s where G3 publishes his work first), then checks several websites to see when the chapters are available to the public. When one of those sites publishes the new chapter, this script sends a notification through the ntfy app on your phone. That way, you can read the new chapters right away!

## Sources

Original release source:

- https://www.webnovel.com/book/shadow-slave_22196546206090805

Public chapter sources:

- https://lightnovelworld.org
- https://novelfire.net
- https://novelbin.com

Light Novel World and NovelFire are usually the fastest public sources. NovelBin is often slower, but it is useful as a backup.

## How It Works

The workflow runs on GitHub Actions. It first checks WebNovel for the latest official release. If WebNovel shows a new chapter, the monitor switches to checking the public chapter sites until that chapter becomes available.

When a new chapter is found, the monitor sends a notification through the ntfy app.

## Why Do This?

To get the brand-new chapters on WebNovel, you need to pay for them. So, this exists so that poor people who love *Shadow Slave* can read this magnificent novel!

## Notes

This project was built with help from ChatGPT and Codex. It is mainly a personal tool, but other *Shadow Slave* readers may find it useful.

If you’re a fellow reader and want to use the same notification topic, message me for the ntfy code.
