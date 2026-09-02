# Telegram AutoFilter Bot

A centrally managed Telegram search bot that anyone can add to a group. It searches one shared library of files indexed from the owner's approved source channels.

## Included capabilities

- Search a unified library from any group where the bot is present
- Index documents, media, apps, archives, and other authorized files from owner-approved source channels
- Smart search with typo suggestions, file-type filters, paginated results, and private user-bound delivery buttons that expire after ten minutes
- Optional forced-subscription check, missing-file requests, top-search analytics, broadcast, and an owner-only control panel
- One owner-controlled configuration; group administrators do not manage separate libraries

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

Only index and distribute content you own or are authorized to share.
