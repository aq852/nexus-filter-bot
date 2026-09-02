import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=name, callback_data=f"language:{code}") for code, name in LANGUAGES.items()
    ]])


def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Live stats", callback_data="panel:stats"),
         InlineKeyboardButton(text="📁 Source channels", callback_data="panel:sources")],
        [InlineKeyboardButton(text="📩 Open requests", callback_data="panel:requests"),
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
    return f"<b>Nexus control panel</b>\n\nFiles: <b>{files}</b>\nUsers: <b>{users}</b>\nSource channels: <b>{sources}</b>\nOpen requests: <b>{requests}</b>"


async def subscription_ok(bot: Bot, user_id: int) -> bool:
    if not settings.force_sub_channel_id:
        return True
    try:
        member = await bot.get_chat_member(settings.force_sub_channel_id, user_id)
        return member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    except Exception:
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


def result_keyboard(files: list[dict], session_id: str, page: int, total: int, category: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{item['kind']} · {item['name'][:46]}", callback_data=f"file:{item['_id']}")]
        for item in files
    ]
    rows.append([InlineKeyboardButton(text=label, callback_data=f"filter:{session_id}:{value}") for label, value in FILTERS])
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton(text="‹ Prev", callback_data=f"page:{session_id}:{category or 'all'}:{page - 1}"))
    if (page + 1) * settings.results_per_page < total:
        navigation.append(InlineKeyboardButton(text="Next ›", callback_data=f"page:{session_id}:{category or 'all'}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def delete_later(message: Message) -> None:
    if settings.auto_delete_seconds:
        await asyncio.sleep(settings.auto_delete_seconds)
        try:
            await message.delete()
        except Exception:
            pass


async def announce_new_file(bot: Bot, record: dict, file_id: str) -> None:
    """Publish a compact update only when a file is first added to the index."""
    if not settings.updates_channel_id:
        return
    tags = " ".join(f"#{tag}" for tag in record.get("tags", [])[:8])
    caption_preview = record.get("caption", "").strip()[:250]
    text = f"<b>New in Nexus</b>\n\n{record['kind']} <b>{record['name']}</b>"
    if caption_preview:
        text += f"\n\n{caption_preview}"
    if tags:
        text += f"\n\n{tags}"
    keyboard = None
    if bot_username:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔎 Open in NexusFilterBot", url=f"https://t.me/{bot_username}?start=file_{file_id}")
        ]])
    try:
        await bot.send_message(settings.updates_channel_id, text, reply_markup=keyboard)
    except Exception as error:
        logging.warning("Could not publish new-file announcement: %s", error)


async def render_results(message: Message, user_id: int, query: str, page: int, session_id: str | None = None, category: str | None = None) -> None:
    clean_query = normalize_query(query)
    language = await user_language(user_id)
    await db.record_search(clean_query)
    files, total = await db.search(clean_query, page, settings.results_per_page, category)
    if not session_id:
        session_id = await db.save_search_session(user_id, message.chat.id, clean_query)
    if not files:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📩 Request this", callback_data=f"request:{session_id}")
        ]])
        suggestions = await db.suggestions(clean_query)
        hint = "\n<b>Did you mean:</b> " + " • ".join(suggestions) if suggestions else ""
        sent = await message.answer(f"{translate(language, 'no_results', query=clean_query)}{hint}", reply_markup=keyboard)
        asyncio.create_task(delete_later(sent))
        return
    first = page * settings.results_per_page + 1
    last = first + len(files) - 1
    filter_label = f" · {category.title()}" if category else ""
    sent = await message.answer(
        f"<b>Results for:</b> {clean_query}{filter_label}\nShowing {first}–{last} of {total}.\n\nChoose a type to narrow results, or tap a result for private delivery.",
        reply_markup=result_keyboard(files, session_id, page, total, category),
    )
    asyncio.create_task(delete_later(sent))


