import os
import json
import math
import uuid
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


app = FastAPI(title="Social Aggregator Minimal API", version="0.4.0")


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
    Создаёт необходимые таблицы, если их ещё нет.
    Вызывается один раз при старте сервиса.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # Таблица для входящих сообщений из Telegram
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

    # Таблица для радиальных сообщений "рядом"
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS near_alerts (
            id SERIAL PRIMARY KEY,
            text TEXT NOT NULL,
            author_name TEXT,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    # Таблица клиентов, которые пользуются разделом "Рядом"
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS near_clients (
            id UUID PRIMARY KEY,
            last_lat DOUBLE PRECISION,
            last_lon DOUBLE PRECISION,
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    # Таблица доставок сообщений конкретным клиентам (волны)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS near_alert_deliveries (
            alert_id INTEGER NOT NULL REFERENCES near_alerts(id) ON DELETE CASCADE,
            client_id UUID NOT NULL REFERENCES near_clients(id) ON DELETE CASCADE,
            delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status VARCHAR(20) NOT NULL DEFAULT 'delivered',
            PRIMARY KEY (alert_id, client_id)
        );
        """
    )

    conn.commit()
    cur.close()
    conn.close()


init_db()


def upsert_near_client(
    client_id: Optional[str],
    lat: Optional[float],
    lon: Optional[float],
) -> Optional[str]:
    """
    Сохраняем/обновляем клиента в near_clients.
    Если client_id пустой или битый — тихо игнорируем.
    Возвращаем нормализованный UUID-строку или None.
    """
    if not client_id:
        return None

    try:
        uid = uuid.UUID(client_id)
    except Exception:
        # Неверный формат UUID, не пишем в БД
        return None

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if lat is not None and lon is not None:
            # Обновляем и координаты, и last_seen_at
            cur.execute(
                """
                INSERT INTO near_clients (id, last_lat, last_lon, last_seen_at)
                VALUES (%(id)s, %(lat)s, %(lon)s, NOW())
                ON CONFLICT (id) DO UPDATE
                SET last_lat = EXCLUDED.last_lat,
                    last_lon = EXCLUDED.last_lon,
                    last_seen_at = EXCLUDED.last_seen_at;
                """,
                {"id": str(uid), "lat": lat, "lon": lon},
            )
        else:
            # Обновляем только last_seen_at, координаты не трогаем
            cur.execute(
                """
                INSERT INTO near_clients (id, last_lat, last_lon, last_seen_at)
                VALUES (%(id)s, NULL, NULL, NOW())
                ON CONFLICT (id) DO UPDATE
                SET last_seen_at = EXCLUDED.last_seen_at;
                """,
                {"id": str(uid)},
            )
        conn.commit()
    finally:
        cur.close()
        conn.close()

    return str(uid)


def distribute_near_alert(
    alert_id: int,
    source_client_id: Optional[str],
    fanout: int = 40,
):
    """
    Делает "волну" от source_client_id:
    - берём координаты source_client_id;
    - выбираем ближайших клиентов, у которых ещё нет доставки для этого alert;
    - записываем им доставки в near_alert_deliveries;
    - гарантируем, что сам источник тоже видит сообщение.
    """
    if not source_client_id:
        return

    try:
        uid = uuid.UUID(source_client_id)
    except Exception:
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Координаты источника
        cur.execute(
            """
            SELECT last_lat, last_lon
            FROM near_clients
            WHERE id = %(id)s;
            """,
            {"id": str(uid)},
        )
        row = cur.fetchone()
        if not row or row["last_lat"] is None or row["last_lon"] is None:
            # Нет координат — волну не строим
            return

        src_lat = row["last_lat"]
        src_lon = row["last_lon"]

        # Все клиенты без доставки этого alert
        cur.execute(
            """
            SELECT nc.id, nc.last_lat, nc.last_lon
            FROM near_clients nc
            LEFT JOIN near_alert_deliveries d
                ON d.client_id = nc.id AND d.alert_id = %(alert_id)s
            WHERE d.alert_id IS NULL
              AND nc.last_lat IS NOT NULL
              AND nc.last_lon IS NOT NULL
              AND nc.id <> %(source_id)s;
            """,
            {"alert_id": alert_id, "source_id": str(uid)},
        )
        rows = cur.fetchall()
        if not rows:
            # Некому доставлять
            return

        # Сортируем по расстоянию (приближённо, по квадрату расстояния)
        def dist2(r):
            return (r["last_lat"] - src_lat) ** 2 + (r["last_lon"] - src_lon) ** 2

        rows.sort(key=dist2)
        selected = rows[:fanout]

        # Вставляем доставки
        for r in selected:
            cur.execute(
                """
                INSERT INTO near_alert_deliveries (alert_id, client_id, delivered_at, status)
                VALUES (%(alert_id)s, %(client_id)s, NOW(), 'delivered')
                ON CONFLICT (alert_id, client_id) DO NOTHING;
                """,
                {"alert_id": alert_id, "client_id": str(r["id"])},
            )

        # Источник тоже должен видеть сообщение
        cur.execute(
            """
            INSERT INTO near_alert_deliveries (alert_id, client_id, delivered_at, status)
            VALUES (%(alert_id)s, %(client_id)s, NOW(), 'author')
            ON CONFLICT (alert_id, client_id) DO NOTHING;
            """,
            {"alert_id": alert_id, "client_id": str(uid)},
        )

        conn.commit()
    finally:
        cur.close()
        conn.close()


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
    latitude: Optional[float] = None
    longitude: Optional[float] = None


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


def _row_to_nearby_alert(row: dict, distance_m: Optional[int] = None) -> NearbyAlert:
    created_at = row["created_at"]
    if isinstance(created_at, datetime):
        created_str = created_at.strftime("%Y-%m-%d %H:%M")
    else:
        created_str = str(created_at)

    return NearbyAlert(
        id=row["id"],
        text=row["text"],
        authorName=row.get("author_name"),
        distanceMeters=distance_m,
        createdAt=created_str,
        latitude=row.get("latitude"),
        longitude=row.get("longitude"),
    )


@app.post("/near/alerts", response_model=NearbyAlert)
def create_near_alert(
    req: SendNearAlertRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> NearbyAlert:
    """
    Создать важное сообщение "рядом".
    Храним в таблице near_alerts (Postgres).
    Одновременно фиксируем клиента и запускаем первую волну.
    """
    # сохраняем/обновляем клиента и получаем нормальный UUID
    client_uuid = upsert_near_client(x_client_id, req.latitude, req.longitude)

    now = datetime.now(timezone.utc)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO near_alerts (text, author_name, latitude, longitude, created_at)
            VALUES (%(text)s, %(author_name)s, %(lat)s, %(lon)s, %(created_at)s)
            RETURNING id, text, author_name, latitude, longitude, created_at;
            """,
            {
                "text": req.text,
                "author_name": "Автор",  # TODO: подставить реального пользователя
                "lat": req.latitude,
                "lon": req.longitude,
                "created_at": now,
            },
        )
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close()
        conn.close()

    alert = _row_to_nearby_alert(row, distance_m=0)

    # Первая волна от автора
    distribute_near_alert(alert_id=alert.id, source_client_id=client_uuid, fanout=40)

    # Для собственного сообщения distanceMeters = 0 (оно "у нас под ногами")
    return alert


