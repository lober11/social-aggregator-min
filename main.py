import os
import json
import math
from typing import List, Optional, Literal
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Depends, Header, Query, BackgroundTasks
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
import httpx

import psycopg2
from psycopg2.extras import RealDictCursor

import firebase_admin
from firebase_admin import credentials, messaging


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


# ==== Firebase Admin для FCM ==================================================

firebase_app = None


def init_firebase():
    """
    Ленивая инициализация Firebase Admin SDK.
    Берём JSON сервисного аккаунта из переменной окружения FIREBASE_SERVICE_ACCOUNT_JSON.
    """
    global firebase_app
    if firebase_app is not None:
        return

    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        print("FIREBASE_SERVICE_ACCOUNT_JSON is not set")
        return

    try:
        cred = credentials.Certificate(json.loads(sa_json))
        firebase_app = firebase_admin.initialize_app(cred)
        print("Firebase app initialized")
    except Exception as e:
        print("Error initializing Firebase:", e)
        firebase_app = None


def send_unread_count_push(unread_count: int):
    """
    Отправляет data-push в FCM с полем unread_count.
    Токен устройства берём из FCM_DEVICE_TOKEN.
    """
    init_firebase()
    if firebase_app is None:
        return

    device_token = os.environ.get("FCM_DEVICE_TOKEN")
    if not device_token:
        print("FCM_DEVICE_TOKEN is not set")
        return

    message = messaging.Message(
        data={
            "unread_count": str(unread_count)
        },
        token=device_token,
    )

    try:
        response = messaging.send(message)
        print("Successfully sent FCM message:", response)
    except Exception as e:
        print("Error sending FCM message:", e)


def get_unread_count() -> int:
    """
    Возвращает количество непрочитанных сообщений (status = 'new').
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COUNT(*) AS c FROM incoming_messages WHERE status = 'new';"
        )
        row = cur.fetchone()
        if not row:
            return 0
        return int(row.get("c", 0))
    finally:
        cur.close()
        conn.close()


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


# ==== Near alerts (радиальные сообщения) =====================================

class SendNearAlertRequest(BaseModel):
    text: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class NearbyAlert(BaseModel):
    id: int
    text: str
    authorName: Optional[str] = None
    distanceMeters: Optional[int] = None
    createdAt: str
    # внутренние поля — можно не использовать на клиенте
    latitude: Optional[float] = None
    longitude: Optional[float] = None


_near_alerts: List[NearbyAlert] = []
_next_near_id: int = 1


def _next_alert_id() -> int:
    global _next_near_id
    current = _next_near_id
    _next_near_id += 1
    return current


def _haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """
    Приблизительное расстояние между двумя точками в метрах.
    """
    R = 6371000  # радиус Земли, м
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return int(R * c)


@app.post("/near/alerts", response_model=NearbyAlert)
def create_near_alert(req: SendNearAlertRequest) -> NearbyAlert:
    """
    Создать важное сообщение "рядом".
    Пока храним только в памяти.
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    alert = NearbyAlert(
        id=_next_alert_id(),
        text=req.text,
        authorName="Автор",  # позже сюда можно подставить реального пользователя
        distanceMeters=0,
        createdAt=now_str,
        latitude=req.latitude,
        longitude=req.longitude,
    )
    _near_alerts.insert(0, alert)
    return alert


@app.get("/near/alerts", response_model=List[NearbyAlert])
def list_near_alerts(
    lat: Optional[float] = Query(None),
    lon: Optional[float] = Query(None),
) -> List[NearbyAlert]:
    """
    Получить важные сообщения 'рядом'.

    - Если lat/lon не переданы: просто возвращаем последние 50 сообщений.
    - Если lat/lon переданы:
        * сообщения с известными координатами фильтруем по радиусу;
        * сообщения БЕЗ координат тоже показываем (в конце списка), но без distanceMeters.
    """
    if not _near_alerts:
        return []

    # Если клиент не прислал координаты — просто отдаём последние 50 как есть
    if lat is None or lon is None:
        return _near_alerts[:50]

    radius_m = 5000  # 5 км

    nearby: List[NearbyAlert] = []
    no_location: List[NearbyAlert] = []

    for alert in _near_alerts:
        if alert.latitude is None or alert.longitude is None:
            # Сообщение без координат — покажем всем, но без расстояния
            no_location.append(alert.copy(update={"distanceMeters": None}))
            continue

        dist = _haversine_distance_m(lat, lon, alert.latitude, alert.longitude)
        if dist <= radius_m:
            nearby.append(alert.copy(update={"distanceMeters": dist}))

    # Сначала близкие с известным расстоянием, потом без координат
    return nearby + no_location


@app.post("/near/alerts/{alert_id}/forward", status_code=204)
def forward_near_alert(alert_id: int):
    """
    Пометить сообщение как важное и отправить дальше.
    Сейчас просто проверяем, что оно существует.
    """
    for alert in _near_alerts:
        if alert.id == alert_id:
            # TODO: здесь потом реализовать реальное распространение волнами
            return
    raise HTTPException(status_code=404, detail="Alert not found")


@app.post("/near/alerts/{alert_id}/dismiss", status_code=204)
def dismiss_near_alert(alert_id: int):
    """
    Скрыть сообщение. Сейчас просто удаляем его из общей "мини-базы".
    """
    global _near_alerts
    _near_alerts = [a for a in _near_alerts if a.id != alert_id]
    return


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
async def telegram_webhook(req: Request, background_tasks: BackgroundTasks):
    data = await req.json()
    print("Telegram webhook:", data)

    try:
        save_incoming_update(data)
    except Exception as e:
        # Не роняем вебхук, просто логируем
        print("Error while saving incoming Telegram message:", e)

    # После сохранения считаем количество непрочитанных и шлём пуш в фоне
    try:
        unread_count = get_unread_count()
        background_tasks.add_task(send_unread_count_push, unread_count)
    except Exception as e:
        print("Error scheduling FCM push:", e)

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
def mark_inbox_read(message_id: int, background_tasks: BackgroundTasks):
    """
    Помечает сообщение как прочитанное: status = 'read' по id.
    И после этого отправляет обновлённый unread_count в FCM.
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

    try:
        unread_count = get_unread_count()
        background_tasks.add_task(send_unread_count_push, unread_count)
    except Exception as e:
        print("Error scheduling FCM push after mark_read:", e)

    return {"status": "ok"}


@app.delete(
    "/api/inbox/{message_id}",
    dependencies=[Depends(verify_api_key)],
)
def delete_inbox_message(message_id: int, background_tasks: BackgroundTasks):
    """
    Удаляет сообщение из incoming_messages по id.
    После удаления отправляет обновлённый unread_count в FCM.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            DELETE FROM incoming_messages
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

    # Обновляем количество непрочитанных и шлём пуш
    try:
        unread_count = get_unread_count()
        background_tasks.add_task(send_unread_count_push, unread_count)
    except Exception as e:
        print("Error scheduling FCM push after delete:", e)

    return {"status": "ok"}
