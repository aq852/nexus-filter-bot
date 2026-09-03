import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    ReactionTypeEmoji,
    ChatMemberUpdated,
)
from bson import ObjectId
from pymongo import ReturnDocument

from .config import Settings, get_settings
from .database import Database
from .i18n import LANGUAGES, translate
from .utils import media_category, media_kind, message_file, normalize_query

router = Router()
db: Database
settings: Settings
recent_messages: dict[tuple[int, int], float] = {}
bot_username: str | None = None


def is_owner(user_id: int) -> bool:
    return user_id == settings.owner_id


async def user_language(user_id: int) -> str:
    user = await db.db.users.find_one({"user_id": user_id}, {"language": 1})
    return (user or {}).get("language", "en")


def language_keyboard() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=name, callback_data=f"language:{code}") for code, name in LANGUAGES.items()]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[index:index + 3] for index in range(0, len(buttons), 3)])


def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Live stats", callback_data="panel:stats"),
         InlineKeyboardButton(text="📁 Source channels", callback_data="panel:sources")],
        [InlineKeyboardButton(text="📩 Open requests", callback_data="panel:requests"),
         InlineKeyboardButton(text="🎚 Request system", callback_data="panel:requesttoggle")],
        [InlineKeyboardButton(text="🔐 Force subscription", callback_data="panel:fsub"),
         InlineKeyboardButton(text="🔎 Top searches", callback_data="panel:searches")],
        [InlineKeyboardButton(text="🗂 Recent files", callback_data="panel:files"),
         InlineKeyboardButton(text="📣 Broadcast help", callback_data="panel:broadcast")],
        [InlineKeyboardButton(text="🗓 Schedules", callback_data="panel:schedules")],
        [InlineKeyboardButton(text="🛡 User controls", callback_data="panel:users")],
        [InlineKeyboardButton(text="✕ Close", callback_data="panel:close")],
    ])


async def owner_stats_text() -> str:
    files = await db.db.files.count_documents({})
    users = await db.db.users.count_documents({})
    sources = await db.db.source_channels.count_documents({})
    requests = await db.db.requests.count_documents({"status": "open"})
    request_setting = await db.db.bot_settings.find_one({"_id": "global"}) or {}
    request_state = "on" if request_setting.get("request_system_enabled", True) else "off"
    fsub_count = await db.db.force_sub_channels.count_documents({})
    premium = await db.db.users.count_documents({"premium_until": {"$gt": datetime.now(timezone.utc)}})
    verified = await db.db.users.count_documents({"verified_until": {"$gt": datetime.now(timezone.utc)}})
    return f"<b>AkMovieVerse control panel</b>\n\nFiles: <b>{files}</b>\nUsers: <b>{users}</b>\nPremium users: <b>{premium}</b>\nVerified users: <b>{verified}</b>\nSource channels: <b>{sources}</b>\nForce-sub channels: <b>{fsub_count}</b>\nRequest system: <b>{request_state}</b>\nOpen requests: <b>{requests}</b>"


async def analytics_text(chat_id: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    day_start = now - timedelta(days=1)
    week_start = now - timedelta(days=7)
    scope = {"chat_id": chat_id} if chat_id is not None else {}

    def selector(start: datetime) -> dict:
        return {**scope, "created_at": {"$gte": start}}

    day_searches = await db.db.search_events.count_documents(selector(day_start))
    week_searches = await db.db.search_events.count_documents(selector(week_start))
    day_users = len(await db.db.search_events.distinct("user_id", selector(day_start)))
    week_users = len(await db.db.search_events.distinct("user_id", selector(week_start)))
    top_pipeline = [
        {"$match": selector(week_start)},
        {"$group": {"_id": "$query", "count": {"$sum": 1}}},
        {"$sort": {"count": -1, "_id": 1}},
        {"$limit": 5},
    ]
    top = await db.db.search_events.aggregate(top_pipeline).to_list(length=5)
    top_text = "\n".join(f"• {escape(item['_id'])} — <b>{item['count']}</b>" for item in top) or "No searches recorded yet."
    heading = "This group" if chat_id is not None else "AkMovieVerse global"
    text = (
        f"<b>{heading} analytics</b>\n\n"
        f"Last 24 hours: <b>{day_searches}</b> searches · <b>{day_users}</b> users\n"
        f"Last 7 days: <b>{week_searches}</b> searches · <b>{week_users}</b> users\n\n"
        f"<b>Top searches (7 days)</b>\n{top_text}"
    )
    if chat_id is None:
        groups = await db.db.search_events.aggregate([
            {"$match": {"created_at": {"$gte": week_start}, "chat_id": {"$lt": 0}}},
            {"$group": {"_id": "$chat_id", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 5},
        ]).to_list(length=5)
        group_text = "\n".join(f"• <code>{item['_id']}</code> — <b>{item['count']}</b>" for item in groups) or "No group searches recorded yet."
        text += f"\n\n<b>Top groups (7 days)</b>\n{group_text}"
    return text


async def force_sub_channels() -> list[dict]:
    channels = await db.db.force_sub_channels.find({}).sort("added_at", 1).to_list(length=20)
    if not channels and settings.force_sub_channel_id:
        channels = [{"chat_id": settings.force_sub_channel_id, "title": "Updates channel", "join_url": None}]
    return channels


async def subscription_ok(bot: Bot, user_id: int) -> bool:
    for channel in await force_sub_channels():
        try:
            member = await bot.get_chat_member(channel["chat_id"], user_id)
            if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                return False
        except Exception:
            return False
    return True


async def force_sub_keyboard() -> InlineKeyboardMarkup | None:
    rows = []
    for channel in await force_sub_channels():
        url = channel.get("join_url")
        if url:
            rows.append([InlineKeyboardButton(text=f"Join {channel.get('title') or 'channel'}", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.chat_member()
async def complete_referral_after_channel_join(update: ChatMemberUpdated) -> None:
    """React immediately when an invited user joins a configured force-sub channel."""
    channels = await force_sub_channels()
    if update.chat.id not in {channel["chat_id"] for channel in channels}:
        return
    if update.new_chat_member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        return
    await complete_pending_referral(update.bot, update.new_chat_member.user.id)


async def request_system_enabled() -> bool:
    setting = await db.db.bot_settings.find_one({"_id": "global"})
    return setting.get("request_system_enabled", True) if setting else True


async def delivery_settings() -> dict:
    configured = await db.db.bot_settings.find_one({"_id": "delivery"}) or {}
    return {
        "auto_delete_seconds": configured.get("auto_delete_seconds", settings.auto_delete_seconds),
        "protect_content": configured.get("protect_content", False),
    }


async def cleanup_settings() -> dict:
    configured = await db.db.bot_settings.find_one({"_id": "cleanup"}) or {}
    return {"remove_low_quality": configured.get("remove_low_quality", False)}


async def auto_reaction_settings() -> dict:
    configured = await db.db.bot_settings.find_one({"_id": "auto_reaction"}) or {}
    return {"enabled": configured.get("enabled", False), "emoji": configured.get("emoji", "👍")}


async def maintenance_settings() -> dict:
    configured = await db.db.bot_settings.find_one({"_id": "maintenance"}) or {}
    return {
        "enabled": configured.get("enabled", False),
        "message": configured.get("message", "AkMovieVerse is temporarily under maintenance. Please try again soon."),
    }


async def maintenance_active(user_id: int) -> bool:
    return not is_owner(user_id) and (await maintenance_settings())["enabled"]


async def pm_search_enabled() -> bool:
    configured = await db.db.bot_settings.find_one({"_id": "pm_search"}) or {}
    return configured.get("enabled", True)


async def referral_settings() -> dict:
    configured = await db.db.bot_settings.find_one({"_id": "referral"}) or {}
    return {"enabled": configured.get("enabled", False), "reward_minutes": configured.get("reward_minutes", 1_440)}


async def ad_settings() -> dict:
    configured = await db.db.bot_settings.find_one({"_id": "ad"}) or {}
    return {
        "enabled": configured.get("enabled", False),
        "text": configured.get("text", ""),
        "url": configured.get("url", ""),
        "button_text": configured.get("button_text", "Open sponsor"),
    }


async def caption_template() -> str | None:
    configured = await db.db.bot_settings.find_one({"_id": "caption"}) or {}
    return configured.get("template") or None


async def custom_delivery_caption(file: dict) -> str | None:
    template = await caption_template()
    if not template:
        return None
    return template.format(
        file_name=escape(file.get("name", "file")),
        original_caption=escape(file.get("caption", "")),
    )[:1024]


async def tmdb_lookup(query: str) -> dict | None:
    if not settings.tmdb_api_key and not settings.tmdb_read_access_token:
        return None
    headers = {"Authorization": f"Bearer {settings.tmdb_read_access_token}"} if settings.tmdb_read_access_token else {}
    params = {"query": query, "include_adult": "false", "language": "en-US"}
    if settings.tmdb_api_key:
        params["api_key"] = settings.tmdb_api_key
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://api.themoviedb.org/3/search/multi", params=params, headers=headers)
            response.raise_for_status()
        results = response.json().get("results", [])
        return next((item for item in results if item.get("media_type") in {"movie", "tv"}), None)
    except httpx.HTTPError as error:
        logging.warning("TMDB lookup failed: %s", error)
        return None


def tmdb_caption(item: dict) -> str:
    title = escape(item.get("title") or item.get("name") or "Untitled")
    date = item.get("release_date") or item.get("first_air_date") or "Unknown year"
    year = date[:4] if date else "Unknown year"
    media_type = "Movie" if item.get("media_type") == "movie" else "Series"
    rating = item.get("vote_average")
    rating_text = f"\nRating: <b>{rating:.1f}/10</b>" if isinstance(rating, (int, float)) and rating else ""
    overview = escape(item.get("overview") or "No overview is available.")[:700]
    return f"<b>{title}</b> ({year})\nType: <b>{media_type}</b>{rating_text}\n\n{overview}\n\n<i>Metadata provided by TMDB.</i>"


async def auto_metadata_enabled() -> bool:
    configured = await db.db.bot_settings.find_one({"_id": "auto_metadata"}) or {}
    return configured.get("enabled", False)


async def update_template_settings() -> dict:
    configured = await db.db.bot_settings.find_one({"_id": "update_template"}) or {}
    return {
        "enabled": configured.get("enabled", False),
        "movie_template": configured.get("movie_template"),
        "series_template": configured.get("series_template"),
    }


async def custom_update_text(record: dict) -> str | None:
    metadata = record.get("tmdb") or {}
    content_type = "movie" if metadata.get("type") == "movie" else "series" if metadata.get("type") == "tv" else None
    if not content_type:
        return None
    config = await update_template_settings()
    template = config.get(f"{content_type}_template")
    if not config["enabled"] or not template:
        return None
    values = {
        "title": escape(metadata.get("title") or record["name"]),
        "year": escape(str(metadata.get("year") or "—")),
        "rating": escape(f"{metadata['rating']:.1f}/10") if isinstance(metadata.get("rating"), (int, float)) and metadata.get("rating") else "—",
        "type": "Movie" if content_type == "movie" else "Series",
        "file_name": escape(record["name"]),
        "caption": escape(record.get("caption") or ""),
        "tags": " ".join(f"#{escape(tag)}" for tag in record.get("tags", [])[:8]),
        "overview": escape(metadata.get("overview") or ""),
    }
    try:
        return template.format(**values).strip()[:1024]
    except (KeyError, ValueError):
        return None


def tmdb_query_from_filename(name: str) -> str:
    title = re.sub(r"\[[^]]*\]|\([^)]*\)", " ", name)
    title = re.sub(r"\.(mkv|mp4|avi|webm|mov)$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(480p|720p|1080p|1440p|2160p|4k|webrip|web-dl|bluray|brrip|hdrip|dvdrip|x264|x265|hevc|aac|ddp|atmos|hindi|english|telugu|tamil|dubbed)\b", " ", title, flags=re.IGNORECASE)
    return normalize_query(title)


async def enrich_video_metadata(record: dict) -> None:
    if record.get("category") != "video" or not await auto_metadata_enabled():
        return
    query = tmdb_query_from_filename(record["name"])
    if len(query) < 2:
        return
    item = await tmdb_lookup(query)
    if not item:
        return
    date = item.get("release_date") or item.get("first_air_date") or ""
    media_type = item["media_type"]
    metadata = {
        "id": item["id"],
        "type": media_type,
        "title": item.get("title") or item.get("name") or record["name"],
        "year": date[:4] if date else None,
        "rating": item.get("vote_average"),
        "overview": item.get("overview") or "",
        "poster_url": f"https://image.tmdb.org/t/p/w500{item['poster_path']}" if item.get("poster_path") else None,
        "details_url": f"https://www.themoviedb.org/{media_type}/{item['id']}",
    }
    record["tmdb"] = metadata
    record["search_text"] = normalize_query(f"{record['search_text']} {metadata['title']} {metadata.get('year') or ''}")


def saved_alert_matches(alert_query: str, searchable_text: str) -> bool:
    query = normalize_query(alert_query).lower()
    haystack = normalize_query(searchable_text).lower()
    words = [word for word in query.split() if len(word) > 1]
    return bool(query and (query in haystack or (words and all(word in haystack for word in words))))


async def notify_saved_alerts(bot: Bot, record: dict, file_id: str) -> None:
    """Notify subscribers when a newly indexed file matches their saved search."""
    async for alert in db.db.search_alerts.find({}):
        if not saved_alert_matches(alert["query"], record["search_text"]):
            continue
        token = await db.make_download_token(file_id, alert["user_id"], await delivery_token_minutes(alert["user_id"]))
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=await delivery_button_text(alert["user_id"]), callback_data=f"send:{token}")
        ]])
        try:
            await bot.send_message(
                alert["user_id"],
                f"🔔 <b>Saved search match</b>\n\n<b>{escape(record['name'])}</b> matches your alert: <i>{escape(alert['query'])}</i>",
                reply_markup=keyboard,
            )
            await db.db.search_alerts.update_one({"_id": alert["_id"]}, {"$set": {"last_notified_at": datetime.now(timezone.utc), "last_file_id": file_id}})
        except Exception:
            continue


async def add_ad_to_results(keyboard: InlineKeyboardMarkup | None, user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    if await premium_until(user_id):
        return "", keyboard
    ad = await ad_settings()
    if not ad["enabled"] or not ad["text"] or not ad["url"]:
        return "", keyboard
    rows = list(keyboard.inline_keyboard) if keyboard else []
    rows.append([InlineKeyboardButton(text=ad["button_text"], url=ad["url"])])
    return f"\n\n<i>{escape(ad['text'])}</i>", InlineKeyboardMarkup(inline_keyboard=rows)


async def award_referral(bot: Bot, inviter_id: int, new_user_id: int) -> datetime | None:
    config = await referral_settings()
    if not config["enabled"] or inviter_id == new_user_id or inviter_id == settings.owner_id:
        return None
    inviter = await db.db.users.find_one({"user_id": inviter_id})
    if not inviter or inviter.get("banned"):
        return None
    current = inviter.get("premium_until")
    starts_at = current if current and current > datetime.now(timezone.utc) else datetime.now(timezone.utc)
    until = starts_at + timedelta(minutes=int(config["reward_minutes"]))
    await db.db.users.update_one(
        {"user_id": inviter_id},
        {"$set": {"premium_until": until, "premium_granted_at": datetime.now(timezone.utc)}, "$inc": {"referral_count": 1}},
    )
    try:
        await bot.send_message(inviter_id, f"🎉 Your referral joined AkMovieVerse. You earned premium until <b>{until.astimezone(ZoneInfo(settings.timezone)):%d %b %Y, %I:%M %p}</b>.")
    except Exception:
        pass
    return until


async def complete_pending_referral(bot: Bot, referred_user_id: int) -> bool:
    """Award one pending referral only after the invited user joined all force-sub channels."""
    config = await referral_settings()
    channels = await force_sub_channels()
    if not config["enabled"] or not channels or not await subscription_ok(bot, referred_user_id):
        return False
    user = await db.db.users.find_one_and_update(
        {"user_id": referred_user_id, "referral_status": "pending"},
        {"$set": {"referral_status": "completed", "referral_completed_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.BEFORE,
    )
    if not user:
        return False
    return bool(await award_referral(bot, user["referred_by"], referred_user_id))


async def react_to_group_message(message: Message) -> None:
    config = await auto_reaction_settings()
    if not config["enabled"]:
        return
    try:
        await message.bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=config["emoji"])],
        )
    except Exception as error:
        logging.debug("Could not add automatic reaction: %s", error)


async def log_index_cleanup(bot: Bot, text: str) -> None:
    if not settings.deletion_log_channel_id:
        return
    try:
        await bot.send_message(settings.deletion_log_channel_id, f"<b>AkMovieVerse index cleanup</b>\n\n{text}")
    except Exception as error:
        logging.warning("Could not post cleanup log: %s", error)


def low_quality_release(record: dict) -> bool:
    text = f"{record.get('name', '')} {record.get('caption', '')}".lower()
    return any(term in text for term in ("predvd", "pre-dvd", "camrip", "cam rip", "hdcam"))


async def verification_settings() -> dict:
    return await db.db.bot_settings.find_one({"_id": "verification"}) or {"enabled": False, "valid_minutes": 720}


async def premium_until(user_id: int) -> datetime | None:
    user = await db.db.users.find_one({"user_id": user_id}, {"premium_until": 1}) or {}
    until = user.get("premium_until")
    return until if until and until > datetime.now(timezone.utc) else None


async def delivery_token_minutes(user_id: int) -> int:
    return 1_440 if await premium_until(user_id) else 10


async def results_per_page_for(user_id: int) -> int:
    return 20 if await premium_until(user_id) else settings.results_per_page


async def delivery_button_text(user_id: int, language: str | None = None) -> str:
    minutes = await delivery_token_minutes(user_id)
    if minutes >= 60:
        return "📥 Send to my DM (24h)"
    return translate(language or await user_language(user_id), "send_to_dm")


async def delivery_ready_text(user_id: int, language: str | None = None) -> str:
    if await delivery_token_minutes(user_id) >= 60:
        return "Your private delivery link is ready for 24 hours."
    return translate(language or await user_language(user_id), "delivery_ready")


async def verification_ok(user_id: int) -> bool:
    if await premium_until(user_id):
        return True
    config = await verification_settings()
    if not config.get("enabled", False):
        return True
    user = await db.db.users.find_one({"user_id": user_id}, {"verified_until": 1}) or {}
    until = user.get("verified_until")
    return bool(until and until > datetime.now(timezone.utc))


async def active_shortener() -> dict | None:
    """Choose an enabled provider, respecting optional daily local-time windows."""
    now = datetime.now(ZoneInfo(settings.timezone)).strftime("%H:%M")
    providers = await db.db.shorteners.find({"enabled": True}).sort([("priority", 1), ("last_used_at", 1)]).to_list(length=50)
    for provider in providers:
        start, end = provider.get("window_start"), provider.get("window_end")
        if not start or not end or (start <= end and start <= now <= end) or (start > end and (now >= start or now <= end)):
            return provider
    return None


async def verification_keyboard(user_id: int) -> InlineKeyboardMarkup | None:
    if not settings.verify_base_url:
        return None
    provider = await active_shortener()
    if not provider:
        return None
    config = await verification_settings()
    token = await db.create_verification_token(user_id, provider["name"], int(config.get("valid_minutes", 720)))
    callback_url = f"{settings.verify_base_url.rstrip('/')}/verify/{token}"
    try:
        url = provider["url_template"].replace("{url}", quote(callback_url, safe=""))
    except Exception:
        return None
    await db.db.shorteners.update_one({"_id": provider["_id"]}, {"$set": {"last_used_at": datetime.now(timezone.utc)}})
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"Verify via {provider['name']}", url=url)
    ]])


