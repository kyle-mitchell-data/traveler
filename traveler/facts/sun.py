"""Sunrise, sunset and sun elevation from the standard sunrise equation (about 1 minute accurate).

No network and no dependencies, so the computed adapter always works.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

_J2000 = 2451545.0
_EPOCH = datetime(2000, 1, 1, 12, tzinfo=UTC)
_TILT = math.radians(23.4397)


def _jd(when: datetime) -> float:
    return _J2000 + (when - _EPOCH).total_seconds() / 86400


def _from_jd(jd: float) -> datetime:
    return _EPOCH + timedelta(days=jd - _J2000)


def _solar(n: float) -> tuple[float, float, float]:
    """Mean anomaly M, ecliptic longitude lam (radians) and the transit correction, for day n."""
    m = math.radians((357.5291 + 0.98560028 * n) % 360)
    c = 1.9148 * math.sin(m) + 0.02 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)
    return m, lam, 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)


def _transit_and_decl(day: date, lon: float) -> tuple[float, float]:
    n = (day - _EPOCH.date()).days - lon / 360
    _, lam, corr = _solar(n)
    return _J2000 + n + corr, math.asin(math.sin(lam) * math.sin(_TILT))


def sun_times(day: date, lat: float, lon: float) -> tuple[datetime, datetime] | None:
    """(sunrise, sunset) in UTC for the local calendar day, or None during polar day/night."""
    transit, decl = _transit_and_decl(day, lon)
    phi = math.radians(lat)
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(decl)) / (math.cos(phi) * math.cos(decl))
    if not -1 <= cos_w <= 1:
        return None
    w = math.degrees(math.acos(cos_w)) / 360
    return _from_jd(transit - w), _from_jd(transit + w)


def elevation(when: datetime, lat: float, lon: float) -> float:
    """Sun elevation above the horizon in degrees (negative below it)."""
    transit, decl = _transit_and_decl(when.date(), lon)
    hour_angle = math.radians((_jd(when) - transit) * 360)
    phi = math.radians(lat)
    s = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(hour_angle)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))
