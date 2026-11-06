"""Source adapters: each turns one source into candidate facts.

Local adapters (computed, country, evergreen) never touch the network and always run.
Network adapters each make one request through `ctx.fetch`; a failure or a slow source
costs that adapter's facts and nothing else (see engine.gather).
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from traveler import regions
from traveler.facts import sun
from traveler.facts.model import Context, Fact

Adapter = Callable[[Context], list[Fact]]


# --- helpers -------------------------------------------------------------------


def _local(ctx: Context) -> datetime:
    return ctx.now.astimezone(ZoneInfo(str(ctx.profile.get("tz") or "UTC")))


def _hhmm(when: datetime, tz: ZoneInfo) -> str:
    return when.astimezone(tz).strftime("%H:%M")


def _span(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    return f"{hours} h" if hours else f"{mins} min"


def _when(local: datetime) -> str:
    """'Saturday afternoon', 'early Saturday morning', 'Friday night'."""
    day, hour = local.strftime("%A"), local.hour
    if hour < 5:
        return f"early {day} morning"
    part = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening" if hour < 21 else "night"
    return f"{day} {part}"


def km_between(a: dict[str, Any], b: dict[str, Any]) -> int:
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(2 * 6371 * math.asin(math.sqrt(h)))


def _short_near(profile: dict[str, Any]) -> str:
    """The bare place name: 'Tokyo', 'Northern Virginia', 'Singapore'."""
    return str(profile.get("city") or profile.get("code"))


# --- local adapters --------------------------------------------------------------


def computed(ctx: Context) -> list[Fact]:
    p = ctx.profile
    tz = ZoneInfo(str(p.get("tz") or "UTC"))
    local = _local(ctx)
    facts = [
        Fact(
            "time",
            "live",
            "time.now",
            {
                "near": ctx.near,
                "hhmm": local.strftime("%H:%M"),
                "when": _when(local),
                "date": local.date().isoformat(),
            },
            now=True,
            source_name="Computed",
        )
    ]

    if "lat" in p and "lon" in p:
        lat, lon = float(p["lat"]), float(p["lon"])
        times = sun.sun_times(local.date(), lat, lon)
        if times:
            rise, set_ = times
            base = {"near": ctx.near, "sunrise": _hhmm(rise, tz), "sunset": _hhmm(set_, tz)}
            if ctx.now < rise:
                facts.append(
                    Fact(
                        "sun",
                        "live",
                        "sun.before_rise",
                        {**base, "wait": _span((rise - ctx.now).total_seconds())},
                        source_name="Computed",
                    )
                )
            elif ctx.now < set_:
                left = _span((set_ - ctx.now).total_seconds())
                facts.append(Fact("sun", "live", "sun.daylight_left", {**base, "left": left}, source_name="Computed"))
            else:
                facts.append(
                    Fact(
                        "sun",
                        "live",
                        "sun.after_set",
                        {**base, "ago": _span((ctx.now - set_).total_seconds())},
                        source_name="Computed",
                    )
                )
            length = _span((set_ - rise).total_seconds())
            facts.append(
                Fact(
                    "sun",
                    "today",
                    "sun.day_length",
                    {**base, "length": length, "date": local.date().isoformat()},
                    source_name="Computed",
                )
            )
        deg = sun.elevation(ctx.now, lat, lon)
        key = "sun.elevation_up" if deg >= 0 else "sun.elevation_down"
        facts.append(Fact("sun", "live", key, {"near": ctx.near, "deg": round(abs(deg))}, source_name="Computed"))

    if ctx.from_region and ctx.from_region != ctx.region:
        origin = regions.profile(ctx.from_region)
        if "lat" in origin and "lat" in p:
            km = km_between(origin, p)
            facts.append(
                Fact(
                    "journey",
                    "live",
                    "journey.distance",
                    {"km": f"{km:,}", "origin": _short_near(origin)},
                    source_name="Computed",
                )
            )
        facts.extend(_clock_change(ctx, origin, tz))
    if ctx.hop_count:
        facts.append(Fact("journey", "live", "journey.stop", {"n": f"{ctx.hop_count:,}"}, source_name="Computed"))
    return facts


def _clock_change(ctx: Context, origin: dict[str, Any], tz: ZoneInfo) -> list[Fact]:
    then = ctx.now.astimezone(ZoneInfo(str(origin.get("tz") or "UTC"))).utcoffset()
    here = ctx.now.astimezone(tz).utcoffset()
    if then is None or here is None:
        return []
    diff_h = (here - then).total_seconds() / 3600
    values: dict[str, Any] = {"near": ctx.near, "origin": _short_near(origin)}
    if diff_h == 0:
        return [Fact("journey", "live", "journey.same_clock", values, source_name="Computed")]
    amount = abs(diff_h)
    hours = f"{amount:g} hour" + ("" if amount == 1 else "s")
    values |= {
        "hours": hours,
        "direction": "ahead of" if diff_h > 0 else "behind",
        "direction_short": "forward" if diff_h > 0 else "back",
    }
    return [Fact("journey", "live", "journey.clock", values, source_name="Computed")]


def country(ctx: Context) -> list[Fact]:
    """Static country basics shipped in the region profile (no REST Countries dependency)."""
    c = ctx.profile.get("country_facts") or {}
    name = ctx.country_name
    if not c or not name:
        return []
    wiki = f"https://en.wikipedia.org/wiki/{quote(ctx.country.replace(' ', '_'))}"
    out: list[Fact] = []

    def add(key: str, **values: Any) -> None:
        out.append(Fact("country", "evergreen", f"country.{key}", {"country": name, **values}, "Wikipedia", wiki))

    if c.get("capital") and c["capital"] != name:
        add("capital", capital=c["capital"])
    if langs := c.get("languages"):
        words = langs[0] if len(langs) == 1 else ", ".join(langs[:-1]) + " and " + langs[-1]
        add("languages", languages=words, lang_word="language" if len(langs) == 1 else "languages")
    if c.get("currency") and c.get("currency_name"):
        add("currency", currency=c["currency"], currency_name=c["currency_name"])
    if c.get("drives_on") in ("left", "right"):
        add("drives", side=c["drives_on"])
    if c.get("calling_code"):
        add("calling", calling_code=c["calling_code"])
    return out


def evergreen(ctx: Context) -> list[Fact]:
    out = []
    for item in ctx.profile.get("evergreen_facts") or []:
        if not item.get("text") or not item.get("id"):
            continue
        out.append(
            Fact(
                str(item.get("category") or "culture"),
                "evergreen",
                "evergreen.text",
                {"text": item["text"]},
                source_name=str(item.get("source_name") or "Curated"),
                source_url=str(item.get("source_url") or ""),
                pack=True,
                id=str(item["id"]),
            )
        )
    return out


# --- network adapters ------------------------------------------------------------

_SKY = {
    "clearsky": "clear skies",
    "fair": "mostly clear skies",
    "partlycloudy": "partly cloudy skies",
    "cloudy": "overcast skies",
    "fog": "fog",
    "lightrain": "light rain",
    "rain": "rain",
    "heavyrain": "heavy rain",
    "lightrainshowers": "light showers",
    "rainshowers": "rain showers",
    "heavyrainshowers": "heavy showers",
    "lightsleet": "light sleet",
    "sleet": "sleet",
    "heavysleet": "heavy sleet",
    "lightsleetshowers": "sleet showers",
    "sleetshowers": "sleet showers",
    "heavysleetshowers": "heavy sleet showers",
    "lightsnow": "light snow",
    "snow": "snow",
    "heavysnow": "heavy snow",
    "lightsnowshowers": "snow flurries",
    "snowshowers": "snow showers",
    "heavysnowshowers": "heavy snow showers",
}
_ROUGH = ("heavy", "thunder", "snow", "sleet")
WIND_WORTH_KMH = 15  # calmer than this is not worth a statement


def weather(ctx: Context) -> list[Fact]:
    """MET Norway Locationforecast (CC BY 4.0; requires an identifying User-Agent)."""
    p = ctx.profile
    data = ctx.fetch(
        f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={p['lat']:.4f}&lon={p['lon']:.4f}"
    )
    series = data["properties"]["timeseries"]
    # The first entry is the current hour (or the most recent one before it).
    step = series[0]
    details = step["data"]["instant"]["details"]
    temp = float(details["air_temperature"])
    code = str(((step["data"].get("next_1_hours") or {}).get("summary") or {}).get("symbol_code", ""))
    base = re.sub(r"_(day|night|polartwilight)$", "", code)
    cond = _SKY.get(base) or ("thunderstorms" if "thunder" in base else "")
    out = []
    if cond:
        rare = (1.0 if any(w in base for w in _ROUGH) else 0.0) + (1.0 if temp >= 35 or temp <= -10 else 0.0)
        values = {"near": ctx.near, "c": round(temp), "f": round(temp * 9 / 5 + 32), "cond": cond, "hour": step["time"]}
        out.append(
            Fact(
                "weather",
                "live",
                "weather.now",
                values,
                now=True,
                rarity=rare,
                source_name="MET Norway",
                source_url="https://api.met.no/",
            )
        )
    if (wind := details.get("wind_speed")) is not None:
        kmh = round(float(wind) * 3.6)
        if kmh >= WIND_WORTH_KMH:
            values = {"near": ctx.near, "kmh": kmh, "mph": round(kmh / 1.609), "hour": step["time"]}
            out.append(
                Fact(
                    "weather",
                    "live",
                    "weather.wind",
                    values,
                    rarity=1.0 if kmh >= 50 else 0.0,
                    source_name="MET Norway",
                    source_url="https://api.met.no/",
                )
            )
    return out


QUAKE_RADIUS_KM = 800
QUAKE_MIN_MAG = 2.5


def quakes(ctx: Context) -> list[Fact]:
    """USGS earthquake catalog (public domain): the strongest quake nearby in the past week."""
    p = ctx.profile
    since = (ctx.now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    data = ctx.fetch(
        "https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
        f"&latitude={p['lat']}&longitude={p['lon']}&maxradiuskm={QUAKE_RADIUS_KM}"
        f"&minmagnitude={QUAKE_MIN_MAG}&starttime={since}&orderby=magnitude&limit=1"
    )
    base = {"near": ctx.near, "city": _short_near(p), "radius": f"{QUAKE_RADIUS_KM:,}"}
    features = data.get("features") or []
    if not features:
        week = ctx.now.strftime("%G-W%V")
        values = {**base, "min": QUAKE_MIN_MAG, "week": week}
        return [
            Fact("quake", "today", "quake.none", values, source_name="USGS", source_url="https://earthquake.usgs.gov/")
        ]
    q = features[0]["properties"]
    mag = float(q["mag"])
    age_h = (ctx.now.timestamp() * 1000 - float(q["time"])) / 3_600_000
    rare = 2.0 if mag >= 6 else 1.0 if mag >= 4.5 else 0.0
    values = {
        **base,
        "mag": f"{mag:.1f}",
        "place": str(q.get("place") or "location not given"),
        "event": features[0].get("id", ""),
    }
    return [
        Fact(
            "quake",
            "live" if age_h < 24 else "today",
            "quake.strongest",
            values,
            source_name="USGS",
            source_url=str(q.get("url") or "https://earthquake.usgs.gov/"),
            rarity=rare,
        )
    ]


def onthisday(ctx: Context) -> list[Fact]:
    """Wikipedia's selected anniversaries for the local date. Titles only, in our own words."""
    local = _local(ctx)
    data = ctx.fetch(f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{local:%m}/{local:%d}")
    keys = {ctx.country.lower(), str(ctx.profile.get("city") or "").lower()} - {""}
    out = []
    for event in data.get("selected") or []:
        pages = event.get("pages") or []
        if not pages or "year" not in event:
            continue
        page = pages[0]
        title = str((page.get("titles") or {}).get("normalized") or page.get("title", "")).replace("_", " ")
        url = str(((page.get("content_urls") or {}).get("desktop") or {}).get("page") or "")
        # Only anniversaries tied to this place: a bare title says nothing on its own,
        # and Wikipedia's event sentence is not ours to copy.
        hit = next((k for k in keys if k in str(event.get("text", "")).lower()), None)
        if not title or len(title) > 80 or not hit:
            continue
        place = ctx.profile.get("city") if hit != ctx.country.lower() else ctx.country_name
        place_pos = f"{place}'" if str(place).endswith("s") else f"{place}'s"
        values = {
            "year": int(event["year"]),
            "title": title,
            "place": place,
            "place_pos": place_pos,
            "date": f"{local:%m-%d}",
        }
        out.append(
            Fact(
                "onthisday",
                "today",
                "onthisday.event",
                values,
                source_name="Wikipedia",
                source_url=url,
                rarity=1.0,
            )
        )
    return out


_SKIP_TITLE = re.compile(r"^(List of|Index of)|\(disambiguation\)", re.I)


def nearby(ctx: Context) -> list[Fact]:
    """Wikipedia geosearch around the region's map point."""
    p = ctx.profile
    data = ctx.fetch(
        "https://en.wikipedia.org/w/api.php?action=query&list=geosearch&format=json&formatversion=2"
        f"&gscoord={p['lat']}%7C{p['lon']}&gsradius=10000&gslimit=30"
    )
    places = [g for g in data["query"]["geosearch"] if not _SKIP_TITLE.search(str(g.get("title", "")))]
    rng = random.Random(f"{ctx.hop_id}:nearby")
    out = []
    for g in rng.sample(places, min(3, len(places))):
        km = max(1, round(float(g["dist"]) / 1000))
        out.append(
            Fact(
                "nearby",
                "evergreen",
                "nearby.place",
                {"title": g["title"], "km": km},
                source_name="Wikipedia",
                source_url=f"https://en.wikipedia.org/?curid={g['pageid']}",
            )
        )
    return out


def holidays(ctx: Context) -> list[Fact]:
    """Nager.Date public holidays for the country: today, or the next one within 45 days."""
    cc = ctx.profile.get("country_code")
    if not cc:
        return []
    data = ctx.fetch(f"https://date.nager.at/api/v3/NextPublicHolidays/{cc}")
    today = _local(ctx).date()
    for h in data:
        if h.get("global") is False:
            continue  # regional holidays: not true for the whole country
        day = datetime.fromisoformat(str(h["date"])).date()
        name = str(h.get("name") or h.get("localName"))
        if day == today:
            return [
                Fact(
                    "holiday",
                    "today",
                    "holiday.today",
                    {"name": name, "country": ctx.country_name, "date": str(day)},
                    rarity=3.0,
                    source_name="Nager.Date",
                    source_url="https://date.nager.at/",
                )
            ]
        days = (day - today).days
        if 0 < days <= 45:
            values = {"name": name, "country": ctx.country_name, "days": days}
            return [
                Fact(
                    "holiday",
                    "today",
                    "holiday.soon",
                    values,
                    rarity=0.5 if days <= 7 else 0.0,
                    source_name="Nager.Date",
                    source_url="https://date.nager.at/",
                )
            ]
        if days > 45:
            break
    return []


def fx(ctx: Context) -> list[Fact]:
    """Frankfurter (ECB reference rates): the local currency against the US dollar."""
    c = ctx.profile.get("country_facts") or {}
    cur = c.get("currency")
    if not cur or cur == "USD":
        return []
    data = ctx.fetch(f"https://api.frankfurter.dev/v1/latest?base=USD&symbols={cur}")
    rate = float(data["rates"][cur])
    shown = f"{rate:,.2f}" if rate < 100 else f"{rate:,.0f}"
    values = {
        "rate": shown,
        "currency": cur,
        "currency_name": c.get("currency_name") or cur,
        "date": data.get("date", ""),
    }
    return [
        Fact(
            "money", "today", "money.fx", values, source_name="Frankfurter (ECB)", source_url="https://frankfurter.dev/"
        )
    ]


LOCAL: dict[str, Adapter] = {"computed": computed, "country": country, "evergreen": evergreen}
NETWORK: dict[str, Adapter] = {
    "weather": weather,
    "quakes": quakes,
    "onthisday": onthisday,
    "nearby": nearby,
    "holidays": holidays,
    "fx": fx,
}
