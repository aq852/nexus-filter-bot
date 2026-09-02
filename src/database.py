from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from .utils import normalize_query

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase


class Database:
    def __init__(self, uri: str, name: str) -> None:
        self.client = AsyncIOMotorClient(uri)
        self.db: AsyncIOMotorDatabase = self.client[name]

    async def connect(self) -> None:
        await self.db.command("ping")
        await self.db.files.create_index([("search_text", "text")])
        await self.db.files.create_index([("source_chat_id", 1), ("source_message_id", 1)], unique=True)
        await self.db.users.create_index("user_id", unique=True)
        await self.db.source_channels.create_index("chat_id", unique=True)
        await self.db.search_sessions.create_index("created_at", expireAfterSeconds=3600)
        await self.db.download_tokens.create_index("expires_at", expireAfterSeconds=0)
        await self.db.searches.create_index("query", unique=True)
        await self.db.keyword_filters.create_index([("chat_id", 1), ("keyword", 1)], unique=True)
        await self.db.blacklist.create_index([("chat_id", 1), ("word", 1)], unique=True)
        await self.db.scheduled_broadcasts.create_index([("status", 1), ("due_at", 1)])

    async def close(self) -> None:
        self.client.close()

    async def is_source_channel(self, chat_id: int) -> bool:
        return await self.db.source_channels.find_one({"chat_id": chat_id}) is not None

    async def add_source_channel(self, chat_id: int, title: str | None) -> None:
        await self.db.source_channels.update_one(
            {"chat_id": chat_id}, {"$set": {"title": title, "added_at": datetime.now(timezone.utc)}}, upsert=True
        )

    async def upsert_file(self, record: dict) -> None:
        await self.db.files.update_one(
            {"source_chat_id": record["source_chat_id"], "source_message_id": record["source_message_id"]},
            {"$set": record},
            upsert=True,
        )

    async def search(self, query: str, page: int, size: int, category: str | None = None) -> tuple[list[dict], int]:
        words = " ".join(normalized for normalized in query.split() if normalized)
        selector = {"$text": {"$search": words}} if words else {}
        if category:
            selector["category"] = category
        total = await self.db.files.count_documents(selector)
        cursor = self.db.files.find(selector, {"score": {"$meta": "textScore"}}).sort(
            [("score", {"$meta": "textScore"}), ("created_at", -1)]
        )
        return await cursor.skip(page * size).limit(size).to_list(length=size), total

    async def suggestions(self, query: str, limit: int = 3) -> list[str]:
        """Return a small set of close filename matches without a costly full-library scan."""
        candidates = await self.db.files.find({}, {"name": 1}).sort("created_at", -1).limit(250).to_list(length=250)
        scored: list[tuple[float, str]] = []
        needle = query.lower()
        for item in candidates:
            name = item.get("name", "")
            score = SequenceMatcher(None, needle, name.lower()).ratio()
            if score >= 0.45:
                scored.append((score, name))
        return [name for _, name in sorted(scored, reverse=True)[:limit]]

    async def save_search_session(self, user_id: int, chat_id: int, query: str) -> str:
        result = await self.db.search_sessions.insert_one(
            {"user_id": user_id, "chat_id": chat_id, "query": query, "created_at": datetime.now(timezone.utc)}
        )
        return str(result.inserted_id)

    async def get_session(self, session_id: str) -> dict | None:
        from bson import ObjectId
        try:
            return await self.db.search_sessions.find_one({"_id": ObjectId(session_id)})
        except Exception:
            return None

    async def make_download_token(self, file_id: str, user_id: int) -> str:
        result = await self.db.download_tokens.insert_one(
            {"file_id": file_id, "user_id": user_id, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10)}
        )
        return str(result.inserted_id)

    async def get_download_token(self, token: str, user_id: int) -> dict | None:
        from bson import ObjectId
        try:
            return await self.db.download_tokens.find_one({"_id": ObjectId(token), "user_id": user_id})
        except Exception:
            return None

    async def record_search(self, query: str) -> None:
        await self.db.searches.update_one(
            {"query": query},
            {"$inc": {"count": 1}, "$set": {"last_searched": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def top_searches(self, limit: int = 5) -> list[dict]:
        return await self.db.searches.find({}).sort("count", -1).limit(limit).to_list(length=limit)

    async def matching_requests(self, searchable_text: str, limit: int = 100) -> list[dict]:
        """Find open requests that are likely fulfilled by an indexed file."""
        haystack = normalize_query(searchable_text).lower()
        matches = []
        async for request in self.db.requests.find({"status": "open"}).limit(limit):
            query = normalize_query(request["query"]).lower()
            words = [word for word in query.split() if len(word) > 2]
            overlap = sum(word in haystack for word in words)
            ratio = SequenceMatcher(None, query, haystack).ratio()
            if query in haystack or (words and overlap / len(words) >= 0.75) or ratio >= 0.62:
                matches.append(request)
        return matches
