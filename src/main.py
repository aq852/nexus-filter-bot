import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from bson import ObjectId

from .config import Settings, get_settings
from .database import Database
from .utils import media_category, media_kind, message_file, normalize_query

router = Router()
db: Database
settings: Settings


def is_owner(user_id: int) -> bool:
    return user_id == settings.owner_id


def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Live stats", callback_data="panel:stats"),
         InlineKeyboardButton(text="📁 Source channels", callback_data="panel:sources")],
        [InlineKeyboardButton(text="📩 Open requests", callback_data="panel:requests"),
         InlineKeyboardButton(text="🔎 Top searches", callback_data="panel:searches")],
        [InlineKeyboardButton(text="📣 Broadcast help", callback_data="panel:broadcast"),
         InlineKeyboardButton(text="🛡 User controls", callback_data="panel:users")],
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


async def render_results(message: Message, user_id: int, query: str, page: int, session_id: str | None = None, category: str | None = None) -> None:
    clean_query = normalize_query(query)
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
        sent = await message.answer(f"No results for <b>{clean_query}</b>.{hint}", reply_markup=keyboard)
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
async def start(message: Message) -> None:
    await db.db.users.update_one(
        {"user_id": message.from_user.id},
        {"$set": {"user_id": message.from_user.id, "name": message.from_user.full_name, "last_seen": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await message.answer("Welcome to <b>NexusFilterBot</b>. Add me to a group, then send a file name or title to search the shared library.")
    popular = await db.top_searches(5)
    if popular:
        await message.answer("<b>Popular searches</b>\n" + "\n".join(f"• {item['query']}" for item in popular))


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
        details = "\n".join(f"• <code>{item['_id']}</code> — {item['query']}" for item in requests)
        text = f"<b>Open requests ({len(requests)})</b>\n{details or 'No pending requests.'}\n\nClose one: <code>/closerequest REQUEST_ID</code>"
    elif action == "searches":
        searches = await db.top_searches()
        details = "\n".join(f"• {item['query']} — <b>{item['count']}</b>" for item in searches)
        text = f"<b>Top searches</b>\n{details or 'No searches recorded yet.'}"
    elif action == "broadcast":
        text = "<b>Broadcast</b>\n\nSend <code>/broadcast Your message here</code>. The bot delivers it to every user who has started it."
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
    await db.upsert_file(record)


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text, ~F.text.startswith("/"))
async def group_search(message: Message) -> None:
    if await db.db.groups.find_one({"chat_id": message.chat.id, "disabled": True}):
        return
    if await db.db.users.find_one({"user_id": message.from_user.id, "banned": True}):
        return
    if not await subscription_ok(message.bot, message.from_user.id):
        await message.reply("Join the required updates channel first, then try again.")
        return
    if len(normalize_query(message.text)) < 2:
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
    sent = failed = 0
    async for user in db.db.users.find({"banned": {"$ne": True}}):
        try:
            await message.bot.send_message(user["user_id"], text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)
    await message.answer(f"<b>Broadcast complete</b>\nSent: {sent}\nFailed: {failed}")


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
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📥 Send to my DM (10 min)", callback_data=f"send:{token}")
    ]])
    await callback.message.answer("Your private delivery link is ready for 10 minutes.", reply_markup=keyboard)
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
    await db.db.requests.update_one(
        {"query": session["query"], "user_id": callback.from_user.id},
        {"$setOnInsert": {"query": session["query"], "user_id": callback.from_user.id, "created_at": datetime.now(timezone.utc), "status": "open"}},
        upsert=True,
    )
    await callback.answer("Request saved. The owner can review it.", show_alert=True)


@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    await message.answer(await owner_stats_text(), reply_markup=panel_keyboard())


async def main() -> None:
    global db, settings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    db = Database(settings.mongodb_uri, settings.mongodb_database)
    await db.connect()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
