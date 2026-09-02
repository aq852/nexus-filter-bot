# Telegram AutoFilter Bot

A centrally managed Telegram search bot that anyone can add to a group. It searches one shared library of files indexed from the owner's approved source channels.

## Included capabilities

- Search a unified library from any group where the bot is present
- Index documents, media, apps, archives, and other authorized files from owner-approved source channels
- Smart search with typo suggestions, file-type filters, paginated results, and private user-bound delivery buttons that expire after ten minutes
- Optional forced-subscription check, missing-file requests, top-search analytics, broadcast, and an owner-only control panel
- One owner-controlled configuration; group administrators do not manage separate libraries
- Group-local tools for administrators: welcome messages, rules, keyword replies, blacklist, anti-spam, and search on/off

## Technical direction

- Python 3.11+
- aiogram 3 for Telegram handling
- MongoDB for indexed files, settings, users, and requests
- Docker Compose for local development and deployment

## Project structure

```text
src/            bot application code
tests/          automated tests
docs/           setup and operating notes
```

## Quick start

1. Create a bot with [@BotFather](https://t.me/BotFather), copy `.env.example` to `.env`, and set `BOT_TOKEN` and `OWNER_ID`.
2. Start MongoDB with `docker compose up -d mongo`, then install and run the bot with `pip install -e .` and `python -m src.main`.
3. Add the bot as an administrator in every source channel. From your private owner chat, run `/addsource -1001234567890` for each channel.
4. Post authorized files to a source channel. The bot indexes its filename and caption automatically.
5. Add the bot to any public group. Members can search by sending a title or filename.

Useful owner commands: `/panel`, `/stats`, `/addsource`, `/removesource`, `/broadcast`, `/ban`, `/unban`, and `/closerequest`.

## File management

Use `/recentfiles` or the **Recent files** control-panel button to get file IDs. Then manage indexed search metadata without reposting the file:

- `/renamefile FILE_ID | New title` updates the searchable display title.
- `/addtags FILE_ID | tag1 tag2` adds search tags such as language, quality, or topic.
- `/removefile FILE_ID` removes a stale entry from the searchable index; it does not delete the original Telegram channel post.

## Owner upload inbox

Set `STORAGE_CHANNEL_ID` to an owner-controlled channel where the bot is an administrator. Then send a supported file to the bot in a private chat as the configured owner. The bot copies the file into that channel and indexes it immediately, so it is searchable in groups without manually posting it to a source channel.

## Inline search

Enable inline mode for the bot in BotFather using `/setinline`. After that, people can search from any Telegram chat with `@YourBotUsername title`. Each result uses a deep link that opens the bot privately; the user then completes the usual subscription check and receives a personal ten-minute delivery button.

## Scheduled broadcasts

Send a broadcast immediately with `/broadcast Your message`. To schedule one, use `/schedule YYYY-MM-DD HH:MM | Your message`; the time uses `TIMEZONE` (defaults to `Asia/Kolkata`). Use `/schedules` to list pending messages and `/cancelschedule SCHEDULE_ID` to cancel one. Scheduled sends are recorded in the owner control panel.

Group-admin commands: `/settings`, `/setwelcome`, `/clearwelcome`, `/setrules`, `/rules`, `/filter`, `/stopfilter`, `/blacklist`, `/unblacklist`, `/antispam on|off`, and `/disable` or `/enable`.

## Group settings panel

Group administrators can open `/settings` to toggle group search and anti-spam using buttons, view the current rules, and see the relevant management commands. This panel only changes settings for the current group; it never changes the shared file library.

## Request fulfilment

Missing-file requests are grouped by search term and sent to the owner. When you add a likely matching authorized file to a source channel, the bot automatically marks the request fulfilled and privately notifies every requester with a ten-minute delivery button.

Only index and distribute content you own or are authorized to share.