async def require_verification(message: Message | CallbackQuery) -> bool:
    user_id = message.from_user.id
    if await verification_ok(user_id):
        return True
    keyboard = await verification_keyboard(user_id)
    text = "Verification is required before file delivery. Complete it once to unlock access for the owner-selected time."
    if keyboard:
        await message.message.answer(text, reply_markup=keyboard) if isinstance(message, CallbackQuery) else await message.answer(text, reply_markup=keyboard)
    else:
        await message.message.answer("Verification is enabled, but no active shortener is configured. Please contact the owner.") if isinstance(message, CallbackQuery) else await message.answer("Verification is enabled, but no active shortener is configured. Please contact the owner.")
    return False


async def is_group_admin(message: Message) -> bool:
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return False
    if is_owner(message.from_user.id):
        return True
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    except Exception:
        return False


async def require_group_admin(message: Message) -> bool:
    if await is_group_admin(message):
        return True
    await message.reply("Only this group's administrators can use that command.")
    return False


async def callback_is_group_admin(callback: CallbackQuery) -> bool:
    if is_owner(callback.from_user.id):
        return True
    try:
        member = await callback.bot.get_chat_member(callback.message.chat.id, callback.from_user.id)
        return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    except Exception:
        return False


def group_settings_keyboard(group: dict) -> InlineKeyboardMarkup:
    search_label = "✅ Search enabled" if not group.get("disabled") else "⛔ Search disabled"
    spam_label = "🛡 Anti-spam on" if group.get("anti_spam") else "🛡 Anti-spam off"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=search_label, callback_data="groupset:search"),
         InlineKeyboardButton(text=spam_label, callback_data="groupset:spam")],
        [InlineKeyboardButton(text="📜 View rules", callback_data="groupset:rules"),
         InlineKeyboardButton(text="ℹ️ Help", callback_data="groupset:help")],
        [InlineKeyboardButton(text="✕ Close", callback_data="groupset:close")],
    ])


def group_settings_text(group: dict) -> str:
    return (
        "<b>Group settings</b>\n\n"
        f"Search: <b>{'disabled' if group.get('disabled') else 'enabled'}</b>\n"
        f"Anti-spam: <b>{'on' if group.get('anti_spam') else 'off'}</b>\n"
        "\nOnly group administrators can change these settings."
    )


FILTERS = (("🎬 Video", "video"), ("📚 Books", "book"), ("🛠 Tools", "tool"), ("🎵 Audio", "audio"), ("📄 Other", "file"))


def result_keyboard(files: list[dict], session_id: str, page: int, total: int, page_size: int, category: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{item['kind']} · {item['name'][:46]}", callback_data=f"file:{item['_id']}")]
        for item in files
    ]
    rows.append([InlineKeyboardButton(text=label, callback_data=f"filter:{session_id}:{value}") for label, value in FILTERS])
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton(text="‹ Prev", callback_data=f"page:{session_id}:{category or 'all'}:{page - 1}"))
    if (page + 1) * page_size < total:
        navigation.append(InlineKeyboardButton(text="Next ›", callback_data=f"page:{session_id}:{category or 'all'}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def delete_message_later(bot: Bot, chat_id: int, message_id: int) -> None:
    seconds = (await delivery_settings())["auto_delete_seconds"]
    if seconds:
        await asyncio.sleep(seconds)
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass


async def delete_later(message: Message) -> None:
    await delete_message_later(message.bot, message.chat.id, message.message_id)


async def announce_new_file(bot: Bot, record: dict, file_id: str) -> None:
    """Publish a compact update only when a file is first added to the index."""
    if not settings.updates_channel_id:
        return
    tags = " ".join(f"#{tag}" for tag in record.get("tags", [])[:8])
    caption_preview = escape(record.get("caption", "").strip()[:250])
    metadata = record.get("tmdb") or {}
    template_text = await custom_update_text(record)
    if template_text:
        text = template_text
    elif metadata:
        rating = metadata.get("rating")
        rating_text = f"\nRating: <b>{rating:.1f}/10</b>" if isinstance(rating, (int, float)) and rating else ""
        text = f"<b>New in AkMovieVerse</b>\n\n<b>{escape(metadata['title'])}</b> ({metadata.get('year') or '—'})\nType: <b>{metadata.get('type', 'video').title()}</b>{rating_text}\n\n{escape(metadata.get('overview') or record['name'])[:500]}"
    else:
        text = f"<b>New in AkMovieVerse</b>\n\n{record['kind']} <b>{escape(record['name'])}</b>"
    if caption_preview and not template_text:
        text += f"\n\n{caption_preview}"
    if tags:
        text += f"\n\n{tags}"
    keyboard = None
    if bot_username:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔎 Open in AkMovieVerse", url=f"https://t.me/{bot_username}?start=file_{file_id}")
        ]])
    try:
        if metadata.get("poster_url"):
            await bot.send_photo(settings.updates_channel_id, metadata["poster_url"], caption=text[:1024], reply_markup=keyboard)
        else:
            await bot.send_message(settings.updates_channel_id, text, reply_markup=keyboard)
    except Exception as error:
        if metadata.get("poster_url"):
            try:
                await bot.send_message(settings.updates_channel_id, text, reply_markup=keyboard)
                return
            except Exception:
                pass
        logging.warning("Could not publish new-file announcement: %s", error)


