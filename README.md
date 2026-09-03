# 🎬 AkMovieVerse

> A centrally managed Telegram auto-filter bot for searchable movie, series, books, apps, tools, and any other authorized files.

Anyone can add AkMovieVerse to a group and search one shared library. Only the owner manages source channels, premium, safety, monetization, and global settings.

## What it does

| Search & delivery | Owner controls | Premium & growth |
| --- | --- | --- |
| Unified group and private-chat search | Source-channel indexing | Manual premium plans |
| Smart suggestions, filters, and pagination | File cleanup and audit logs | Refer & Earn Premium |
| Inline mode and private delivery links | Force subscription and requests | Redeem codes |
| Protected forwarding and timed deletion | Maintenance, ads, captions, TMDB | Premium = no shortlinks / no ads |

## Start in 5 minutes

1. Create a bot with [@BotFather](https://t.me/BotFather).
2. Copy `.env.example` to `.env`, then set `BOT_TOKEN`, `OWNER_ID`, and MongoDB values.
3. Run locally with `docker compose up -d`, or deploy the worker to Koyeb with `python -m src.main`.
4. Add the bot as an administrator in each source channel.
5. In your private owner chat, run `/addsource -1001234567890`.
6. Post authorized files to a source channel. They become searchable in every group where the bot is added.

## Main features

- Search from groups, private chat, or Telegram inline mode
- Index documents, videos, audio, photos, archives, apps, and more
- Filter results by video, books, tools, audio, or other files
- Private user-bound delivery buttons with ten-minute expiry
- Multi-channel force subscription and missing-file requests
- Auto-delete, forward protection, custom captions, and group moderation
- Private premium, referral rewards, shortlink verification, and sponsor ads
- Premium perks: 24-hour delivery links, 20 results per page, no shortlinks, and no ads
- Optional TMDB movie/series metadata with poster and rating

## User commands

| Command | Use |
| --- | --- |
| `/start` | Start the bot or open a private delivery link |
| `/id` | Show your Telegram ID and current chat ID |
| `/language` | Choose English, Hindi, Bengali, Tamil, Telugu, Malayalam, Kannada, Marathi, or Urdu |
| `/verify` | Get a shortlink-verification link when required |
| `/myplan` | Check premium status and expiry |
| `/alert title` | Save a search and receive matching-file alerts |
| `/alerts` · `/stopalert ID` | List or remove saved searches |
| `/refer` | Get your referral link when Refer & Earn is enabled |
| `/redeem CODE` | Redeem a premium code |

Send a title or filename in a group, or directly in DM when private search is enabled.

## Owner command center

### Library & source channels

| Command | Use |
| --- | --- |
| `/panel` or `/stats` | Open owner dashboard and statistics |
| `/analytics` | View global search analytics (owner) |
| `/addsource CHAT_ID` | Add a source channel |
| `/removesource CHAT_ID` | Remove a source channel |
| `/recentfiles` | Show recent indexed file titles |
| `/renamefile Search title | Title` | Search, tap, then rename a file |
| `/addtags Search title | tag1 tag2` | Search, tap, then add tags |
| `/removefile Search title` | Search, tap, then remove one record |
| `/deletebyname text` | Remove records matching a title |
| `/deleteolder DAYS` | Remove old records |
| `/clearindex CONFIRM` | Clear the searchable index only |
| `/autocleanup on|off` | Skip new CamRip / PreDVD / HDCam records |

### Members, premium & referrals

| Command | Use |
| --- | --- |
| `/ban USER_ID` · `/unban USER_ID` | Control access |
| `/userinfo USER_ID` | View language, premium, verification, and referral status |
| `/verifiedstats` | Show active and all-time verified-user counts |
| `/unverify USER_ID` | Revoke a user’s active verification |
| `/addpremium USER_ID | 30d` | Grant or extend premium |
| `/removepremium USER_ID` | Revoke premium |
| `/premiumstats` | Count active premium users |
| `/referral on|off` | Enable or pause Refer & Earn |
| `/referral 3d` | Set referral reward duration |
| `/createcode CODE | 30d | 100` | Create a limited-use premium code |
| `/codes` · `/deletecode CODE` | Review or delete redeem codes |

### Search, delivery & safety

| Command | Use |
| --- | --- |
| `/addfsub CHAT_ID | JOIN_LINK` | Add a required subscription channel |
| `/removefsub CHAT_ID` · `/fsub` | Manage or list required channels |
| `/requests on|off` | Toggle missing-file requests |
| `/pmsearch on|off` | Allow or disable DM searching |
| `/autodelete SECONDS` | Delete bot search/delivery messages after a delay |
| `/protection on|off` | Restrict forwarding/saving on new deliveries |
| `/deliverysettings` | View delivery safety settings |
| `/freelimit MB` | Set the maximum file size free users can receive (`0` disables it) |
| `/freequality 1080p 2160p` | Restrict selected qualities to premium users |
| `/freerules` | View active free-user access rules |
| `/setcaption template` | Set custom media caption using `{file_name}` and `{original_caption}` |
| `/caption` · `/clearcaption` | View or remove custom caption |
| `/maintenance on | Notice` | Pause public search and delivery |
| `/maintenance off` | Restore normal use |

### Monetization & metadata

| Command | Use |
| --- | --- |
| `/setad Text | URL | Button` | Save sponsor ad for free users |
| `/ads on|off` | Show or pause ads |
| `/adstatus` · `/clearad` | Review or remove the ad |
| `/tmdb Title` | Fetch TMDB poster, year, rating, and overview |
| `/autometa on\|off` | Automatically enrich new video files with TMDB data |
| `/setposttemplate movie\|series | template` | Save a branded movie or series update layout |
| `/posttemplate on\|off` | Enable or disable custom update layouts |
| `/clearposttemplate movie\|series` | Remove one saved update layout |
| Send a photo with `/setposter Title` as caption | Search, tap, and set a custom portrait/landscape poster |
| `/clearposter Title` | Search, tap, and remove a custom poster |

### Messaging & activity

| Command | Use |
| --- | --- |
| `/broadcast message` | Broadcast to users |
| `/schedule YYYY-MM-DD HH:MM | message` | Schedule a broadcast in `TIMEZONE` |
| `/schedules` · `/cancelschedule ID` | Manage scheduled broadcasts |
| `/autoreaction on|off` | Add automatic reactions to accepted group messages |
| `/reactionemoji 👍` · `/reactionstatus` | Set or view the reaction emoji |

## Group-admin commands

| Command | Use |
| --- | --- |
| `/settings` | Open group settings panel |
| `/analytics` | View search analytics for this group only |
| `/setwelcome text` · `/clearwelcome` | Manage welcome message |
| `/setrules text` · `/rules` | Manage group rules |
| `/filter keyword | reply` · `/stopfilter keyword` | Add/remove keyword replies |
| `/blacklist word` · `/unblacklist word` | Manage blocked words |
| `/antispam on|off` | Toggle basic anti-spam |
| `/disable` · `/enable` | Disable or enable search in this group |

## Koyeb deployment

Use two services from the same repository when shortlink verification is enabled:

| Service | Run command | Purpose |
| --- | --- | --- |
| Worker | `python -m src.main` | Telegram bot polling |
| Web | `uvicorn src.web:app --host 0.0.0.0 --port $PORT` | Verification callback |

Set the Web service’s public `https://…koyeb.app` address as `VERIFY_BASE_URL` in both services. Use an external MongoDB deployment such as MongoDB Atlas.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `BOT_TOKEN` | Yes | BotFather token |
| `OWNER_ID` | Yes | Numeric Telegram owner ID |
| `MONGODB_URI` | Yes | MongoDB connection string |
| `MONGODB_DATABASE` | Yes | Database name |
| `STORAGE_CHANNEL_ID` | No | Owner private-upload storage channel |
| `UPDATES_CHANNEL_ID` | No | New-file update channel |
| `DELETION_LOG_CHANNEL_ID` | No | Index-cleanup audit channel |
| `VERIFY_BASE_URL` | No | Public Koyeb Web callback URL |
| `TMDB_READ_ACCESS_TOKEN` | No | Recommended TMDB credential |
| `TMDB_API_KEY` | No | Alternative TMDB v3 credential |

## Notes

- Configure TMDB credentials in Koyeb before using `/tmdb` or `/autometa on`. Automatic TMDB metadata applies only to newly indexed video files and creates rich update-channel posts when a match is found. Set movie and series post templates with `{title}`, `{year}`, `{rating}`, `{type}`, `{file_name}`, `{caption}`, `{overview}`, and `{tags}`; templates remain inactive until `/posttemplate on`. [TMDB authentication guide](https://developer.themoviedb.org/docs/authentication-application)
- To override a poster for one file, send the image to the bot in private chat with `/setposter Movie Name` as its caption. Choose the matching title and portrait or landscape; no internal file ID is needed.
- Enable inline mode in BotFather with `/setinline` to use `@YourBot title` anywhere.
- Free users can save up to 3 search alerts; premium users can save up to 20.
- Free-user file-size rules apply to files indexed after this feature is deployed, because older records may not include Telegram’s file-size value.
- Source and deletion actions affect the searchable MongoDB index unless Telegram deletion is explicitly stated.
- Only index and distribute files that you own or are authorized to share.
