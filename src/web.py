"""Public verification callback service. Deploy this module as a Koyeb Web service."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .config import get_settings
from .database import Database


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_settings()
    app.state.db = Database(config.mongodb_uri, config.mongodb_database)
    await app.state.db.connect()
    yield
    await app.state.db.close()


app = FastAPI(title="Nexus verification", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/verify/{token}", response_class=HTMLResponse)
async def verify(token: str) -> str:
    verification = await app.state.db.complete_verification_token(token)
    if not verification:
        raise HTTPException(status_code=404, detail="This verification link has expired or is invalid.")
    await app.state.db.db.users.update_one(
        {"user_id": verification["user_id"]},
        {"$set": {
            "verified_until": datetime.now(timezone.utc) + timedelta(minutes=verification.get("verification_minutes", 720)),
            "verified_at": verification["completed_at"],
            "verification_provider": verification["provider"],
        }},
        upsert=True,
    )
    return "<h2>Verification complete</h2><p>Return to Telegram and try your file delivery again.</p>"
