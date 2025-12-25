# near_routes.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, List, Any
import math
import os
import shutil

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/near", tags=["near"])

# куда сохраняем файлы
UPLOAD_ROOT = os.getenv("NEAR_UPLOAD_DIR", "./uploads/near")


# ----- Pydantic-модели -----

class SendNearAlertRequest(BaseModel):
    text: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class NearAttachment(BaseModel):
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

    # новое
    attachments: List[NearAttachment] = []


# ----- "мини-база" в памяти -----

_alerts: list[NearbyAlert] = []
_next_id: int = 1


def _next_alert_id() -> int:
    global _next_id
    current = _next_id
    _next_id += 1
    return current


def _haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return int(R * c)


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


def _save_uploadfile_to_path(upload_file, dst_path: str) -> None:
    # upload_file: UploadFile (из FastAPI). Пишем потоково, без загрузки всего файла в память.
    with open(dst_path, "wb") as out:
        shutil.copyfileobj(upload_file.file, out)


# ----- Эндпоинты -----

@router.post("/alerts", response_model=NearbyAlert)
async def create_alert(request: Request) -> NearbyAlert:
    """
    Создать важное сообщение.

    Поддерживает:
    - JSON (как раньше): {"text": "...", "latitude": ..., "longitude": ...}
    - multipart/form-data:
        text=...
        latitude=...
        longitude=...
        files=<file1>, files=<file2> ...
    """
    content_type = (request.headers.get("content-type") or "").lower()

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    alert_id = _next_alert_id()

    # MULTIPART
    if content_type.startswith("multipart/form-data"):
        form = await request.form()

        text = str(form.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")

        latitude = _parse_optional_float(form.get("latitude"))
        longitude = _parse_optional_float(form.get("longitude"))

        files = form.getlist("files")  # list[UploadFile]

        # готовим папку
        alert_dir = os.path.join(UPLOAD_ROOT, str(alert_id))
        os.makedirs(alert_dir, exist_ok=True)

        attachments: list[NearAttachment] = []

        for f in files:
            if f is None:
                continue

            filename = _safe_filename(getattr(f, "filename", None) or "file")
            dst_path = os.path.join(alert_dir, filename)

            await run_in_threadpool(_save_uploadfile_to_path, f, dst_path)

            size_bytes = None
            try:
                size_bytes = os.path.getsize(dst_path)
            except Exception:
                pass

            attachments.append(
                NearAttachment(
                    filename=filename,
                    contentType=getattr(f, "content_type", None),
                    url=f"/media/near/{alert_id}/{filename}",
                    sizeBytes=size_bytes,
                )
            )

        alert = NearbyAlert(
            id=alert_id,
            text=text,
            authorName="Автор",
            distanceMeters=0,
            createdAt=now_str,
            latitude=latitude,
            longitude=longitude,
            attachments=attachments,
        )

        _alerts.insert(0, alert)
        return alert

    # JSON (как раньше)
    data = await request.json()
    req = SendNearAlertRequest.model_validate(data)

    alert = NearbyAlert(
        id=alert_id,
        text=req.text,
        authorName="Автор",
        distanceMeters=0,
        createdAt=now_str,
        latitude=req.latitude,
        longitude=req.longitude,
        attachments=[],
    )
    _alerts.insert(0, alert)
    return alert


@router.get("/alerts", response_model=List[NearbyAlert])
def list_alerts(
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> List[NearbyAlert]:
    if not _alerts:
        return []

    if lat is None or lon is None:
        return _alerts[:50]

    radius_m = 5000

    result: list[NearbyAlert] = []
    for alert in _alerts:
        if alert.latitude is None or alert.longitude is None:
            continue

        dist = _haversine_distance_m(lat, lon, alert.latitude, alert.longitude)
        if dist <= radius_m:
            result.append(alert.model_copy(update={"distanceMeters": dist}))

    return result


@router.post("/alerts/{alert_id}/forward", status_code=204)
def forward_alert(alert_id: int) -> None:
    for alert in _alerts:
        if alert.id == alert_id:
            return
    raise HTTPException(status_code=404, detail="Alert not found")


@router.post("/alerts/{alert_id}/dismiss", status_code=204)
def dismiss_alert(alert_id: int) -> None:
    global _alerts
    _alerts = [a for a in _alerts if a.id != alert_id]
    return
