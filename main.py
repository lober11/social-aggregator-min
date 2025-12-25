import os
import json
import math
import uuid
import shutil
from typing import List, Optional, Literal, Any
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Depends, Header, Query, BackgroundTasks
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
import httpx

import psycopg2
from psycopg2.extras import RealDictCursor

import firebase_admin
from firebase_admin import credentials, messaging


app = FastAPI(title="Social Aggregator Minimal API", version="0.4.0")

# ==== Static / uploads =========================================================
# Папка, где лежат все загружаемые файлы (и откуда их раздаём через /media)
UPLOADS_DIR = os.getenv("UPLOADS_DIR", "./uploads")
NEAR_UPLOAD_DIR = os.getenv("NEAR_UPLOAD_DIR", os.path.join(UPLOADS_DIR, "near"))

# важно: папка должна существовать, иначе StaticFiles может упасть при старте
os.makedirs(NEAR_UPLOAD_DIR, exist_ok=True)

# /media/near/<alert_id>/<filename> -> ./uploads/near/<alert_id>/<filename>
app.mount("/media", StaticFiles(directory=UPLOADS_DIR), name="media")


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

# Радиус волны "рядом" в метрах (по умолчанию 5 км)
NEAR_WAVE_RADIUS_METERS = float(os.getenv("NEAR_WAVE_RADIUS_METERS", "5000"))


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

    # Таблица вложений near_alerts
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS near_alert_attachments (
            id SERIAL PRIMARY KEY,
            alert_id INTEGER NOT NULL REFERENCES near_alerts(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            content_type TEXT,
            url TEXT NOT NULL,
            size_bytes BIGINT,
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

    # 🔹 Добавляем fcm_token, если его ещё нет
    cur.execute(
        """
        ALTER TABLE near_clients
        ADD COLUMN IF NOT EXISTS fcm_token TEXT;
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

    ВАЖНО:
    - Если lat/lon переданы, обновляем координаты и last_seen_at.
    - Если lat/lon НЕ переданы, обновляем ТОЛЬКО last_seen_at, координаты не трогаем.
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
                INSERT INTO near_clients (id, last_seen_at)
                VALUES (%(id)s, NOW())
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


def distribute_near_alert(
    alert_id: int,
    source_client_id: Optional[str],
    fanout: int = 40,
):
    """
    Делает "волну" от source_client_id.

    Логика:
    - Пытаемся получить координаты источника.
    - Берём всех клиентов, у которых ещё нет доставки этого alert.
    - Если у источника ЕСТЬ координаты:
        * делим кандидатов на:
            - внутри радиуса NEAR_WAVE_RADIUS_METERS;
            - остальных (в т.ч. без координат).
        * если внутри радиуса кто-то есть:
            - сортируем их по расстоянию и берём до fanout;
            - если их меньше fanout — добираем остальных по id.
        * если внутри радиуса никого нет:
            - просто берём всех кандидатов по id до fanout.
    - Если у источника НЕТ координат:
        * берём всех кандидатов по id до fanout.
    - ВСЕГДА добавляем запись для самого источника (status='author').

    Дополнительно:
    - После вставки новых доставок считаем для этих клиентов
      количество "непрочитанных" near (status='delivered')
      и шлём каждому data-push type="near_alert".
    """
    if not source_client_id:
        return

    try:
        uid = uuid.UUID(source_client_id)
    except Exception:
        return

    conn = get_db_connection()
    cur = conn.cursor()

    # сюда сложим (fcm_token, unread_count) для пушей
    push_targets = []

    try:
        # Координаты источника (могут быть NULL)
        cur.execute(
            """
            SELECT last_lat, last_lon
            FROM near_clients
            WHERE id = %(id)s;
            """,
            {"id": str(uid)},
        )
        src_row = cur.fetchone()
        src_lat = src_row["last_lat"] if src_row else None
        src_lon = src_row["last_lon"] if src_row else None

        # Все клиенты, у которых ещё нет доставки этого alert (кроме самого источника)
        cur.execute(
            """
            SELECT nc.id, nc.last_lat, nc.last_lon, nc.fcm_token
            FROM near_clients nc
            LEFT JOIN near_alert_deliveries d
                ON d.client_id = nc.id AND d.alert_id = %(alert_id)s
            WHERE d.alert_id IS NULL
              AND nc.id <> %(source_id)s;
            """,
            {"alert_id": alert_id, "source_id": str(uid)},
        )
        rows = cur.fetchall()

        selected = []

        if rows:
            if src_lat is not None and src_lon is not None:
                inside_radius = []
                others = []

                for r in rows:
                    lat = r["last_lat"]
                    lon = r["last_lon"]

                    if lat is None or lon is None:
                        others.append(r)
                        continue

                    dist_m = _haversine_distance_m(src_lat, src_lon, lat, lon)
                    if dist_m <= NEAR_WAVE_RADIUS_METERS:
                        inside_radius.append((dist_m, r))
                    else:
                        others.append(r)

                if inside_radius:
                    inside_radius.sort(key=lambda t: t[0])
                    selected = [r for _, r in inside_radius[:fanout]]

                    if len(selected) < fanout and others:
                        remaining = fanout - len(selected)
                        others_sorted = sorted(others, key=lambda rr: str(rr["id"]))
                        selected.extend(others_sorted[:remaining])
                else:
                    rows_sorted = sorted(rows, key=lambda r: str(r["id"]))
                    selected = rows_sorted[:fanout]
            else:
                rows_sorted = sorted(rows, key=lambda r: str(r["id"]))
                selected = rows_sorted[:fanout]

            # Вставляем доставки для выбранных клиентов
            for r in selected:
                cur.execute(
                    """
                    INSERT INTO near_alert_deliveries (alert_id, client_id, delivered_at, status)
                    VALUES (%(alert_id)s, %(client_id)s, NOW(), 'delivered')
                    ON CONFLICT (alert_id, client_id) DO NOTHING;
                    """,
                    {"alert_id": alert_id, "client_id": str(r["id"])},
                )

            # Для каждого выбранного клиента считаем количество "delivered"
            for r in selected:
                token = r.get("fcm_token")
                if not token:
                    continue

                cur.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM near_alert_deliveries
                    WHERE client_id = %(cid)s
                      AND status = 'delivered';
                    """,
                    {"cid": str(r["id"])},
                )
                row = cur.fetchone()
                if not row:
                    continue
                unread = int(row.get("c", 0))
                if unread > 0:
                    push_targets.append((token, unread))

        # ВСЕГДА вставляем запись для автора
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

    for token, unread in push_targets:
        send_near_unread_push(token, unread)


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


def send_near_unread_push(device_token: str, near_unread_count: int):
    """
    Отправляет data-push в FCM для раздела "Рядом".
    Каждый клиент получает свой near_unread_count.
    """
    init_firebase()
    if firebase_app is None:
        return

    if not device_token:
        return

    message = messaging.Message(
        data={
            "type": "near_alert",
            "near_unread_count": str(near_unread_count),
        },
        token=device_token,
    )

    try:
        response = messaging.send(message)
        print("Successfully sent NEAR FCM message:", response)
    except Exception as e:
        print("Error sending NEAR FCM message:", e)


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


class RegisterDeviceRequest(BaseModel):
    fcmToken: str


class NearFileAttachment(BaseModel):
    filename: str
    contentType: Optional[str] = None
    url: str
    sizeBytes: Optional[int] = None


class NearbyAlert(BaseModel):
    id: int
    text: str
    authorName: Optional[str] = None
    distanceMeters: Optional[int] = None
    createdAt: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    deliveryStatus: Optional[str] = None  # 'author' | 'delivered' | 'forwarded' | 'dismissed'

    # новое
    attachments: List[NearFileAttachment] = []


def _row_to_nearby_alert(
    row: dict,
    distance_m: Optional[int] = None,
    delivery_status: Optional[str] = None,
    attachments: Optional[List[NearFileAttachment]] = None,
) -> NearbyAlert:
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
        deliveryStatus=delivery_status,
        attachments=attachments or [],
    )


def _safe_filename(name: str) -> str:
    name = (name or "").strip()
    name = name.replace("\\", "_").replace("/", "_")
    return name or "file"


def _parse_optional_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "" or s.lower() == "null":
        return None
    return float(s)


def _unique_path(dir_path: str, filename: str) -> str:
    """
    Если файл с таким именем уже есть — добавляем префикс, чтобы не перезатереть.
    """
    base = _safe_filename(filename)
    candidate = os.path.join(dir_path, base)
    if not os.path.exists(candidate):
        return candidate

    prefix = uuid.uuid4().hex[:8]
    candidate = os.path.join(dir_path, f"{prefix}_{base}")
    return candidate


def _save_uploadfile_to_path(upload_file, dst_path: str) -> None:
    with open(dst_path, "wb") as out:
        shutil.copyfileobj(upload_file.file, out)


class NearAlertDeliveryDebug(BaseModel):
    clientId: str
    status: str
    deliveredAt: datetime
    lastLat: Optional[float] = None
    lastLon: Optional[float] = None
    lastSeenAt: Optional[datetime] = None


class NearAlertDebug(BaseModel):
    alert: NearbyAlert
    deliveries: List[NearAlertDeliveryDebug]


@app.post("/near/alerts", response_model=NearbyAlert)
async def create_near_alert(
    request: Request,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> NearbyAlert:
    """
    Создать важное сообщение "рядом".
    Поддерживает:
    - JSON (как раньше)
    - multipart/form-data с файлами:
        text=...
        latitude=...
        longitude=...
        files=<file>, files=<file2>...

    Храним alert в near_alerts (Postgres),
    вложения — в near_alert_attachments + на диске,
    фиксируем клиента и запускаем первую волну.
    """
    content_type = (request.headers.get("content-type") or "").lower()

    text: str
    latitude: Optional[float]
    longitude: Optional[float]
    upload_files: List[Any] = []

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        text = str(form.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")

        latitude = _parse_optional_float(form.get("latitude"))
        longitude = _parse_optional_float(form.get("longitude"))

        upload_files = form.getlist("files")  # list[UploadFile]
    else:
        data = await request.json()
        req = SendNearAlertRequest.model_validate(data)
        text = req.text
        latitude = req.latitude
        longitude = req.longitude

    # сохраняем/обновляем клиента
    client_uuid = upsert_near_client(x_client_id, latitude, longitude)

    now = datetime.now(timezone.utc)

    conn = get_db_connection()
    cur = conn.cursor()

    attachments_models: List[NearFileAttachment] = []

    try:
        # 1) создаём alert
        cur.execute(
            """
            INSERT INTO near_alerts (text, author_name, latitude, longitude, created_at)
            VALUES (%(text)s, %(author_name)s, %(lat)s, %(lon)s, %(created_at)s)
            RETURNING id, text, author_name, latitude, longitude, created_at;
            """,
            {
                "text": text,
                "author_name": "Автор",  # TODO: подставить реального пользователя
                "lat": latitude,
                "lon": longitude,
                "created_at": now,
            },
        )
        row = cur.fetchone()
        alert_id = int(row["id"])

        # 2) если есть файлы — сохраняем и пишем в таблицу near_alert_attachments
        if upload_files:
            alert_dir = os.path.join(NEAR_UPLOAD_DIR, str(alert_id))
            os.makedirs(alert_dir, exist_ok=True)

            for f in upload_files:
                if f is None:
                    continue

                original_name = getattr(f, "filename", None) or "file"
                dst_path = _unique_path(alert_dir, original_name)
                saved_name = os.path.basename(dst_path)

                await run_in_threadpool(_save_uploadfile_to_path, f, dst_path)

                size_bytes = None
                try:
                    size_bytes = os.path.getsize(dst_path)
                except Exception:
                    pass

                content_type_f = getattr(f, "content_type", None)
                url = f"/media/near/{alert_id}/{saved_name}"

                cur.execute(
                    """
                    INSERT INTO near_alert_attachments (alert_id, filename, content_type, url, size_bytes)
                    VALUES (%(alert_id)s, %(filename)s, %(content_type)s, %(url)s, %(size_bytes)s);
                    """,
                    {
                        "alert_id": alert_id,
                        "filename": saved_name,
                        "content_type": content_type_f,
                        "url": url,
                        "size_bytes": size_bytes,
                    },
                )

                attachments_models.append(
                    NearFileAttachment(
                        filename=saved_name,
                        contentType=content_type_f,
                        url=url,
                        sizeBytes=size_bytes,
                    )
                )

        conn.commit()
    finally:
        cur.close()
        conn.close()

    alert = _row_to_nearby_alert(row, distance_m=0, delivery_status="author", attachments=attachments_models)

    # Первая волна от автора
    distribute_near_alert(alert_id=alert.id, source_client_id=client_uuid, fanout=40)

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
    - attachments подтягиваем из near_alert_attachments.
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

        if not rows:
            return []

        alert_ids = [int(r["id"]) for r in rows]

        # Все вложения пачкой
        cur.execute(
            """
            SELECT alert_id, filename, content_type, url, size_bytes
            FROM near_alert_attachments
            WHERE alert_id = ANY(%(ids)s)
            ORDER BY id ASC;
            """,
            {"ids": alert_ids},
        )
        att_rows = cur.fetchall()

    finally:
        cur.close()
        conn.close()

    attachments_by_alert: dict[int, List[NearFileAttachment]] = {}
    for a in att_rows:
        aid = int(a["alert_id"])
        attachments_by_alert.setdefault(aid, []).append(
            NearFileAttachment(
                filename=a["filename"],
                contentType=a.get("content_type"),
                url=a["url"],
                sizeBytes=a.get("size_bytes"),
            )
        )

    alerts = [
        _row_to_nearby_alert(
            row,
            distance_m=None,
            delivery_status=row.get("status"),
            attachments=attachments_by_alert.get(int(row["id"]), []),
        )
        for row in rows
    ]

    if lat is None or lon is None:
        return alerts

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


@app.post("/near/register_device", status_code=204)
def register_device(
    req: RegisterDeviceRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
):
    """
    Регистрирует (или обновляет) FCM-токен для клиента раздела "Рядом".
    Вызывается с:
      - заголовком X-Client-Id (UUID клиента),
      - телом {"fcmToken": "..."}.
    """
    if not x_client_id:
        return

    try:
        uid = uuid.UUID(x_client_id)
    except Exception:
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO near_clients (id, fcm_token, last_seen_at)
            VALUES (%(id)s, %(token)s, NOW())
            ON CONFLICT (id) DO UPDATE
            SET fcm_token = EXCLUDED.fcm_token,
                last_seen_at = EXCLUDED.last_seen_at;
            """,
            {"id": str(uid), "token": req.fcmToken},
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

    return


@app.get(
    "/near/debug/alerts/{alert_id}",
    response_model=NearAlertDebug,
    dependencies=[Depends(verify_api_key)],
)
def debug_near_alert(alert_id: int) -> NearAlertDebug:
    """
    Debug: информация по одному alert'у + все доставки и координаты клиентов.
    Защищено X-Api-Key.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # сам alert
        cur.execute(
            """
            SELECT id, text, author_name, latitude, longitude, created_at
            FROM near_alerts
            WHERE id = %(id)s;
            """,
            {"id": alert_id},
        )
        alert_row = cur.fetchone()
        if not alert_row:
            raise HTTPException(status_code=404, detail="Alert not found")

        # вложения
        cur.execute(
            """
            SELECT filename, content_type, url, size_bytes
            FROM near_alert_attachments
            WHERE alert_id = %(id)s
            ORDER BY id ASC;
            """,
            {"id": alert_id},
        )
        att_rows = cur.fetchall()
        atts = [
            NearFileAttachment(
                filename=a["filename"],
                contentType=a.get("content_type"),
                url=a["url"],
                sizeBytes=a.get("size_bytes"),
            )
            for a in att_rows
        ]

        alert = _row_to_nearby_alert(alert_row, distance_m=None, delivery_status=None, attachments=atts)

        # доставки
        cur.execute(
            """
            SELECT
                d.client_id,
                d.status,
                d.delivered_at,
                c.last_lat,
                c.last_lon,
                c.last_seen_at
            FROM near_alert_deliveries d
            LEFT JOIN near_clients c
              ON c.id = d.client_id
            WHERE d.alert_id = %(alert_id)s
            ORDER BY d.delivered_at DESC;
            """,
            {"alert_id": alert_id},
        )
        rows = cur.fetchall()

        deliveries = [
            NearAlertDeliveryDebug(
                clientId=str(r["client_id"]),
                status=r["status"],
                deliveredAt=r["delivered_at"],
                lastLat=r.get("last_lat"),
                lastLon=r.get("last_lon"),
                lastSeenAt=r.get("last_seen_at"),
            )
            for r in rows
        ]
    finally:
        cur.close()
        conn.close()

    return NearAlertDebug(alert=alert, deliveries=deliveries)


# ==== Стандартные эндпоинты ===================================================

@app.get("/")
def root():
    return {"status": "ok", "service": "Social Aggregator Minimal API"}


@app.head("/")
def root_head():
    return PlainTextResponse("", status_code=200)


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.head("/health")
def health_head():
    return PlainTextResponse("", status_code=200)


@app.get("/api/feed")
def feed():
    return []


@app.get("/api/telegram/send", dependencies=[Depends(verify_api_key)])
async def telegram_send(chat_id: str, text: str):
    result = await tg_send_message(chat_id=chat_id, text=text)
    return {"ok": True, "result": result}


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
        return

    ts = message.get("date")
    if ts:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        dt = datetime.now(timezone.utc)

    if update_id is None or chat_id is None:
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


@app.post("/api/webhooks/telegram")
async def telegram_webhook(req: Request, background_tasks: BackgroundTasks):
    data = await req.json()
    print("Telegram webhook:", data)

    try:
        save_incoming_update(data)
    except Exception as e:
        print("Error while saving incoming Telegram message:", e)

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

    return [InboxMessage(**row) for row in rows]


@app.post(
    "/api/inbox/{message_id}/read",
    dependencies=[Depends(verify_api_key)],
)
def mark_inbox_read(message_id: int, background_tasks: BackgroundTasks):
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

    try:
        unread_count = get_unread_count()
        background_tasks.add_task(send_unread_count_push, unread_count)
    except Exception as e:
        print("Error scheduling FCM push after delete:", e)

    return {"status": "ok"}