async def render_results(message: Message, user_id: int, query: str, page: int, session_id: str | None = None, category: str | None = None) -> None:
    clean_query = normalize_query(query)
    language = await user_language(user_id)
    await db.record_search(clean_query, user_id, message.chat.id)
    page_size = await results_per_page_for(user_id)
    files, total = await db.search(clean_query, page, page_size, category)
    if not session_id:
        session_id = await db.save_search_session(user_id, message.chat.id, clean_query)
    if not files:
        keyboard = None
        if await request_system_enabled():
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📩 Request this", callback_data=f"request:{session_id}")
            ]])
        ad_text, keyboard = await add_ad_to_results(keyboard, user_id)
        suggestions = await db.suggestions(clean_query)
        hint = "\n<b>Did you mean:</b> " + " • ".join(suggestions) if suggestions else ""
        sent = await message.answer(f"{translate(language, 'no_results', query=clean_query)}{hint}{ad_text}", reply_markup=keyboard)
        asyncio.create_task(delete_later(sent))
        return
    first = page * page_size + 1
    last = first + len(files) - 1
    filter_label = f" · {category.title()}" if category else ""
    ad_text, keyboard = await add_ad_to_results(result_keyboard(files, session_id, page, total, page_size, category), user_id)
    sent = await message.answer(
        f"<b>Results for:</b> {clean_query}{filter_label}\nShowing {first}–{last} of {total}.\n\nChoose a type to narrow results, or tap a result for private delivery.{ad_text}",
        reply_markup=keyboard,
    )
    asyncio.create_task(delete_later(sent))


@router.message(Command("start"))
async def start(message: Message, command: CommandObject) -> None:
    payload = command.args or ""
    user_update = await db.db.users.update_one(
        {"user_id": message.from_user.id},
        {"$set": {"user_id": message.from_user.id, "name": message.from_user.full_name, "last_seen": datetime.now(timezone.utc)}},
        upsert=True,
    )
    if user_update.upserted_id and payload.startswith("ref_") and (await referral_settings())["enabled"]:
        try:
            inviter_id = int(payload.removeprefix("ref_"))
        except ValueError:
            inviter_id = 0
        if inviter_id and inviter_id != message.from_user.id:
            await db.db.users.update_one(
                {"user_id": message.from_user.id},
                {"$set": {"referred_by": inviter_id, "referral_status": "pending", "referral_created_at": datetime.now(timezone.utc)}},
            )
    await complete_pending_referral(message.bot, message.from_user.id)
    if await maintenance_active(message.from_user.id):
        await message.answer((await maintenance_settings())["message"])
        return
    language = await user_language(message.from_user.id)
    if payload.startswith("file_"):
        try:
            file = await db.db.files.find_one({"_id": ObjectId(payload.removeprefix("file_"))})
        except Exception:
            file = None
        if not file:
            await message.answer("That inline result is no longer available. Please search again.")
            return
        if not await subscription_ok(message.bot, message.from_user.id):
            await message.answer(translate(language, "join_required"), reply_markup=await force_sub_keyboard())
            return
        if not await require_verification(message):
            return
        token = await db.make_download_token(str(file["_id"]), message.from_user.id, await delivery_token_minutes(message.from_user.id))
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=await delivery_button_text(message.from_user.id, language), callback_data=f"send:{token}")
        ]])
        await message.answer(f"<b>{file['name']}</b>\nYour private delivery button is ready.", reply_markup=keyboard)
        return
    await message.answer(translate(language, "welcome"))
    popular = await db.top_searches(5)
    if popular:
        await message.answer("<b>Popular searches</b>\n" + "\n".join(f"• {item['query']}" for item in popular))


@router.message(Command("language"))
async def language_selector(message: Message) -> None:
    language = await user_language(message.from_user.id)
    await message.answer(translate(language, "choose_language"), reply_markup=language_keyboard())


@router.message(Command("verify"))
async def verify_access(message: Message) -> None:
    if await maintenance_active(message.from_user.id):
        await message.answer((await maintenance_settings())["message"])
        return
    if await premium_until(message.from_user.id):
        await message.answer("You have premium access, so shortlink verification is not required.")
        return
    if await verification_ok(message.from_user.id):
        await message.answer("Your access is already verified.")
        return
    keyboard = await verification_keyboard(message.from_user.id)
    if not keyboard:
        await message.answer("Verification is unavailable right now. Please contact the owner.")
        return
    await message.answer("Complete verification, then return here. Your verified access lasts for the owner-selected time.", reply_markup=keyboard)


