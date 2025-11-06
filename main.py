import os
from typing import List, Optional, Literal
from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
import httpx

app = FastAPI(title="Social Aggregator Minimal API", version="0.2.2")

def get_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN is not set")
    return token

def get_publish_secret() -> str:
    secret = os.getenv("PUBLISH_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="PUBLISH_SECRET is not set")
    return secret

# Проверка ключа доступа: ТОЛЬКО через заголовок X-Api-Key
async def verify_api_key(x_api_key: Optional[str] = Header(default=None)):
    secret = get_publish_secret()
    if x_api_key != secret:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing X-Api-Key")

async def tg_send_message(chat_id: str, text: str):
    token = get_token()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, json=payload)
        if r.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Telegram error: {r.text}")
        return r.json()

# Root
@app.get("/")
def root():
    return {"status": "ok", "service": "Social Aggregator Minimal API"}

@app.head("/")
def root_head():
    return PlainTextResponse("", status_code=200)

# Health endpoint (GET и HEAD)
@app.get("/health")
def health():
    return {"status": "healthy"}

@app.head("/health")
def health_head():
    return PlainTextResponse("", status_code=200)

# Пустая лента (зарезервировано под будущее)
@app.get("/api/feed")
def feed():
    return []

# Тестовая отправка в TG (требует X-Api-Key)
@app.get("/api/telegram/send", dependencies=[Depends(verify_api_key)])
async def telegram_send(chat_id: str, text: str):
    result = await tg_send_message(chat_id=chat_id, text=text)
    return {"ok": True, "result": result}

# Унифицированная публикация (поддерживает только TG пока) — защищено X-Api-Key
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
    sourceId: str  # для TG — это chat_id канала или @username

class PublishRequest(BaseModel):
    targets: List[Target]
    content: Content

class PublishResponse(BaseModel):
    status: str

@app.post("/api/posts/publish", response_model=PublishResponse, dependencies=[Depends(verify_api_key)])
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

# Webhook для Telegram (оставляем открытым — Telegram не присылает наш заголовок)
@app.post("/api/webhooks/telegram")
async def telegram_webhook(req: Request):
    data = await req.json()
    print("Telegram webhook:", data)
    return {"ok": True}
