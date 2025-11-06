import os
from typing import List, Optional, Literal
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import httpx

app = FastAPI(title="Social Aggregator Minimal API", version="0.1.0")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

def ensure_token():
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN is not set")

async def tg_send_message(chat_id: str, text: str):
    ensure_token()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, json=payload)
        if r.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Telegram error: {r.text}")
        return r.json()

@app.get("/")
def root():
    return {"status": "ok", "service": "Social Aggregator Minimal API"}

@app.get("/api/feed")
def feed():
    return []

@app.get("/api/telegram/send")
async def telegram_send(chat_id: str, text: str):
    result = await tg_send_message(chat_id=chat_id, text=text)
    return {"ok": True, "result": result}

Provider = Literal["tg"]

class Attachment(BaseModel):
    type: Literal["image", "video", "link"]
    url: str
    thumb: Optional[str] = None

class Content(BaseModel):
    text: str
    media: List[Attachment] = []

class Target(BaseModel):
    provider: Provider
    sourceId: str

class PublishRequest(BaseModel):
    targets: List[Target]
    content: Content

class PublishResponse(BaseModel):
    status: str

@app.post("/api/posts/publish", response_model=PublishResponse)
async def publish(req: PublishRequest):
       errors = []
    for t in req.targets:
        if t.provider == "tg":
            try:
                await tg_send_message(chat_id=t.sourceId, text=req.content.text)
            except HTTPException as e:
                errors.append({"provider": "tg", "error": str(e.detail)})
        else:
            errors.append({"provider": t.provider, "error": "not implemented"})
    if errors:
        raise HTTPException(status_code=500, detail={"errors": errors})
    return PublishResponse(status="ok")

@app.post("/api/webhooks/telegram")
async def telegram_webhook(req: Request):
    data = await req.json()
    print("Telegram webhook:", data)
    return {"ok": True}
