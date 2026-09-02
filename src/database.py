from datetime import datetime, timedelta, timezone

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

    async def search(self, query: str, page: int, size: int) -> tuple[list[dict], int]:
        words = " ".join(normalized for normalized in query.split() if normalized)
        selector = {"$text": {"$search": words}} if words else {}
        total = await self.db.files.count_documents(selector)
        cursor = self.db.files.find(selector, {"score": {"$meta": "textScore"}}).sort(
            [("score", {"$meta": "textScore"}), ("created_at", -1)]
        )
        return await cursor.skip(page * size).limit(size).to_list(length=size), total

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
