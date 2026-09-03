# AkMovieVerse

A centrally managed Telegram search bot that anyone can add to a group. It searches one shared library of files indexed from the owner's approved source channels.

## Included capabilities

- Search a unified library from any group where the bot is present
- Index documents, media, apps, archives, and other authorized files from owner-approved source channels
- Smart search with typo suggestions, file-type filters, paginated results, and private user-bound delivery buttons that expire after ten minutes
- Optional multi-channel force-subscription check, configurable missing-file requests, top-search analytics, broadcast, time-based multi-shortener verification, and an owner-only control panel
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

## Force subscription

The owner can require users to join multiple channels before private delivery. Add a channel with `/addfsub CHAT_ID | JOIN_LINK`; the link is optional for a public channel. Remove a channel with `/removefsub CHAT_ID`, or list them with `/fsub`. The bot checks membership in every configured channel and shows join buttons when links are available.

## File management

Use `/recentfiles` or the **Recent files** control-panel button to get file IDs. Then manage indexed search metadata without reposting the file:

- `/renamefile FILE_ID | New title` updates the searchable display title.
- `/addtags FILE_ID | tag1 tag2` adds search tags such as language, quality, or topic.
- `/removefile FILE_ID` removes a stale entry from the searchable index; it does not delete the original Telegram channel post.

## Owner upload inbox

Set `STORAGE_CHANNEL_ID` to an owner-controlled channel where the bot is an administrator. Then send a supported file to the bot in a private chat as the configured owner. The bot copies the file into that channel and indexes it immediately, so it is searchable in groups without manually posting it to a source channel.

## New-content notifications

Set `UPDATES_CHANNEL_ID` to a channel where the bot can post. Each newly indexed file then creates one formatted announcement with its title, type, optional caption/tags, and a button that opens the private delivery flow. Existing files are not announced again when their index record is updated.

## Inline search

Enable inline mode for the bot in BotFather using `/setinline`. After that, people can search from any Telegram chat with `@YourBotUsername title`. Each result uses a deep link that opens the bot privately; the user then completes the usual subscription check and receives a personal ten-minute delivery button.

## Languages

Users can run `/language` to choose English, Hindi, or Bengali. AkMovieVerse stores the choice for each user and uses it for onboarding, missing-result notices, subscription prompts, and private-delivery controls. New user-facing features can reuse the same translation layer.

## Scheduled broadcasts

Send a broadcast immediately with `/broadcast Your message`. To schedule one, use `/schedule YYYY-MM-DD HH:MM | Your message`; the time uses `TIMEZONE` (defaults to `Asia/Kolkata`). Use `/schedules` to list pending messages and `/cancelschedule SCHEDULE_ID` to cancel one. Scheduled sends are recorded in the owner control panel.

Group-admin commands: `/settings`, `/setwelcome`, `/clearwelcome`, `/setrules`, `/rules`, `/filter`, `/stopfilter`, `/blacklist`, `/unblacklist`, `/antispam on|off`, and `/disable` or `/enable`.

## Shortlink verification

The verification system is disabled by default. It is built around a public callback URL, so deploy this repository twice on Koyeb: keep the bot as a **Worker** with `python -m src.main`, then create a **Web** service from the same repository using `uvicorn src.web:app --host 0.0.0.0 --port $PORT`. Copy the Web service's default `https://…koyeb.app` address into `VERIFY_BASE_URL` for both services.

Add one or more compatible redirect providers with `/addshortener Name | https://provider.example/?url={url} | 09:00-23:00`. The `{url}` placeholder is replaced with a secure one-time AkMovieVerse callback; the time window is optional and uses `TIMEZONE`. Use `/shorteners` to inspect providers and `/shortener Name | on` or `/shortener Name | off` to change availability. AkMovieVerse picks an enabled provider in an active window and rotates by least-recent use.

Enable the gate with `/verification on | 720`, where the final number is the verified-access period in minutes (5–43,200). Use `/verification off` to remove the gate. Users can run `/verify` if they need a fresh verification link. This generic redirect-template adapter supports many simple shorteners; providers that only expose a proprietary API will need their own adapter and API key, which should be stored as a Koyeb environment secret rather than sent in Telegram.