@router.message(Command("start"))
async def start(message: Message, command: CommandObject) -> None:
    await db.db.users.update_one(
        {"user_id": message.from_user.id},
        {"$set": {"user_id": message.from_user.id, "name": message.from_user.full_name, "last_seen": datetime.now(timezone.utc)}},
        upsert=True,
    )
    language = await user_language(message.from_user.id)
    payload = command.args or ""
    if payload.startswith("file_"):
        try:
            file = await db.db.files.find_one({"_id": ObjectId(payload.removeprefix("file_"))})
        except Exception:
            file = None
        if not file:
            await message.answer("That inline result is no longer available. Please search again.")
            return
        if not await subscription_ok(message.bot, message.from_user.id):
            await message.answer(translate(language, "join_required"))
            return
        token = await db.make_download_token(str(file["_id"]), message.from_user.id)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=translate(language, "send_to_dm"), callback_data=f"send:{token}")
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
                message_text=f"<b>{file['name']}</b>\nPrivate delivery is available from NexusFilterBot.",
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
    index_result = await db.upsert_file(record)
    stored_file = await db.db.files.find_one(
        {"source_chat_id": message.chat.id, "source_message_id": message.message_id}, {"_id": 1}
    )
    if not stored_file:
        return
    stored_file_id = str(stored_file["_id"])
    if index_result.upserted_id:
        await announce_new_file(message.bot, record, stored_file_id)
    matching_requests = await db.matching_requests(record["search_text"])
    for request in matching_requests:
        requester_ids = request.get("requesters") or ([request["user_id"]] if request.get("user_id") else [])
        delivered = 0
        for user_id in requester_ids:
            token = await db.make_download_token(stored_file_id, user_id)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📥 Send to my DM (10 min)", callback_data=f"send:{token}")
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
    index_result = await db.upsert_file(record)
    stored_file = await db.db.files.find_one(
        {"source_chat_id": storage.id, "source_message_id": copied.message_id}, {"_id": 1}
    )
    if index_result.upserted_id and stored_file:
        await announce_new_file(message.bot, record, str(stored_file["_id"]))
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


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text, ~F.text.startswith("/"))
async def group_search(message: Message) -> None:
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
    keyword_filter = await db.db.keyword_filters.find_one({"chat_id": message.chat.id, "keyword": lower_text})
    if keyword_filter:
        await message.reply(keyword_filter["reply"])
        return
    if group.get("disabled"):
        return
    if await db.db.users.find_one({"user_id": message.from_user.id, "banned": True}):
        return
    if not await subscription_ok(message.bot, message.from_user.id):
        await message.reply(translate(await user_language(message.from_user.id), "join_required"))
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
    files, total = await db.search(session["query"], int(raw_page), settings.results_per_page, category)
    await callback.message.edit_text(
        f"<b>Results for:</b> {session['query']}{f' · {category.title()}' if category else ''}",
        reply_markup=result_keyboard(files, session_id, int(raw_page), total, category),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("filter:"))
async def filter_results(callback: CallbackQuery) -> None:
    _, session_id, category = callback.data.split(":")
    session = await db.get_session(session_id)
    if not session or session["user_id"] != callback.from_user.id:
        await callback.answer("This search expired. Please search again.", show_alert=True)
        return
    files, total = await db.search(session["query"], 0, settings.results_per_page, category)
    if not files:
        await callback.answer(f"No {category} results for this search.", show_alert=True)
        return
    await callback.message.edit_text(
        f"<b>Results for:</b> {session['query']} · {category.title()}",
        reply_markup=result_keyboard(files, session_id, 0, total, category),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("file:"))
async def prepare_download(callback: CallbackQuery) -> None:
    file = await db.db.files.find_one({"_id": ObjectId(callback.data.split(":", 1)[1])})
    if not file or not await subscription_ok(callback.bot, callback.from_user.id):
        await callback.answer("This file is unavailable or your subscription check failed.", show_alert=True)
        return
    token = await db.make_download_token(str(file["_id"]), callback.from_user.id)
    language = await user_language(callback.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=translate(language, "send_to_dm"), callback_data=f"send:{token}")
    ]])
    await callback.message.answer(translate(language, "delivery_ready"), reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("send:"))
async def send_file(callback: CallbackQuery) -> None:
    token = await db.get_download_token(callback.data.split(":", 1)[1], callback.from_user.id)
    if not token or not await subscription_ok(callback.bot, callback.from_user.id):
        await callback.answer("This delivery link expired or is unavailable.", show_alert=True)
        return
    file = await db.db.files.find_one({"_id": ObjectId(token["file_id"])})
    try:
        await callback.bot.copy_message(callback.from_user.id, file["source_chat_id"], file["source_message_id"])
    except Exception:
        await callback.answer("Start the bot in private chat first, then try again.", show_alert=True)
        return
    await callback.answer("Sent to your private chat!")


@router.callback_query(F.data.startswith("request:"))
async def request_missing(callback: CallbackQuery) -> None:
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