@app.get("/near/alerts", response_model=List[NearbyAlert])
def list_near_alerts(
    lat: Optional[float] = Query(None),
    lon: Optional[float] = Query(None),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> List[NearbyAlert]:
    """
    Получить важные сообщения 'рядом' ДЛЯ КОНКРЕТНОГО клиента.

    - Используем near_alert_deliveries: показываем только то, что доставлено этому clientId.
    - Сообщения со статусом 'dismissed' не показываем.
    - Фильтруем по последним 24 часам.
    - Если lat/lon переданы — считаем distanceMeters от текущих координат клиента.
    """
    client_uuid = upsert_near_client(x_client_id, lat, lon)
    if not client_uuid:
        return []

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT
                a.id,
                a.text,
                a.author_name,
                a.latitude,
                a.longitude,
                a.created_at,
                d.status,
                d.delivered_at
            FROM near_alerts a
            JOIN near_alert_deliveries d
              ON d.alert_id = a.id
            WHERE d.client_id = %(client_id)s
              AND d.status <> 'dismissed'
              AND a.created_at >= NOW() - INTERVAL '24 hours'
            ORDER BY d.delivered_at DESC
            LIMIT 200;
            """,
            {"client_id": client_uuid},
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not rows:
        return []

    alerts = [_row_to_nearby_alert(row, distance_m=None) for row in rows]

    # Если нет текущих координат — возвращаем как есть
    if lat is None or lon is None:
        return alerts

    # Считаем distanceMeters от текущей позиции клиента
    result: List[NearbyAlert] = []
    for alert in alerts:
        if alert.latitude is None or alert.longitude is None:
            result.append(alert)
        else:
            dist = _haversine_distance_m(lat, lon, alert.latitude, alert.longitude)
            result.append(alert.copy(update={"distanceMeters": dist}))

    return result


@app.post("/near/alerts/{alert_id}/forward", status_code=204)
def forward_near_alert(
    alert_id: int,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
):
    """
    Пометить сообщение как важное и отправить дальше.
    - Обновляем статус доставки для текущего клиента.
    - Запускаем новую волну от его координат.
    """
    client_uuid = upsert_near_client(x_client_id, None, None)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Проверяем, что alert существует
        cur.execute(
            "SELECT 1 FROM near_alerts WHERE id = %(id)s;",
            {"id": alert_id},
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Alert not found")

        # Помечаем, что этот клиент переслал
        if client_uuid:
            cur.execute(
                """
                UPDATE near_alert_deliveries
                SET status = 'forwarded'
                WHERE alert_id = %(alert_id)s AND client_id = %(client_id)s;
                """,
                {"alert_id": alert_id, "client_id": client_uuid},
            )
        conn.commit()
    finally:
        cur.close()
        conn.close()

    # Новая волна от этого клиента
    distribute_near_alert(alert_id=alert_id, source_client_id=client_uuid, fanout=40)
    return


@app.post("/near/alerts/{alert_id}/dismiss", status_code=204)
def dismiss_near_alert(
    alert_id: int,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
):
    """
    Скрыть сообщение для ТЕКУЩЕГО клиента.
    Просто ставим status = 'dismissed' в near_alert_deliveries.
    Сам alert остаётся для других.
    """
    client_uuid = upsert_near_client(x_client_id, None, None)

    if not client_uuid:
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE near_alert_deliveries
            SET status = 'dismissed'
            WHERE alert_id = %(alert_id)s AND client_id = %(client_id)s;
            """,
            {"alert_id": alert_id, "client_id": client_uuid},
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
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
