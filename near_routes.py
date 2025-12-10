# near_routes.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import math

router = APIRouter(prefix="/near", tags=["near"])


# ----- Pydantic-модели -----

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

    # внутренние поля, можно не возвращать в клиент (но можно и вернуть – Android их проигнорирует)
    latitude: Optional[float] = None
    longitude: Optional[float] = None


# ----- "мини-база" в памяти -----

_alerts: list[NearbyAlert] = []
_next_id: int = 1


def _next_alert_id() -> int:
    global _next_id
    current = _next_id
    _next_id += 1
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


# ----- Эндпоинты -----

@router.post("/alerts", response_model=NearbyAlert)
def create_alert(req: SendNearAlertRequest) -> NearbyAlert:
    """
    Создать важное сообщение.
    Пока просто кладём его в память и возвращаем.
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    alert = NearbyAlert(
        id=_next_alert_id(),
        text=req.text,
        authorName="Автор",  # позже можно подставить реального пользователя
        distanceMeters=0,
        createdAt=now_str,
        latitude=req.latitude,
        longitude=req.longitude,
    )
    _alerts.insert(0, alert)
    return alert


@router.get("/alerts", response_model=List[NearbyAlert])
def list_alerts(
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> List[NearbyAlert]:
    """
    Получить важные сообщения 'рядом'.
    Если lat/lon переданы – считаем расстояние и фильтруем по радиусу.
    Если нет – просто возвращаем последние сообщения.
    """
    if not _alerts:
        return []

    # если координаты не передали – просто последние N сообщений без расстояния
    if lat is None or lon is None:
        return _alerts[:50]

    radius_m = 5000  # 5 км радиус, можно потом вынести в настройки

    result: list[NearbyAlert] = []
    for alert in _alerts:
        if alert.latitude is None or alert.longitude is None:
            continue

        dist = _haversine_distance_m(lat, lon, alert.latitude, alert.longitude)
        if dist <= radius_m:
            result.append(
                alert.copy(update={"distanceMeters": dist})
            )

    return result


@router.post("/alerts/{alert_id}/forward", status_code=204)
def forward_alert(alert_id: int) -> None:
    """
    Пометить сообщение как важное и отправить дальше.
    Пока просто проверяем, что оно существует.
    """
    for alert in _alerts:
        if alert.id == alert_id:
            # TODO: здесь позже добавить реальную логику "следующей волны"
            return
    raise HTTPException(status_code=404, detail="Alert not found")


@router.post("/alerts/{alert_id}/dismiss", status_code=204)
def dismiss_alert(alert_id: int) -> None:
    """
    Скрыть сообщение для пользователя.
    Здесь для простоты просто удаляем его из общей "мини-базы".
    """
    global _alerts
    _alerts = [a for a in _alerts if a.id != alert_id]
    return