@router.message(Command("autodelete"))
async def set_auto_delete(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        seconds = int(command.args or "")
        if not 0 <= seconds <= 604_800:
            raise ValueError
    except ValueError:
        await message.answer("Usage: <code>/autodelete SECONDS</code>\nUse <code>0</code> to disable, up to 604800 (7 days).")
        return
    await db.db.bot_settings.update_one({"_id": "delivery"}, {"$set": {"auto_delete_seconds": seconds}}, upsert=True)
    if seconds:
        await message.answer(f"✅ Search results and delivered files will auto-delete after <b>{seconds} seconds</b> when Telegram permits it.")
    else:
        await message.answer("✅ Auto-delete is disabled.")


@router.message(Command("protection"))
async def set_forward_protection(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    choice = (command.args or "").lower().strip()
    if choice not in {"on", "off"}:
        await message.answer("Usage: <code>/protection on</code> or <code>/protection off</code>")
        return
    await db.db.bot_settings.update_one({"_id": "delivery"}, {"$set": {"protect_content": choice == "on"}}, upsert=True)
    await message.answer(f"✅ Forward protection is <b>{choice}</b> for newly delivered files.")


@router.message(Command("deliverysettings"))
async def show_delivery_settings(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    current = await delivery_settings()
    await message.answer(
        "<b>Delivery safety settings</b>\n\n"
        f"Auto-delete: <b>{current['auto_delete_seconds']} seconds</b>\n"
        f"Forward protection: <b>{'on' if current['protect_content'] else 'off'}</b>\n\n"
        "Set: <code>/autodelete SECONDS</code>\nProtect: <code>/protection on</code>"
    )


@router.message(Command("autoreaction"))
async def set_auto_reaction(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    choice = (command.args or "").strip().lower()
    if choice not in {"on", "off"}:
        await message.answer("Usage: <code>/autoreaction on</code> or <code>/autoreaction off</code>")
        return
    await db.db.bot_settings.update_one({"_id": "auto_reaction"}, {"$set": {"enabled": choice == "on"}}, upsert=True)
    current = await auto_reaction_settings()
    emoji_hint = f" ({current['emoji']})" if choice == "on" else ""
    await message.answer(f"✅ Auto reaction is <b>{choice}</b>{emoji_hint} for new group text messages.")


@router.message(Command("reactionemoji"))
async def set_reaction_emoji(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    emoji = (command.args or "").strip()
    if not emoji or len(emoji) > 8:
        await message.answer("Usage: <code>/reactionemoji 👍</code>\nUse one standard Telegram emoji.")
        return
    await db.db.bot_settings.update_one({"_id": "auto_reaction"}, {"$set": {"emoji": emoji}}, upsert=True)
    await message.answer(f"✅ Auto-reaction emoji set to {emoji}. Use <code>/autoreaction on</code> to enable it.")


@router.message(Command("reactionstatus"))
async def auto_reaction_status(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    current = await auto_reaction_settings()
    await message.answer(f"<b>Auto reaction</b>: <b>{'on' if current['enabled'] else 'off'}</b>\nEmoji: {current['emoji']}\n\nUse <code>/autoreaction on</code> or <code>/reactionemoji ❤️</code>.")


@router.message(Command("maintenance"))
async def manage_maintenance(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    raw = (command.args or "").strip()
    if not raw:
        current = await maintenance_settings()
        await message.answer(f"Maintenance mode is <b>{'on' if current['enabled'] else 'off'}</b>.\nNotice: {current['message']}\n\nUse <code>/maintenance on | Notice</code> or <code>/maintenance off</code>.")
        return
    choice, _, notice = raw.partition("|")
    choice = choice.strip().lower()
    if choice not in {"on", "off"}:
        await message.answer("Usage: <code>/maintenance on | Optional notice</code> or <code>/maintenance off</code>")
        return
    update = {"enabled": choice == "on"}
    if notice.strip():
        update["message"] = notice.strip()[:500]
    await db.db.bot_settings.update_one({"_id": "maintenance"}, {"$set": update}, upsert=True)
    await message.answer(f"✅ Maintenance mode is now <b>{choice}</b>.")


@router.message(Command("pmsearch"))
async def toggle_pm_search(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    choice = (command.args or "").strip().lower()
    if not choice:
        await message.answer(f"Private-chat search is <b>{'on' if await pm_search_enabled() else 'off'}</b>.\nUse <code>/pmsearch on</code> or <code>/pmsearch off</code>.")
        return
    if choice not in {"on", "off"}:
        await message.answer("Usage: <code>/pmsearch on</code> or <code>/pmsearch off</code>")
        return
    await db.db.bot_settings.update_one({"_id": "pm_search"}, {"$set": {"enabled": choice == "on"}}, upsert=True)
    await message.answer(f"✅ Private-chat search is now <b>{choice}</b>.")


def parse_premium_duration(value: str) -> timedelta:
    raw = value.strip().lower()
    if raw.endswith("d"):
        amount, unit = raw[:-1], "d"
    elif raw.endswith("h"):
        amount, unit = raw[:-1], "h"
    else:
        raise ValueError
    count = int(amount)
    if count < 1 or count > (3650 if unit == "d" else 87_600):
        raise ValueError
    return timedelta(days=count) if unit == "d" else timedelta(hours=count)


@router.message(Command("refer"))
async def referral_link(message: Message) -> None:
    config = await referral_settings()
    if not config["enabled"]:
        await message.answer("Refer & Earn is not active right now.")
        return
    if not bot_username:
        await message.answer("Referral links are temporarily unavailable. Please try again soon.")
        return
    channels = await force_sub_channels()
    if not channels:
        await message.answer("Refer & Earn needs the owner to configure at least one force-subscription channel first.")
        return
    user = await db.db.users.find_one({"user_id": message.from_user.id}, {"referral_count": 1}) or {}
    link = f"https://t.me/{bot_username}?start=ref_{message.from_user.id}"
    await message.answer(f"<b>Refer & Earn Premium</b>\n\nShare your personal link:\n<code>{link}</code>\n\nYou earn premium only after the new user starts AkMovieVerse and joins every required channel. Successful referrals: <b>{user.get('referral_count', 0)}</b>")


@router.message(Command("referral"))
async def manage_referrals(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    raw = (command.args or "").strip().lower()
    if not raw:
        config = await referral_settings()
        await message.answer(f"Refer & Earn is <b>{'on' if config['enabled'] else 'off'}</b>; reward: <b>{config['reward_minutes']} minutes</b>.\n\nUse <code>/referral on</code>, <code>/referral off</code>, or <code>/referral 3d</code> to set the reward.")
        return
    if raw in {"on", "off"}:
        await db.db.bot_settings.update_one({"_id": "referral"}, {"$set": {"enabled": raw == "on"}}, upsert=True)
        if raw == "on" and not await force_sub_channels():
            await message.answer("⚠️ Refer & Earn is on, but it will not award anything until you add at least one required channel with <code>/addfsub CHAT_ID | JOIN_LINK</code>.")
        else:
            await message.answer(f"✅ Refer & Earn is now <b>{raw}</b>.")
        return
    try:
        reward = parse_premium_duration(raw)
    except ValueError:
        await message.answer("Usage: <code>/referral on</code>, <code>/referral off</code>, or <code>/referral 3d</code>.")
        return
    minutes = int(reward.total_seconds() // 60)
    await db.db.bot_settings.update_one({"_id": "referral"}, {"$set": {"reward_minutes": minutes}}, upsert=True)
    await message.answer(f"✅ Referral reward set to <b>{raw}</b> of premium time per new user.")


@router.message(Command("setposttemplate"))
async def set_update_post_template(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        raw_type, template = (command.args or "").split("|", 1)
        content_type = raw_type.strip().lower()
        template = template.strip()
        if content_type not in {"movie", "series"} or not template or len(template) > 1024:
            raise ValueError
        template.format(title="Title", year="2026", rating="8.0/10", type="Movie", file_name="file.mkv", caption="Caption", tags="#tag", overview="Overview")
    except (ValueError, KeyError):
        await message.answer("Usage: <code>/setposttemplate movie | &lt;b&gt;{title}&lt;/b&gt; ({year})\nRating: {rating}\n\n{overview}\n\n{tags}</code>\nUse <code>movie</code> or <code>series</code> and placeholders: <code>{title}</code>, <code>{year}</code>, <code>{rating}</code>, <code>{type}</code>, <code>{file_name}</code>, <code>{caption}</code>, <code>{overview}</code>, <code>{tags}</code>.")
        return
    await db.db.bot_settings.update_one({"_id": "update_template"}, {"$set": {f"{content_type}_template": template}}, upsert=True)
    await message.answer(f"✅ {content_type.title()} update template saved. Turn templates on with <code>/posttemplate on</code>.")


@router.message(Command("posttemplate"))
async def toggle_update_post_templates(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    choice = (command.args or "").strip().lower()
    if choice not in {"on", "off"}:
        config = await update_template_settings()
        available = ", ".join(name for name in ("movie" if config["movie_template"] else "", "series" if config["series_template"] else "") if name) or "none"
        await message.answer(f"Custom update templates are <b>{'on' if config['enabled'] else 'off'}</b>. Saved templates: <b>{available}</b>.\nUse <code>/posttemplate on</code> or <code>/posttemplate off</code>.")
        return
    if choice == "on":
        config = await update_template_settings()
        if not config["movie_template"] and not config["series_template"]:
            await message.answer("Save a movie or series template first with <code>/setposttemplate TYPE | template</code>.")
            return
    await db.db.bot_settings.update_one({"_id": "update_template"}, {"$set": {"enabled": choice == "on"}}, upsert=True)
    await message.answer(f"✅ Custom update templates are now <b>{choice}</b>.")


@router.message(Command("clearposttemplate"))
async def clear_update_post_template(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    content_type = (command.args or "").strip().lower()
    if content_type not in {"movie", "series"}:
        await message.answer("Usage: <code>/clearposttemplate movie</code> or <code>/clearposttemplate series</code>")
        return
    await db.db.bot_settings.update_one({"_id": "update_template"}, {"$unset": {f"{content_type}_template": ""}}, upsert=True)
    await message.answer(f"✅ {content_type.title()} update template cleared.")


@router.message(Command("autometa"))
async def toggle_automatic_metadata(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    choice = (command.args or "").strip().lower()
    if choice not in {"on", "off"}:
        await message.answer(f"Automatic TMDB metadata is <b>{'on' if await auto_metadata_enabled() else 'off'}</b>.\nUse <code>/autometa on</code> or <code>/autometa off</code>.")
        return
    if choice == "on" and not settings.tmdb_api_key and not settings.tmdb_read_access_token:
        await message.answer("Set <code>TMDB_READ_ACCESS_TOKEN</code> or <code>TMDB_API_KEY</code> in Koyeb first, then restart the bot.")
        return
    await db.db.bot_settings.update_one({"_id": "auto_metadata"}, {"$set": {"enabled": choice == "on"}}, upsert=True)
    await message.answer(f"✅ Automatic TMDB metadata is now <b>{choice}</b> for newly indexed video files.")


@router.message(Command("tmdb"))
async def tmdb_metadata(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("Usage: <code>/tmdb Movie or series title</code>")
        return
    if not settings.tmdb_api_key and not settings.tmdb_read_access_token:
        await message.answer("TMDB is not configured. Add <code>TMDB_READ_ACCESS_TOKEN</code> (recommended) or <code>TMDB_API_KEY</code> in Koyeb and restart the bot.")
        return
    item = await tmdb_lookup(query)
    if not item:
        await message.answer("No TMDB movie or series result was found.")
        return
    media_type = item["media_type"]
    details_url = f"https://www.themoviedb.org/{media_type}/{item['id']}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="View on TMDB", url=details_url)
    ]])
    poster = item.get("poster_path")
    if poster:
        await message.answer_photo(
            photo=f"https://image.tmdb.org/t/p/w500{poster}",
            caption=tmdb_caption(item),
            reply_markup=keyboard,
        )
    else:
        await message.answer(tmdb_caption(item), reply_markup=keyboard)


@router.message(Command("setcaption"))
async def set_custom_caption(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    template = (command.args or "").strip()
    if not template:
        await message.answer("Usage: <code>/setcaption 🎬 {file_name}\n\n{original_caption}</code>\nAvailable placeholders: <code>{file_name}</code>, <code>{original_caption}</code>.")
        return
    if len(template) > 1024:
        await message.answer("The caption template must be 1024 characters or fewer.")
        return
    try:
        template.format(file_name="Example file", original_caption="Original caption")
    except (KeyError, ValueError):
        await message.answer("Use only <code>{file_name}</code> and <code>{original_caption}</code> placeholders.")
        return
    await db.db.bot_settings.update_one({"_id": "caption"}, {"$set": {"template": template}}, upsert=True)
    await message.answer("✅ Custom delivery caption saved. It will be used for newly delivered media files.")


@router.message(Command("caption"))
async def show_custom_caption(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    template = await caption_template()
    await message.answer(f"<b>Custom caption</b>\n\n{escape(template) if template else 'Not configured.'}\n\nSet: <code>/setcaption Your template</code>\nClear: <code>/clearcaption</code>")


@router.message(Command("clearcaption"))
async def clear_custom_caption(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    await db.db.bot_settings.update_one({"_id": "caption"}, {"$unset": {"template": ""}}, upsert=True)
    await message.answer("✅ Custom delivery caption cleared.")


@router.message(Command("setad"))
async def set_ad(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        text, url, *button = [part.strip() for part in (command.args or "").split("|")]
        if not text or not url.startswith(("https://", "http://")):
            raise ValueError
        button_text = button[0] if button and button[0] else "Open sponsor"
    except ValueError:
        await message.answer("Usage: <code>/setad Sponsor text | https://example.com | Open sponsor</code>")
        return
    await db.db.bot_settings.update_one(
        {"_id": "ad"},
        {"$set": {"text": text[:300], "url": url, "button_text": button_text[:64]}},
        upsert=True,
    )
    await message.answer("✅ Ad content saved. Use <code>/ads on</code> to show it to free users.")


@router.message(Command("ads"))
async def toggle_ads(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    choice = (command.args or "").strip().lower()
    if choice not in {"on", "off"}:
        current = await ad_settings()
        await message.answer(f"Ads are <b>{'on' if current['enabled'] else 'off'}</b>.\nUse <code>/ads on</code> or <code>/ads off</code>.")
        return
    if choice == "on":
        current = await ad_settings()
        if not current["text"] or not current["url"]:
            await message.answer("Set the ad first: <code>/setad Text | https://link | Button</code>")
            return
    await db.db.bot_settings.update_one({"_id": "ad"}, {"$set": {"enabled": choice == "on"}}, upsert=True)
    await message.answer(f"✅ Ads are now <b>{choice}</b>. Premium users remain ad-free.")


@router.message(Command("adstatus"))
async def show_ad_status(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    current = await ad_settings()
    await message.answer(f"<b>Ads</b>: <b>{'on' if current['enabled'] else 'off'}</b>\nText: {escape(current['text']) or 'Not set'}\nURL: <code>{escape(current['url']) or 'Not set'}</code>")


@router.message(Command("clearad"))
async def clear_ad(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    await db.db.bot_settings.update_one({"_id": "ad"}, {"$set": {"enabled": False, "text": "", "url": "", "button_text": "Open sponsor"}}, upsert=True)
    await message.answer("✅ Ad disabled and cleared.")


@router.message(Command("createcode"))
async def create_redeem_code(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        raw_code, raw_duration, raw_limit = [part.strip() for part in (command.args or "").split("|", 2)]
        code = raw_code.upper()
        if not re.fullmatch(r"[A-Z0-9_-]{4,32}", code):
            raise ValueError
        duration = parse_premium_duration(raw_duration)
        limit = int(raw_limit)
        if not 1 <= limit <= 100_000:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("Usage: <code>/createcode MOVIE30 | 30d | 100</code>\nCodes use 4–32 letters, numbers, <code>_</code>, or <code>-</code>.")
        return
    try:
        await db.db.redeem_codes.insert_one({
            "code": code,
            "duration_minutes": int(duration.total_seconds() // 60),
            "max_uses": limit,
            "uses": 0,
            "redeemed_by": [],
            "created_at": datetime.now(timezone.utc),
            "created_by": message.from_user.id,
        })
    except Exception:
        await message.answer("That code already exists. Choose a different one.")
        return
    await message.answer(f"✅ Redeem code <code>{code}</code> created: <b>{raw_duration}</b>, maximum <b>{limit}</b> users.")


@router.message(Command("redeem"))
async def redeem_code(message: Message, command: CommandObject) -> None:
    code = (command.args or "").strip().upper()
    if not code:
        await message.answer("Usage: <code>/redeem CODE</code>")
        return
    redeemed = await db.db.redeem_codes.find_one_and_update(
        {"code": code, "redeemed_by": {"$ne": message.from_user.id}, "$expr": {"$lt": ["$uses", "$max_uses"]}},
        {"$addToSet": {"redeemed_by": message.from_user.id}, "$inc": {"uses": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if not redeemed:
        await message.answer("This code is invalid, already used by you, or has reached its limit.")
        return
    current = await premium_until(message.from_user.id)
    starts_at = current or datetime.now(timezone.utc)
    until = starts_at + timedelta(minutes=redeemed["duration_minutes"])
    await db.db.users.update_one(
        {"user_id": message.from_user.id},
        {"$set": {"user_id": message.from_user.id, "name": message.from_user.full_name, "premium_until": until, "premium_granted_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await message.answer(f"🎉 Code accepted. Premium is active until <b>{until.astimezone(ZoneInfo(settings.timezone)):%d %b %Y, %I:%M %p}</b>.")


@router.message(Command("codes"))
async def list_redeem_codes(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    codes = await db.db.redeem_codes.find({}).sort("created_at", -1).limit(30).to_list(length=30)
    lines = [f"• <code>{item['code']}</code> — {item['uses']}/{item['max_uses']} used — {item['duration_minutes']} min" for item in codes]
    await message.answer("<b>Redeem codes</b>\n" + ("\n".join(lines) if lines else "No codes created."))


@router.message(Command("deletecode"))
async def delete_redeem_code(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    code = (command.args or "").strip().upper()
    if not code:
        await message.answer("Usage: <code>/deletecode CODE</code>")
        return
    result = await db.db.redeem_codes.delete_one({"code": code})
    await message.answer("✅ Redeem code deleted." if result.deleted_count else "Code not found.")


def local_time_text(value: datetime | None) -> str:
    if not value:
        return "Not active"
    return value.astimezone(ZoneInfo(settings.timezone)).strftime("%d %b %Y, %I:%M %p")


@router.message(Command("id"))
async def show_ids(message: Message) -> None:
    await message.answer(
        f"<b>Your information</b>\n\n"
        f"User ID: <code>{message.from_user.id}</code>\n"
        f"Chat ID: <code>{message.chat.id}</code>\n"
        f"Chat type: <b>{message.chat.type.value}</b>"
    )


@router.message(Command("alert"))
async def save_search_alert(message: Message, command: CommandObject) -> None:
    query = normalize_query(command.args or "")
    if len(query) < 2:
        await message.answer("Usage: <code>/alert Movie or file title</code>")
        return
    limit = 20 if await premium_until(message.from_user.id) else 3
    existing = await db.db.search_alerts.find_one({"user_id": message.from_user.id, "query": query})
    if existing:
        await message.answer("You already have that saved search alert.")
        return
    count = await db.db.search_alerts.count_documents({"user_id": message.from_user.id})
    if count >= limit:
        plan = "Premium users can save up to 20 alerts." if limit == 3 else "You have reached the 20-alert premium limit."
        await message.answer(f"You can save up to <b>{limit}</b> alerts. {plan}")
        return
    result = await db.db.search_alerts.insert_one({
        "user_id": message.from_user.id,
        "query": query,
        "created_at": datetime.now(timezone.utc),
    })
    await message.answer(f"✅ Alert saved for <b>{escape(query)}</b>. I will notify you when a matching new file is indexed.\nID: <code>{result.inserted_id}</code>")


@router.message(Command("alerts"))
async def list_search_alerts(message: Message) -> None:
    alerts = await db.db.search_alerts.find({"user_id": message.from_user.id}).sort("created_at", -1).to_list(length=25)
    if not alerts:
        await message.answer("You have no saved search alerts. Create one with <code>/alert title</code>.")
        return
    lines = [f"• <code>{alert['_id']}</code> — {escape(alert['query'])}" for alert in alerts]
    await message.answer("<b>Your saved search alerts</b>\n\n" + "\n".join(lines) + "\n\nRemove one: <code>/stopalert ALERT_ID</code>")


@router.message(Command("stopalert"))
async def stop_search_alert(message: Message, command: CommandObject) -> None:
    try:
        alert_id = ObjectId(command.args or "")
    except Exception:
        await message.answer("Usage: <code>/stopalert ALERT_ID</code>")
        return
    result = await db.db.search_alerts.delete_one({"_id": alert_id, "user_id": message.from_user.id})
    await message.answer("✅ Saved search alert removed." if result.deleted_count else "Alert not found.")


@router.message(Command("userinfo"))
async def user_info(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        user_id = int(command.args or "")
    except ValueError:
        await message.answer("Usage: <code>/userinfo USER_ID</code>")
        return
    user = await db.db.users.find_one({"user_id": user_id})
    if not user:
        await message.answer("No AkMovieVerse user record was found for that ID.")
        return
    now = datetime.now(timezone.utc)
    premium = user.get("premium_until") if user.get("premium_until") and user["premium_until"] > now else None
    verified = user.get("verified_until") if user.get("verified_until") and user["verified_until"] > now else None
    await message.answer(
        f"<b>User information</b>\n\n"
        f"Name: <b>{escape(user.get('name') or 'Unknown')}</b>\n"
        f"ID: <code>{user_id}</code>\n"
        f"Language: <b>{LANGUAGES.get(user.get('language', 'en'), user.get('language', 'en'))}</b>\n"
        f"Status: <b>{'banned' if user.get('banned') else 'active'}</b>\n"
        f"Premium: <b>{local_time_text(premium)}</b>\n"
        f"Verified: <b>{local_time_text(verified)}</b>\n"
        f"Verification provider: <b>{escape(user.get('verification_provider') or '—')}</b>\n"
        f"Successful referrals: <b>{user.get('referral_count', 0)}</b>\n"
        f"Referred by: <code>{user.get('referred_by') or '—'}</code>"
    )


@router.message(Command("verifiedstats"))
async def verified_stats(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    now = datetime.now(timezone.utc)
    active = await db.db.users.count_documents({"verified_until": {"$gt": now}})
    all_time = await db.db.users.count_documents({"verified_at": {"$exists": True}})
    await message.answer(f"<b>Verification statistics</b>\n\nActive verified users: <b>{active}</b>\nUsers verified at least once: <b>{all_time}</b>")


@router.message(Command("unverify"))
async def unverify_user(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        user_id = int(command.args or "")
    except ValueError:
        await message.answer("Usage: <code>/unverify USER_ID</code>")
        return
    result = await db.db.users.update_one(
        {"user_id": user_id},
        {"$unset": {"verified_until": "", "verified_at": "", "verification_provider": ""}},
    )
    await message.answer("✅ Verification removed." if result.matched_count else "User not found.")


@router.message(Command("addpremium"))
async def add_premium(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        raw_user, raw_duration = [part.strip() for part in (command.args or "").split("|", 1)]
        user_id = int(raw_user)
        duration = parse_premium_duration(raw_duration)
    except (ValueError, TypeError):
        await message.answer("Usage: <code>/addpremium USER_ID | 30d</code>\nUse hours (<code>12h</code>) or days (<code>30d</code>).")
        return
    current = await premium_until(user_id)
    starts_at = current or datetime.now(timezone.utc)
    until = starts_at + duration
    await db.db.users.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "premium_until": until, "premium_granted_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await message.answer(f"✅ Premium enabled for <code>{user_id}</code> until <b>{until.astimezone(ZoneInfo(settings.timezone)):%d %b %Y, %I:%M %p}</b>.")
    try:
        await message.bot.send_message(user_id, f"🎉 Premium access is active until <b>{until.astimezone(ZoneInfo(settings.timezone)):%d %b %Y, %I:%M %p}</b>. You can now bypass shortlink verification.")
    except Exception:
        pass


@router.message(Command("removepremium"))
async def remove_premium(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        user_id = int(command.args or "")
    except ValueError:
        await message.answer("Usage: <code>/removepremium USER_ID</code>")
        return
    result = await db.db.users.update_one({"user_id": user_id}, {"$unset": {"premium_until": "", "premium_granted_at": ""}})
    await message.answer("✅ Premium removed." if result.matched_count else "User not found.")


@router.message(Command("myplan"))
async def my_plan(message: Message) -> None:
    until = await premium_until(message.from_user.id)
    if not until:
        await message.answer("You are using the free plan. Ask the owner if premium access is available.")
        return
    local_until = until.astimezone(ZoneInfo(settings.timezone))
    remaining = until - datetime.now(timezone.utc)
    await message.answer(f"<b>Premium active</b> ✅\nExpires: <b>{local_until:%d %b %Y, %I:%M %p}</b>\nRemaining: <b>{max(1, int(remaining.total_seconds() // 3600))} hours</b>\n\nShortlink verification is bypassed.")


@router.message(Command("premiumstats"))
async def premium_stats(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    active = await db.db.users.count_documents({"premium_until": {"$gt": datetime.now(timezone.utc)}})
    await message.answer(f"<b>Premium members</b>: <b>{active}</b> active\n\nManage: <code>/addpremium USER_ID | 30d</code>\nRemove: <code>/removepremium USER_ID</code>")


@router.message(Command("verification"))
async def manage_verification(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    raw = (command.args or "").strip().lower()
    if not raw:
        config = await verification_settings()
        await message.answer(f"Verification is <b>{'on' if config.get('enabled') else 'off'}</b>; validity: <b>{config.get('valid_minutes', 720)} minutes</b>.\nUsage: <code>/verification on | 720</code> or <code>/verification off</code>")
        return
    choice, _, duration = raw.partition("|")
    choice = choice.strip()
    if choice not in {"on", "off"}:
        await message.answer("Usage: <code>/verification on | 720</code> or <code>/verification off</code>")
        return
    minutes = 720
    if duration.strip():
        try:
            minutes = max(5, min(int(duration.strip()), 43_200))
        except ValueError:
            await message.answer("Verification duration must be a number of minutes (5 to 43200).")
            return
    await db.db.bot_settings.update_one({"_id": "verification"}, {"$set": {"enabled": choice == "on", "valid_minutes": minutes}}, upsert=True)
    await message.answer(f"✅ Verification is now <b>{choice}</b> with a <b>{minutes}-minute</b> verified period.")


@router.message(Command("addshortener"))
async def add_shortener(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        name, template, *options = [part.strip() for part in (command.args or "").split("|")]
        if not name or "{url}" not in template or not template.startswith(("https://", "http://")):
            raise ValueError
        window = options[0] if options else ""
        start, end = (window.split("-", 1) if "-" in window else (None, None))
        if start and (len(start) != 5 or len(end) != 5):
            raise ValueError
    except ValueError:
        await message.answer("Usage: <code>/addshortener Name | https://provider.example/?url={url} | 09:00-23:00</code>\nThe time window is optional and uses TIMEZONE. The template must contain <code>{url}</code>.")
        return
    await db.db.shorteners.update_one(
        {"name": name},
        {"$set": {"name": name, "url_template": template, "window_start": start, "window_end": end, "enabled": True, "priority": 100, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await message.answer(f"✅ Shortener <b>{name}</b> added and enabled.")


@router.message(Command("shorteners"))
async def list_shorteners(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    providers = await db.db.shorteners.find({}).sort([("priority", 1), ("name", 1)]).to_list(length=50)
    rows = [f"• <b>{item['name']}</b> — {'on' if item.get('enabled') else 'off'} — {item.get('window_start') or 'all day'}{('-' + item['window_end']) if item.get('window_end') else ''}" for item in providers]
    await message.answer("<b>Shortener providers</b>\n" + ("\n".join(rows) if rows else "None configured."))


@router.message(Command("shortener"))
async def toggle_shortener(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        name, choice = [part.strip() for part in (command.args or "").split("|", 1)]
        if choice.lower() not in {"on", "off"}:
            raise ValueError
    except ValueError:
        await message.answer("Usage: <code>/shortener Name | on</code> or <code>/shortener Name | off</code>")
        return
    result = await db.db.shorteners.update_one({"name": name}, {"$set": {"enabled": choice.lower() == "on"}})
    await message.answer(f"✅ Shortener <b>{name}</b> {'enabled' if choice.lower() == 'on' else 'disabled'}." if result.matched_count else "Shortener not found.")


@router.callback_query(F.data.startswith("language:"))
async def set_language(callback: CallbackQuery) -> None:
    code = callback.data.split(":", 1)[1]
    if code not in LANGUAGES:
        await callback.answer("Unsupported language.", show_alert=True)
        return
    await db.db.users.update_one({"user_id": callback.from_user.id}, {"$set": {"language": code}}, upsert=True)
    await callback.message.edit_text(translate(code, "language_saved", language=LANGUAGES[code]))
    await callback.answer()


@router.inline_query()
async def inline_search(inline_query: InlineQuery) -> None:
    if await maintenance_active(inline_query.from_user.id):
        await inline_query.answer([], cache_time=5, is_personal=True, switch_pm_text="AkMovieVerse is under maintenance", switch_pm_parameter="maintenance")
        return
    query = normalize_query(inline_query.query)
    if len(query) < 2 or not bot_username:
        await inline_query.answer([], cache_time=5, is_personal=True, switch_pm_text="Type at least two characters to search", switch_pm_parameter="search")
        return
    files, _ = await db.search(query, 0, 20)
    results = []
    for file in files:
        file_id = str(file["_id"])
        delivery_url = f"https://t.me/{bot_username}?start=file_{file_id}"
        results.append(InlineQueryResultArticle(
            id=file_id,
            title=file["name"][:64],
            description=f"{file['kind']} · Open privately for delivery",
            input_message_content=InputTextMessageContent(
                message_text=f"<b>{file['name']}</b>\nPrivate delivery is available from AkMovieVerse.",
                parse_mode=ParseMode.HTML,
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📥 Open private delivery", url=delivery_url)
            ]]),
        ))
    await inline_query.answer(results, cache_time=10, is_personal=True)


@router.message(Command("addsource"))
async def add_source(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        chat_id = int(command.args or "")
    except ValueError:
        await message.answer("Usage: <code>/addsource -1001234567890</code>")
        return
    try:
        chat = await message.bot.get_chat(chat_id)
    except Exception:
        await message.answer("I cannot access that channel. Add me as an admin first, then try again.")
        return
    await db.add_source_channel(chat.id, chat.title)
    await message.answer(f"✅ Source channel added: <b>{chat.title}</b>")


@router.message(Command("removesource"))
async def remove_source(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        chat_id = int(command.args or "")
    except ValueError:
        await message.answer("Usage: <code>/removesource -1001234567890</code>")
        return
    result = await db.db.source_channels.delete_one({"chat_id": chat_id})
    await message.answer("✅ Source channel removed." if result.deleted_count else "That channel is not a source.")


@router.message(Command("addfsub"))
async def add_force_sub_channel(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        raw_chat_id, *raw_link = (command.args or "").split("|", 1)
        chat_id = int(raw_chat_id.strip())
    except ValueError:
        await message.answer("Usage: <code>/addfsub CHAT_ID | JOIN_LINK</code>\nThe join link is optional for public channels.")
        return
    try:
        chat = await message.bot.get_chat(chat_id)
    except Exception:
        await message.answer("I cannot access that channel. Add me as an admin first, then try again.")
        return
    join_url = raw_link[0].strip() if raw_link else (f"https://t.me/{chat.username}" if chat.username else None)
    await db.db.force_sub_channels.update_one(
        {"chat_id": chat.id},
        {"$set": {"chat_id": chat.id, "title": chat.title, "join_url": join_url, "added_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await message.answer(f"✅ Added force-subscription channel: <b>{chat.title}</b>")


@router.message(Command("removefsub"))
async def remove_force_sub_channel(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        chat_id = int(command.args or "")
    except ValueError:
        await message.answer("Usage: <code>/removefsub CHAT_ID</code>")
        return
    result = await db.db.force_sub_channels.delete_one({"chat_id": chat_id})
    await message.answer("✅ Force-subscription channel removed." if result.deleted_count else "That channel is not configured.")


@router.message(Command("fsub"))
async def list_force_sub_channels(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    channels = await force_sub_channels()
    details = "\n".join(f"• <code>{item['chat_id']}</code> — {item.get('title') or 'Untitled'}" for item in channels)
    await message.answer(f"<b>Force-subscription channels</b>\n{details or 'None configured.'}")


@router.message(Command("requests"))
async def toggle_request_system(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    choice = (command.args or "").lower()
    if choice not in {"on", "off"}:
        state = "on" if await request_system_enabled() else "off"
        await message.answer(f"Request system is <b>{state}</b>.\nUsage: <code>/requests on</code> or <code>/requests off</code>")
        return
    await db.db.bot_settings.update_one(
        {"_id": "global"}, {"$set": {"request_system_enabled": choice == "on"}}, upsert=True
    )
    await message.answer(f"✅ Request system is now <b>{choice}</b>.")


@router.message(Command("recentfiles"))
async def recent_files(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    files = await db.db.files.find({}).sort("created_at", -1).to_list(length=20)
    details = "\n".join(f"• <code>{item['_id']}</code> — {item['name'][:55]}" for item in files)
    await message.answer(f"<b>Recently indexed files</b>\n{details or 'No files indexed yet.'}")


@router.message(Command("renamefile"))
async def rename_file(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        raw_id, name = (command.args or "").split("|", 1)
        file_id = ObjectId(raw_id.strip())
        name = name.strip()
        if not name:
            raise ValueError
    except Exception:
        await message.answer("Usage: <code>/renamefile FILE_ID | New searchable title</code>")
        return
    file = await db.db.files.find_one({"_id": file_id})
    if not file:
        await message.answer("File not found.")
        return
    search_text = normalize_query(f"{name} {file.get('caption', '')} {' '.join(file.get('tags', []))}")
    await db.db.files.update_one({"_id": file_id}, {"$set": {"name": name, "search_text": search_text, "updated_at": datetime.now(timezone.utc)}})
    await message.answer("✅ File title updated and reindexed.")


@router.message(Command("addtags"))
async def add_tags(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        raw_id, raw_tags = (command.args or "").split("|", 1)
        file_id = ObjectId(raw_id.strip())
        tags = [normalize_query(tag).lstrip("#").lower() for tag in raw_tags.replace(",", " ").split()]
        tags = list(dict.fromkeys(tag for tag in tags if tag))
        if not tags:
            raise ValueError
    except Exception:
        await message.answer("Usage: <code>/addtags FILE_ID | hindi 1080p action</code>")
        return
    file = await db.db.files.find_one({"_id": file_id})
    if not file:
        await message.answer("File not found.")
        return
    merged_tags = list(dict.fromkeys([*file.get("tags", []), *tags]))
    search_text = normalize_query(f"{file['name']} {file.get('caption', '')} {' '.join(merged_tags)}")
    await db.db.files.update_one({"_id": file_id}, {"$set": {"tags": merged_tags, "search_text": search_text, "updated_at": datetime.now(timezone.utc)}})
    await message.answer(f"✅ Tags saved: <code>{' '.join(merged_tags)}</code>")


@router.message(Command("removefile"))
async def remove_file(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        file_id = ObjectId(command.args or "")
    except Exception:
        await message.answer("Usage: <code>/removefile FILE_ID</code>")
        return
    result = await db.db.files.delete_one({"_id": file_id})
    await message.answer("✅ File removed from the searchable index." if result.deleted_count else "File not found.")


@router.message(Command("deletefiles"))
async def delete_multiple_files(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    raw_ids = re.split(r"[\s,]+", (command.args or "").strip())
    try:
        file_ids = [ObjectId(raw_id) for raw_id in raw_ids if raw_id]
    except Exception:
        await message.answer("Usage: <code>/deletefiles FILE_ID FILE_ID</code>\nYou can separate IDs with spaces or commas.")
        return
    if not file_ids:
        await message.answer("Usage: <code>/deletefiles FILE_ID FILE_ID</code>")
        return
    result = await db.db.files.delete_many({"_id": {"$in": file_ids}})
    await log_index_cleanup(message.bot, f"Removed <b>{result.deleted_count}</b> indexed file(s) by ID.\nOwner: <code>{message.from_user.id}</code>")
    await message.answer(f"✅ Removed <b>{result.deleted_count}</b> file(s) from the searchable index.")


@router.message(Command("deletebyname"))
async def delete_files_by_name(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    query = (command.args or "").strip()
    if len(query) < 2:
        await message.answer("Usage: <code>/deletebyname title or filename</code>")
        return
    result = await db.db.files.delete_many({"name": {"$regex": re.escape(query), "$options": "i"}})
    await log_index_cleanup(message.bot, f"Removed <b>{result.deleted_count}</b> indexed file(s) matching <code>{query}</code>.\nOwner: <code>{message.from_user.id}</code>")
    await message.answer(f"✅ Removed <b>{result.deleted_count}</b> file(s) matching <b>{query}</b> from the searchable index.")


@router.message(Command("deleteolder"))
async def delete_old_files(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        days = int(command.args or "")
        if not 1 <= days <= 3650:
            raise ValueError
    except ValueError:
        await message.answer("Usage: <code>/deleteolder DAYS</code>\nExample: <code>/deleteolder 180</code>")
        return
    threshold = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.db.files.delete_many({"created_at": {"$lt": threshold}})
    await log_index_cleanup(message.bot, f"Removed <b>{result.deleted_count}</b> indexed file(s) older than <b>{days} days</b>.\nOwner: <code>{message.from_user.id}</code>")
    await message.answer(f"✅ Removed <b>{result.deleted_count}</b> file(s) older than <b>{days} days</b> from the searchable index.")


@router.message(Command("clearindex"))
async def clear_index(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    if (command.args or "").strip() != "CONFIRM":
        await message.answer("This clears every searchable file record but does not delete Telegram posts.\nTo continue: <code>/clearindex CONFIRM</code>")
        return
    result = await db.db.files.delete_many({})
    await log_index_cleanup(message.bot, f"⚠️ Cleared the complete searchable index: <b>{result.deleted_count}</b> file record(s).\nOwner: <code>{message.from_user.id}</code>")
    await message.answer(f"✅ Cleared <b>{result.deleted_count}</b> file record(s) from the searchable index.")


@router.message(Command("autocleanup"))
async def set_auto_cleanup(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    choice = (command.args or "").strip().lower()
    if choice not in {"on", "off"}:
        await message.answer("Usage: <code>/autocleanup on</code> or <code>/autocleanup off</code>")
        return
    await db.db.bot_settings.update_one({"_id": "cleanup"}, {"$set": {"remove_low_quality": choice == "on"}}, upsert=True)
    await message.answer(f"✅ CamRip / PreDVD index cleanup is <b>{choice}</b> for newly posted files.")


@router.message(Command("panel"))
async def panel(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    await message.answer(await owner_stats_text(), reply_markup=panel_keyboard())


@router.callback_query(F.data.startswith("panel:"))
async def owner_panel_action(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    if action == "close":
        await callback.message.delete()
        await callback.answer()
        return
    if action == "stats":
        text = await owner_stats_text()
    elif action == "sources":
        sources = await db.db.source_channels.find({}).sort("added_at", -1).to_list(length=50)
        details = "\n".join(f"• <code>{item['chat_id']}</code> — {item.get('title') or 'Untitled'}" for item in sources)
        text = f"<b>Source channels ({len(sources)})</b>\n{details or 'No channels added.'}\n\nAdd: <code>/addsource CHAT_ID</code>\nRemove: <code>/removesource CHAT_ID</code>"
    elif action == "requests":
        requests = await db.db.requests.find({"status": "open"}).sort("created_at", -1).to_list(length=15)
        details = "\n".join(f"• <code>{item['_id']}</code> — {item['query']} ({len(item.get('requesters', [])) or 1} user(s))" for item in requests)
        text = f"<b>Open requests ({len(requests)})</b>\n{details or 'No pending requests.'}\n\nClose one: <code>/closerequest REQUEST_ID</code>"
    elif action == "requesttoggle":
        enabled = await request_system_enabled()
        await db.db.bot_settings.update_one({"_id": "global"}, {"$set": {"request_system_enabled": not enabled}}, upsert=True)
        text = f"<b>Request system</b>\n\nIt is now <b>{'on' if not enabled else 'off'}</b>.\n\nCommand: <code>/requests on</code> or <code>/requests off</code>"
    elif action == "fsub":
        channels = await force_sub_channels()
        details = "\n".join(f"• <code>{item['chat_id']}</code> — {item.get('title') or 'Untitled'}" for item in channels)
        text = f"<b>Force-subscription channels ({len(channels)})</b>\n{details or 'None configured.'}\n\nAdd: <code>/addfsub CHAT_ID | JOIN_LINK</code>\nRemove: <code>/removefsub CHAT_ID</code>"
    elif action == "searches":
        searches = await db.top_searches()
        details = "\n".join(f"• {item['query']} — <b>{item['count']}</b>" for item in searches)
        text = f"<b>Top searches</b>\n{details or 'No searches recorded yet.'}"
    elif action == "files":
        files = await db.db.files.find({}).sort("created_at", -1).to_list(length=10)
        details = "\n".join(f"• <code>{item['_id']}</code> — {item['name'][:55]}" for item in files)
        text = f"<b>Recently indexed files</b>\n{details or 'No files indexed yet.'}\n\nManage: <code>/renamefile ID | title</code>\nTag: <code>/addtags ID | tag1 tag2</code>\nRemove: <code>/removefile ID</code>"
    elif action == "broadcast":
        text = "<b>Broadcast</b>\n\nSend <code>/broadcast Your message here</code> to deliver immediately.\n\nSchedule: <code>/schedule YYYY-MM-DD HH:MM | Your message</code>"
    elif action == "schedules":
        schedules = await db.db.scheduled_broadcasts.find({"status": "pending"}).sort("due_at", 1).to_list(length=15)
        local_zone = ZoneInfo(settings.timezone)
        details = "\n".join(
            f"• <code>{item['_id']}</code> — {item['due_at'].astimezone(local_zone):%d %b %Y, %I:%M %p}" for item in schedules
        )
        text = f"<b>Scheduled broadcasts</b>\n{details or 'No messages scheduled.'}\n\nCancel: <code>/cancelschedule SCHEDULE_ID</code>"
    else:
        text = "<b>User controls</b>\n\nBan: <code>/ban USER_ID</code>\nUnban: <code>/unban USER_ID</code>\n\nBanned users cannot search."
    await callback.message.edit_text(text, reply_markup=panel_keyboard())
    await callback.answer()


@router.channel_post(F.document | F.video | F.audio | F.animation | F.photo)
async def index_channel_file(message: Message) -> None:
    if not await db.is_source_channel(message.chat.id):
        return
    file_id, name = message_file(message)
    if not file_id:
        return
    caption = message.caption or ""
    record = {
        "source_chat_id": message.chat.id,
        "source_message_id": message.message_id,
        "telegram_file_id": file_id,
        "name": name or "file",
        "kind": media_kind(message),
        "category": media_category(message),
        "caption": caption,
        "search_text": normalize_query(f"{name or ''} {caption}"),
        "created_at": datetime.now(timezone.utc),
    }
    if (await cleanup_settings())["remove_low_quality"] and low_quality_release(record):
        await log_index_cleanup(message.bot, f"Skipped new low-quality release: <b>{record['name']}</b>\nSource: <code>{message.chat.id}</code>")
        return
    await enrich_video_metadata(record)
    index_result = await db.upsert_file(record)
    stored_file = await db.db.files.find_one(
        {"source_chat_id": message.chat.id, "source_message_id": message.message_id}, {"_id": 1}
    )
    if not stored_file:
        return
    stored_file_id = str(stored_file["_id"])
    if index_result.upserted_id:
        await announce_new_file(message.bot, record, stored_file_id)
        await notify_saved_alerts(message.bot, record, stored_file_id)
    matching_requests = await db.matching_requests(record["search_text"]) if await request_system_enabled() else []
    for request in matching_requests:
        requester_ids = request.get("requesters") or ([request["user_id"]] if request.get("user_id") else [])
        delivered = 0
        for user_id in requester_ids:
            token = await db.make_download_token(stored_file_id, user_id, await delivery_token_minutes(user_id))
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=await delivery_button_text(user_id), callback_data=f"send:{token}")
            ]])
            try:
                await message.bot.send_message(
                    user_id,
                    f"✅ <b>Now available:</b> {record['name']}\nYour request for <i>{request['query']}</i> may have been fulfilled.",
                    reply_markup=keyboard,
                )
                delivered += 1
            except Exception:
                continue
        await db.db.requests.update_one(
            {"_id": request["_id"]},
            {"$set": {"status": "fulfilled", "fulfilled_at": datetime.now(timezone.utc), "fulfilled_file_id": stored_file_id, "notified_users": delivered}},
        )


@router.message(F.chat.type == ChatType.PRIVATE, F.document | F.video | F.audio | F.animation | F.photo)
async def owner_upload_inbox(message: Message) -> None:
    """Copy owner-uploaded files to the storage channel and index them immediately."""
    if not is_owner(message.from_user.id):
        return
    if not settings.storage_channel_id:
        await message.answer("Set <code>STORAGE_CHANNEL_ID</code> first, then restart the bot. The bot must be an admin in that channel.")
        return
    file_id, name = message_file(message)
    if not file_id:
        return
    try:
        storage = await message.bot.get_chat(settings.storage_channel_id)
        copied = await message.bot.copy_message(settings.storage_channel_id, message.chat.id, message.message_id)
    except Exception:
        await message.answer("I could not copy this file. Confirm that the storage channel ID is correct and that I am an admin there.")
        return
    await db.add_source_channel(storage.id, storage.title)
    caption = message.caption or ""
    record = {
        "source_chat_id": storage.id,
        "source_message_id": copied.message_id,
        "telegram_file_id": file_id,
        "name": name or "file",
        "kind": media_kind(message),
        "category": media_category(message),
        "caption": caption,
        "search_text": normalize_query(f"{name or ''} {caption}"),
        "created_at": datetime.now(timezone.utc),
    }
    if (await cleanup_settings())["remove_low_quality"] and low_quality_release(record):
        await log_index_cleanup(message.bot, f"Skipped owner-uploaded low-quality release: <b>{record['name']}</b>")
        await message.answer("⚠️ This CamRip / PreDVD-style file was copied to storage but skipped from the searchable index by auto-cleanup.")
        return
    await enrich_video_metadata(record)
    index_result = await db.upsert_file(record)
    stored_file = await db.db.files.find_one(
        {"source_chat_id": storage.id, "source_message_id": copied.message_id}, {"_id": 1}
    )
    if index_result.upserted_id and stored_file:
        await announce_new_file(message.bot, record, str(stored_file["_id"]))
        await notify_saved_alerts(message.bot, record, str(stored_file["_id"]))
    await message.answer(
        f"✅ Added to <b>{storage.title}</b> and indexed.\n\n"
        f"Title: <b>{record['name']}</b>\n"
        f"Manage it with <code>/recentfiles</code> if you want to rename it or add tags."
    )


@router.message(Command("setwelcome"))
async def set_welcome(message: Message, command: CommandObject) -> None:
    if not await require_group_admin(message):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Usage: <code>/setwelcome Welcome {name} to {group}!</code>")
        return
    await db.db.groups.update_one({"chat_id": message.chat.id}, {"$set": {"welcome_message": text}}, upsert=True)
    await message.answer("✅ Welcome message saved. Use <code>{name}</code> and <code>{group}</code> as placeholders.")


@router.message(Command("settings"))
async def group_settings(message: Message) -> None:
    if not await require_group_admin(message):
        return
    group = await db.db.groups.find_one({"chat_id": message.chat.id}) or {}
    await message.answer(group_settings_text(group), reply_markup=group_settings_keyboard(group))


@router.callback_query(F.data.startswith("groupset:"))
async def group_settings_action(callback: CallbackQuery) -> None:
    if not await callback_is_group_admin(callback):
        await callback.answer("Only this group's administrators can use these controls.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    if action == "close":
        await callback.message.delete()
        await callback.answer()
        return
    group = await db.db.groups.find_one({"chat_id": chat_id}) or {}
    if action == "search":
        group["disabled"] = not group.get("disabled", False)
        await db.db.groups.update_one({"chat_id": chat_id}, {"$set": {"disabled": group["disabled"]}}, upsert=True)
    elif action == "spam":
        group["anti_spam"] = not group.get("anti_spam", False)
        await db.db.groups.update_one({"chat_id": chat_id}, {"$set": {"anti_spam": group["anti_spam"]}}, upsert=True)
    elif action == "rules":
        await callback.answer(group.get("rules") or "No rules have been set yet.", show_alert=True)
        return
    elif action == "help":
        await callback.answer("Use /setwelcome, /setrules, /filter, /blacklist, or /settings to manage this group.", show_alert=True)
        return
    updated_group = await db.db.groups.find_one({"chat_id": chat_id}) or {}
    await callback.message.edit_text(group_settings_text(updated_group), reply_markup=group_settings_keyboard(updated_group))
    await callback.answer("Settings updated.")


@router.message(Command("clearwelcome"))
async def clear_welcome(message: Message) -> None:
    if not await require_group_admin(message):
        return
    await db.db.groups.update_one({"chat_id": message.chat.id}, {"$unset": {"welcome_message": ""}})
    await message.answer("✅ Welcome message cleared.")


@router.message(Command("setrules"))
async def set_rules(message: Message, command: CommandObject) -> None:
    if not await require_group_admin(message):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Usage: <code>/setrules Your group rules</code>")
        return
    await db.db.groups.update_one({"chat_id": message.chat.id}, {"$set": {"rules": text}}, upsert=True)
    await message.answer("✅ Rules saved.")


@router.message(Command("rules"))
async def rules(message: Message) -> None:
    group = await db.db.groups.find_one({"chat_id": message.chat.id})
    await message.answer(f"<b>Group rules</b>\n\n{group.get('rules', 'No rules have been set yet.') if group else 'No rules have been set yet.'}")


@router.message(Command("filter"))
async def add_filter(message: Message, command: CommandObject) -> None:
    if not await require_group_admin(message):
        return
    try:
        keyword, reply = (command.args or "").split("|", 1)
    except ValueError:
        await message.answer("Usage: <code>/filter keyword | reply text</code>")
        return
    keyword, reply = normalize_query(keyword).lower(), reply.strip()
    if not keyword or not reply:
        await message.answer("Both a keyword and reply are required.")
        return
    await db.db.keyword_filters.update_one(
        {"chat_id": message.chat.id, "keyword": keyword}, {"$set": {"reply": reply}}, upsert=True
    )
    await message.answer(f"✅ Auto-reply added for <code>{keyword}</code>.")


@router.message(Command("stopfilter"))
async def remove_filter(message: Message, command: CommandObject) -> None:
    if not await require_group_admin(message):
        return
    keyword = normalize_query(command.args or "").lower()
    if not keyword:
        await message.answer("Usage: <code>/stopfilter keyword</code>")
        return
    result = await db.db.keyword_filters.delete_one({"chat_id": message.chat.id, "keyword": keyword})
    await message.answer("✅ Auto-reply removed." if result.deleted_count else "That keyword has no auto-reply.")


@router.message(Command("blacklist"))
@router.message(Command("unblacklist"))
async def manage_blacklist(message: Message, command: CommandObject) -> None:
    if not await require_group_admin(message):
        return
    word = normalize_query(command.args or "").lower()
    if not word:
        await message.answer(f"Usage: <code>/{command.command} word or phrase</code>")
        return
    if command.command == "blacklist":
        await db.db.blacklist.update_one({"chat_id": message.chat.id, "word": word}, {"$set": {"word": word}}, upsert=True)
        await message.answer(f"✅ Blocked: <code>{word}</code>")
    else:
        result = await db.db.blacklist.delete_one({"chat_id": message.chat.id, "word": word})
        await message.answer("✅ Removed from blacklist." if result.deleted_count else "That word was not blocked.")


@router.message(Command("antispam"))
async def anti_spam(message: Message, command: CommandObject) -> None:
    if not await require_group_admin(message):
        return
    choice = (command.args or "").lower()
    if choice not in {"on", "off"}:
        await message.answer("Usage: <code>/antispam on</code> or <code>/antispam off</code>")
        return
    await db.db.groups.update_one({"chat_id": message.chat.id}, {"$set": {"anti_spam": choice == "on"}}, upsert=True)
    await message.answer(f"✅ Anti-spam is now <b>{choice}</b>.")


@router.message(Command("disable"))
@router.message(Command("enable"))
async def toggle_group_search(message: Message, command: CommandObject) -> None:
    if not await require_group_admin(message):
        return
    disabled = command.command == "disable"
    await db.db.groups.update_one({"chat_id": message.chat.id}, {"$set": {"disabled": disabled}}, upsert=True)
    await message.answer(f"✅ Search is now <b>{'disabled' if disabled else 'enabled'}</b> in this group.")


@router.message(F.new_chat_members)
async def welcome_new_members(message: Message) -> None:
    group = await db.db.groups.find_one({"chat_id": message.chat.id})
    template = group.get("welcome_message") if group else None
    if not template:
        return
    for user in message.new_chat_members:
        if not user.is_bot:
            text = template.replace("{name}", user.full_name).replace("{group}", message.chat.title or "this group")
            await message.answer(text)


@router.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def private_search(message: Message) -> None:
    if await maintenance_active(message.from_user.id):
        await message.answer((await maintenance_settings())["message"])
        return
    if not await pm_search_enabled():
        await message.answer("Private search is currently disabled. Please use AkMovieVerse in a group.")
        return
    if await db.db.users.find_one({"user_id": message.from_user.id, "banned": True}):
        return
    if len(normalize_query(message.text)) < 2:
        await message.answer("Send at least two characters to search the library.")
        return
    await render_results(message, message.from_user.id, message.text, 0)


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text, ~F.text.startswith("/"))
async def group_search(message: Message) -> None:
    if await maintenance_active(message.from_user.id):
        return
    group = await db.db.groups.find_one({"chat_id": message.chat.id}) or {}
    normalized = normalize_query(message.text)
    lower_text = normalized.lower()
    blocked = await db.db.blacklist.find_one({"chat_id": message.chat.id, "word": {"$in": [lower_text]}})
    if not blocked:
        blocked_words = await db.db.blacklist.find({"chat_id": message.chat.id}).to_list(length=100)
        blocked = next((item for item in blocked_words if item["word"] in lower_text), None)
    if blocked:
        try:
            await message.delete()
        except Exception:
            pass
        return
    if group.get("anti_spam") and not await is_group_admin(message):
        now = message.date.timestamp()
        key = (message.chat.id, message.from_user.id)
        if now - recent_messages.get(key, 0) < 1.2:
            try:
                await message.delete()
            except Exception:
                pass
            return
        recent_messages[key] = now
    await react_to_group_message(message)
    keyword_filter = await db.db.keyword_filters.find_one({"chat_id": message.chat.id, "keyword": lower_text})
    if keyword_filter:
        await message.reply(keyword_filter["reply"])
        return
    if group.get("disabled"):
        return
    if await db.db.users.find_one({"user_id": message.from_user.id, "banned": True}):
        return
    if not await subscription_ok(message.bot, message.from_user.id):
        await message.reply(translate(await user_language(message.from_user.id), "join_required"), reply_markup=await force_sub_keyboard())
        return
    if len(normalized) < 2:
        return
    await render_results(message, message.from_user.id, message.text, 0)


@router.message(Command("ban"))
@router.message(Command("unban"))
async def manage_user(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        user_id = int(command.args or "")
    except ValueError:
        await message.answer(f"Usage: <code>/{command.command} USER_ID</code>")
        return
    banned = command.command == "ban"
    await db.db.users.update_one({"user_id": user_id}, {"$set": {"user_id": user_id, "banned": banned}}, upsert=True)
    await message.answer(f"✅ User <code>{user_id}</code> {'banned' if banned else 'unbanned'}.")


@router.message(Command("closerequest"))
async def close_request(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        request_id = ObjectId(command.args or "")
    except Exception:
        await message.answer("Usage: <code>/closerequest REQUEST_ID</code>")
        return
    result = await db.db.requests.update_one({"_id": request_id}, {"$set": {"status": "closed", "closed_at": datetime.now(timezone.utc)}})
    await message.answer("✅ Request closed." if result.matched_count else "Request not found.")


@router.message(Command("broadcast"))
async def broadcast(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Usage: <code>/broadcast Your message</code>")
        return
    sent, failed = await deliver_broadcast(message.bot, text)
    await message.answer(f"<b>Broadcast complete</b>\nSent: {sent}\nFailed: {failed}")


async def deliver_broadcast(bot: Bot, text: str) -> tuple[int, int]:
    sent = failed = 0
    async for user in db.db.users.find({"banned": {"$ne": True}}):
        try:
            await bot.send_message(user["user_id"], text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)
    return sent, failed


@router.message(Command("schedule"))
async def schedule_broadcast(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        when, text = (command.args or "").split("|", 1)
        local_time = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(settings.timezone))
        text = text.strip()
        if not text or local_time <= datetime.now(ZoneInfo(settings.timezone)):
            raise ValueError
    except ValueError:
        await message.answer("Usage: <code>/schedule 2026-09-03 18:30 | Your message</code>\nTime uses your configured timezone.")
        return
    result = await db.db.scheduled_broadcasts.insert_one({
        "text": text,
        "due_at": local_time.astimezone(timezone.utc),
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    })
    await message.answer(f"✅ Scheduled for <b>{local_time:%d %b %Y, %I:%M %p}</b>.\nID: <code>{result.inserted_id}</code>")


@router.message(Command("schedules"))
async def list_schedules(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    schedules = await db.db.scheduled_broadcasts.find({"status": "pending"}).sort("due_at", 1).to_list(length=30)
    local_zone = ZoneInfo(settings.timezone)
    details = "\n".join(f"• <code>{item['_id']}</code> — {item['due_at'].astimezone(local_zone):%d %b %Y, %I:%M %p}" for item in schedules)
    await message.answer(f"<b>Scheduled broadcasts</b>\n{details or 'No messages scheduled.'}")


@router.message(Command("cancelschedule"))
async def cancel_schedule(message: Message, command: CommandObject) -> None:
    if not is_owner(message.from_user.id):
        return
    try:
        schedule_id = ObjectId(command.args or "")
    except Exception:
        await message.answer("Usage: <code>/cancelschedule SCHEDULE_ID</code>")
        return
    result = await db.db.scheduled_broadcasts.update_one(
        {"_id": schedule_id, "status": "pending"}, {"$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc)}}
    )
    await message.answer("✅ Scheduled broadcast cancelled." if result.modified_count else "That pending schedule was not found.")


async def scheduled_broadcast_worker(bot: Bot) -> None:
    while True:
        now = datetime.now(timezone.utc)
        schedule = await db.db.scheduled_broadcasts.find_one_and_update(
            {"status": "pending", "due_at": {"$lte": now}},
            {"$set": {"status": "sending", "started_at": now}},
            sort=[("due_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        if schedule:
            sent, failed = await deliver_broadcast(bot, schedule["text"])
            await db.db.scheduled_broadcasts.update_one(
                {"_id": schedule["_id"]},
                {"$set": {"status": "sent", "sent_at": datetime.now(timezone.utc), "sent_count": sent, "failed_count": failed}},
            )
            try:
                await bot.send_message(settings.owner_id, f"✅ Scheduled broadcast sent.\nSent: {sent}\nFailed: {failed}")
            except Exception:
                pass
        await asyncio.sleep(20)


@router.callback_query(F.data.startswith("page:"))
async def paginate(callback: CallbackQuery) -> None:
    _, session_id, raw_category, raw_page = callback.data.split(":")
    session = await db.get_session(session_id)
    if not session or session["user_id"] != callback.from_user.id:
        await callback.answer("This search expired. Please search again.", show_alert=True)
        return
    category = None if raw_category == "all" else raw_category
    page_size = await results_per_page_for(callback.from_user.id)
    files, total = await db.search(session["query"], int(raw_page), page_size, category)
    ad_text, keyboard = await add_ad_to_results(result_keyboard(files, session_id, int(raw_page), total, page_size, category), callback.from_user.id)
    await callback.message.edit_text(
        f"<b>Results for:</b> {session['query']}{f' · {category.title()}' if category else ''}{ad_text}",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("filter:"))
async def filter_results(callback: CallbackQuery) -> None:
    _, session_id, category = callback.data.split(":")
    session = await db.get_session(session_id)
    if not session or session["user_id"] != callback.from_user.id:
        await callback.answer("This search expired. Please search again.", show_alert=True)
        return
    page_size = await results_per_page_for(callback.from_user.id)
    files, total = await db.search(session["query"], 0, page_size, category)
    if not files:
        await callback.answer(f"No {category} results for this search.", show_alert=True)
        return
    ad_text, keyboard = await add_ad_to_results(result_keyboard(files, session_id, 0, total, page_size, category), callback.from_user.id)
    await callback.message.edit_text(
        f"<b>Results for:</b> {session['query']} · {category.title()}{ad_text}",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("file:"))
async def prepare_download(callback: CallbackQuery) -> None:
    if await maintenance_active(callback.from_user.id):
        await callback.answer((await maintenance_settings())["message"], show_alert=True)
        return
    file = await db.db.files.find_one({"_id": ObjectId(callback.data.split(":", 1)[1])})
    if not file:
        await callback.answer("This file is unavailable.", show_alert=True)
        return
    if not await subscription_ok(callback.bot, callback.from_user.id):
        await callback.message.answer(translate(await user_language(callback.from_user.id), "join_required"), reply_markup=await force_sub_keyboard())
        await callback.answer()
        return
    if not await require_verification(callback):
        await callback.answer()
        return
    token = await db.make_download_token(str(file["_id"]), callback.from_user.id, await delivery_token_minutes(callback.from_user.id))
    language = await user_language(callback.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=await delivery_button_text(callback.from_user.id, language), callback_data=f"send:{token}")
    ]])
    await callback.message.answer(await delivery_ready_text(callback.from_user.id, language), reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("send:"))
async def send_file(callback: CallbackQuery) -> None:
    if await maintenance_active(callback.from_user.id):
        await callback.answer((await maintenance_settings())["message"], show_alert=True)
        return
    token = await db.get_download_token(callback.data.split(":", 1)[1], callback.from_user.id)
    if not token:
        await callback.answer("This delivery link expired or is unavailable.", show_alert=True)
        return
    if not await subscription_ok(callback.bot, callback.from_user.id):
        await callback.message.answer(translate(await user_language(callback.from_user.id), "join_required"), reply_markup=await force_sub_keyboard())
        await callback.answer()
        return
    if not await require_verification(callback):
        await callback.answer()
        return
    file = await db.db.files.find_one({"_id": ObjectId(token["file_id"])})
    try:
        delivery_caption = await custom_delivery_caption(file)
        delivered = await callback.bot.copy_message(
            callback.from_user.id,
            file["source_chat_id"],
            file["source_message_id"],
            protect_content=(await delivery_settings())["protect_content"],
            caption=delivery_caption,
            parse_mode=ParseMode.HTML if delivery_caption else None,
        )
        asyncio.create_task(delete_message_later(callback.bot, callback.from_user.id, delivered.message_id))
    except Exception:
        await callback.answer("Start the bot in private chat first, then try again.", show_alert=True)
        return
    await callback.answer("Sent to your private chat!")


@router.callback_query(F.data.startswith("request:"))
async def request_missing(callback: CallbackQuery) -> None:
    if not await request_system_enabled():
        await callback.answer("Requests are currently disabled by the owner.", show_alert=True)
        return
    session = await db.get_session(callback.data.split(":", 1)[1])
    if not session or session["user_id"] != callback.from_user.id:
        await callback.answer("This request expired.", show_alert=True)
        return
    existing = await db.db.requests.find_one({"query": session["query"], "status": "open"})
    existing_requesters = (existing or {}).get("requesters") or ([(existing or {}).get("user_id")] if (existing or {}).get("user_id") else [])
    is_new_requester = callback.from_user.id not in existing_requesters
    await db.db.requests.update_one(
        {"query": session["query"], "status": "open"},
        {
            "$setOnInsert": {"query": session["query"], "created_at": datetime.now(timezone.utc), "status": "open"},
            "$addToSet": {"requesters": callback.from_user.id},
            "$set": {"last_requested_at": datetime.now(timezone.utc)},
        },
        upsert=True,
    )
    request = await db.db.requests.find_one({"query": session["query"], "status": "open"})
    count = len(request.get("requesters", [])) if request else 1
    if is_new_requester:
        try:
            await callback.bot.send_message(
                settings.owner_id,
                f"📩 <b>New file request</b>\nQuery: <b>{session['query']}</b>\nRequested by: {count} user(s)\n\nAdd a matching authorized file to a source channel to notify requesters automatically.",
            )
        except Exception:
            pass
    await callback.answer(f"Request saved. {count} user(s) requested this.", show_alert=True)


@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    await message.answer(await owner_stats_text(), reply_markup=panel_keyboard())


@router.message(Command("analytics"))
async def analytics(message: Message) -> None:
    if is_owner(message.from_user.id):
        await message.answer(await analytics_text())
        return
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP} or not await is_group_admin(message):
        await message.answer("Only the bot owner or an administrator of this group can view analytics.")
        return
    await message.answer(await analytics_text(message.chat.id))


async def main() -> None:
    global bot_username, db, settings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    db = Database(settings.mongodb_uri, settings.mongodb_database)
    await db.connect()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot_username = (await bot.get_me()).username
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    scheduler = asyncio.create_task(scheduled_broadcast_worker(bot))
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