## Premium membership

Premium is manual and does not use Telegram Stars or a payment provider. The owner can grant it with `/addpremium USER_ID | 30d` (or an hourly duration such as `12h`); extending an active plan adds time to its existing expiry. Use `/removepremium USER_ID` to revoke it and `/premiumstats` to see the active count. Users check their plan with `/myplan`.

Premium users automatically bypass shortlink verification. This gives a useful premium benefit now while leaving payment methods, referral rewards, and redeem codes for a later phase.

## Delivery safety controls

The owner can control private-file safety globally. Use `/autodelete SECONDS` to delete new search-result messages and files delivered by the bot after a chosen delay; use `0` to disable it (maximum `604800`, or seven days). Use `/protection on` to ask Telegram to prevent forwarding and saving of newly delivered private files, or `/protection off` to disable that restriction. Check the current values with `/deliverysettings`.

Telegram applies these controls to messages sent by the bot; it cannot delete copies or downloads a user made before the timer runs out.

## Index cleanup

These owner-only commands clean the searchable MongoDB index while preserving original Telegram posts: `/deletefiles FILE_ID FILE_ID` removes multiple entries, `/deletebyname text` removes entries whose titles match text, and `/deleteolder DAYS` removes older records. `/clearindex CONFIRM` clears the entire searchable index and deliberately requires the confirmation word.

Use `/autocleanup on` to skip new posts whose names or captions look like PreDVD, CamRip, or HDCam releases; `/autocleanup off` turns this rule off. Set `DELETION_LOG_CHANNEL_ID` to receive an audit message for bulk cleanup actions and skipped releases.

## Automatic group reactions

The owner can enable a small reaction on new non-command text messages in every group with `/autoreaction on`; turn it off with `/autoreaction off`. Set the standard Telegram emoji using `/reactionemoji 👍` and check the setting with `/reactionstatus`. The bot must have permission to react in the group, and Telegram may limit reactions to the emojis allowed by that group.

## Maintenance mode

The owner can temporarily pause public search, inline results, verification links, and private file delivery with `/maintenance on | Optional notice`. Owner commands continue to work, so you can update source channels and settings while the pause is active. Run `/maintenance off` to restore normal use, or `/maintenance` to inspect the current notice and state.

## Private-chat search

Users can search the shared library by sending a title or filename directly to AkMovieVerse in a private chat. This is enabled by default. The owner can use `/pmsearch off` to make searches group-only, `/pmsearch on` to restore it, or `/pmsearch` to view the current state. Private results use the same protected delivery, subscription, verification, and premium rules as group results.

## Refer & Earn Premium

The owner can enable referrals with `/referral on` and choose the reward with `/referral 3d` or another premium duration such as `12h`. Users run `/refer` to receive a personal deep link. A referral stays pending after a genuine new user starts through that link; the referrer receives the premium reward and count only after the new user has joined every channel configured with `/addfsub`. The bot completes this automatically when it receives the channel-membership update, or on the referred user's next `/start`. Use `/referral off` to pause the program. Existing users, self-referrals, banned users, and the owner do not receive a reward.

## Premium redeem codes

Create limited-use premium codes with `/createcode MOVIE30 | 30d | 100`, where the final number is the maximum number of users who can redeem it. Users claim a code with `/redeem MOVIE30`; a user can claim each code only once, and successful redemptions extend an existing premium plan. The owner can inspect codes with `/codes` and remove one with `/deletecode MOVIE30`.

## Group settings panel

Group administrators can open `/settings` to toggle group search and anti-spam using buttons, view the current rules, and see the relevant management commands. This panel only changes settings for the current group; it never changes the shared file library.

## Request fulfilment

Missing-file requests are grouped by search term and sent to the owner. When you add a likely matching authorized file to a source channel, the bot automatically marks the request fulfilled and privately notifies every requester with a ten-minute delivery button.

The owner can turn this system on or off with `/requests on` or `/requests off`, or from the owner control panel. When it is off, the bot does not show request buttons or match new files to open requests.

Only index and distribute content you own or are authorized to share.
