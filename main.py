import os
from typing import List, Optional, Literal
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Depends, Header, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
import httpx

import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI(title="Social Aggregator Minimal API", version="0.3.1")


# ==== Настройки и хелперы =====================================================

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


# ==== Подключение к PostgreSQL ===============================================

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Если упадёт здесь — значит не настроена переменная окружения на Render
    raise RuntimeError("DATABASE_URL is not set")


def get_db_connection():
    """
    Открывает новое подключение к БД.
    Для небольших нагрузок этого достаточно.
    """
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    """
    Создаёт таблицу incoming_messages, если её ещё нет.
    Вызывается один раз при старте сервиса.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS incoming_messages (
            id SERIAL PRIMARY KEY,
            update_id BIGINT UNIQUE NOT NULL,
            chat_id BIGINT NOT NULL,
            from_id BIGINT,
            from_name TEXT,
            text TEXT NOT NULL,
            date TIMESTAMPTZ NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'new'
        );
        """
    )
    conn.commit()
    cur.close()
    conn.close()


init_db()


# ==== Модели ==================================================================

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


class InboxMessage(BaseModel):
    id: int
    chat_id: int
    from_id: Optional[int] = None
    from_name: Optional[str] = None
    text: str
    date: datetime
    status: str


# ==== Стандартные эндпоинты ===================================================

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
# Оба пути работают: /api/posts/publish и /api/publish
@app.post(
    "/api/posts/publish",
    response_model=PublishResponse,
    dependencies=[Depends(verify_api_key)],
)
@app.post(
    "/api/publish",
    response_model=PublishResponse,
    dependencies=[Depends(verify_api_key)],
)
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


# ==== Сохранение входящих сообщений из Telegram ===============================

def save_incoming_update(update: dict):
    """
    Разбирает апдейт Telegram и сохраняет текстовое сообщение в БД.
    Поддерживает message / channel_post.
    """
    message = (
        update.get("message")
        or update.get("channel_post")
        or update.get("edited_message")
        or update.get("edited_channel_post")
    )
    if not message:
        # Не текстовый апдейт — просто игнорируем
        return

    update_id = update.get("update_id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    from_user = message.get("from") or {}
    from_id = from_user.get("id")

    first_name = from_user.get("first_name") or ""
    last_name = from_user.get("last_name") or ""
    username = from_user.get("username") or ""

    name_parts = [p for p in [first_name, last_name] if p]
    from_name = " ".join(name_parts) or username or None

    text = message.get("text") or message.get("caption")
    if not text:
        # Сообщение без текста/подписи нам пока неинтересно
        return

    ts = message.get("date")
    if ts:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        dt = datetime.now(timezone.utc)

    if update_id is None or chat_id is None:
        # Без этих полей не сможем корректно сохранить
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO incoming_messages
                (update_id, chat_id, from_id, from_name, text, date, status)
            VALUES
                (%(update_id)s, %(chat_id)s, %(from_id)s, %(from_name)s, %(text)s, %(date)s, %(status)s)
            ON CONFLICT (update_id) DO NOTHING;
            """,
            {
                "update_id": update_id,
                "chat_id": chat_id,
                "from_id": from_id,
                "from_name": from_name,
                "text": text,
                "date": dt,
                "status": "new",
            },
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


# Webhook для Telegram (оставляем открытым — Telegram не присылает наш заголовок)
@app.post("/api/webhooks/telegram")
async def telegram_webhook(req: Request):
    data = await req.json()
    print("Telegram webhook:", data)

    try:
        save_incoming_update(data)
    except Exception as e:
        # Не роняем вебхук, просто логируем
        print("Error while saving incoming Telegram message:", e)

    return {"ok": True}


# ==== API для получения и изменения входящих сообщений ========================

@app.get(
    "/api/inbox",
    response_model=List[InboxMessage],
    dependencies=[Depends(verify_api_key)],
)
def get_inbox(
    limit: int = Query(50, ge=1, le=200),
    chat_id: Optional[int] = Query(None),
):
    """
    Возвращает последние входящие сообщения.
    - limit: сколько сообщений (по умолчанию 50, максимум 200)
    - chat_id: опционально — фильтр по id чата
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if chat_id is not None:
            cur.execute(
                """
                SELECT id, chat_id, from_id, from_name, text, date, status
                FROM incoming_messages
                WHERE chat_id = %(chat_id)s
                ORDER BY date DESC
                LIMIT %(limit)s;
                """,
                {"chat_id": chat_id, "limit": limit},
            )
        else:
            cur.execute(
                """
                SELECT id, chat_id, from_id, from_name, text, date, status
                FROM incoming_messages
                ORDER BY date DESC
                LIMIT %(limit)s;
                """,
                {"limit": limit},
            )

        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    # rows — это список dict благодаря RealDictCursor
    return [InboxMessage(**row) for row in rows]


@app.post(
    "/api/inbox/{message_id}/read",
    dependencies=[Depends(verify_api_key)],
)
def mark_inbox_read(message_id: int):
    """
    Помечает сообщение как прочитанное: status = 'read' по id.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE incoming_messages
            SET status = 'read'
            WHERE id = %(id)s;
            """,
            {"id": message_id},
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Message not found")
        conn.commit()
    finally:
        cur.close()
        conn.close()

    return {"status": "ok"}
