# Telegram AutoFilter Bot

A centrally managed Telegram search bot that anyone can add to a group. It searches one shared library of files indexed from the owner's approved source channels.

## Planned capabilities

- Search a unified library from any group where the bot is present
- Index documents, media, apps, archives, and other authorized files
- Paginated results, spelling suggestions, secure expiring download links, and forced subscription
- Requests for missing content, broadcasts, moderation tools, and owner-only analytics
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

## Status

Repository scaffold created. The next step is configuring the bot token and implementing the first searchable index.

Only index and distribute content you own or are authorized to share.
