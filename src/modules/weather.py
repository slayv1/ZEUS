# -*- coding: utf-8 -*-
"""Голосовая погода через Open-Meteo (ТЗ websearch_weather §2.2).

Бесплатный API без ключа и лимитов, две цепочки вызовов:
  1. Геокодинг:  geocoding-api.open-meteo.com/v1/search?name={город}
  2. Прогноз:    api.open-meteo.com/v1/forecast?latitude=..&longitude=..

Зависимости — только stdlib (urllib.request + json), как ТЗ и требовало:
ни одной HTTP-библиотеки в requirements.txt ради одной фичи (тем же
способом скачивается модель Vosk в wake_word.py).

Любая сетевая ошибка (нет интернета / таймаут / битый JSON / город не
найден) — success: False, хендлер в actions.py тихо падает на MVP-вариант
(открыть браузер с погодой Google), пользователь всё равно получает ответ.

Ограничение (осознанное): пользователь говорит город в косвенном падеже
(«погода в Москве»), геокодинг ищет по именительному. geocode_city
перебирает наивные формы («москве» -> «москва», «омске» -> «омск»);
несклоняемые («сочи») находятся сразу. Если ни одна форма не нашлась —
фолбэк на браузер: Google падежи понимает сам.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_HTTP_TIMEOUT = 5.0  # секунд на один запрос

# WMO weather interpretation codes (Open-Meteo) -> описание по-русски
_WMO_CODES = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь",
    51: "слабая морось",
    53: "морось",
    55: "сильная морось",
    56: "ледяная морось",
    57: "сильная ледяная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "сильный ледяной дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    77: "снежные зёрна",
    80: "небольшой ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "снежный ливень",
    86: "сильный снежный ливень",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза с градом",
}


def _http_get_json(url: str) -> dict[str, Any] | None:
    """GET по URL с таймаутом; None при любой сетевой ошибке/битом JSON."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ZeusAssistant/1.4"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — сеть/таймаут/парсинг: тихий отказ
        return None


def _city_candidates(city: str) -> list[str]:
    """Наивные варианты имени города из косвенного падежа (см. docstring модуля).

    «москве» -> [«москве», «москв», «москва», «москвь»]; несклоняемые
    города («сочи») находятся уже первым вариантом.
    """
    c = (city or "").strip()
    if not c:
        return []
    variants = [c]
    stem = c[:-1]
    if len(stem) >= 3:
        variants.append(stem)            # омске -> омск
        variants.append(stem + "а")      # москве -> москва, туле -> тула
        variants.append(stem + "ь")      # твери/казани -> тверь/казань
    # Убираем дубли, сохраняя порядок
    seen: set[str] = set()
    return [v for v in variants if not (v in seen or seen.add(v))]


def geocode_city(city: str) -> dict[str, Any] | None:
    """Город -> {name, country, latitude, longitude} или None (не найден/сеть)."""
    for candidate in _city_candidates(city):
        url = (
            f"{_GEOCODING_URL}?"
            + urllib.parse.urlencode(
                {"name": candidate, "count": 1, "language": "ru", "format": "json"}
            )
        )
        data = _http_get_json(url)
        if not data:
            return None  # сеть недоступна — перебирать формы бессмысленно
        results = data.get("results")
        if not isinstance(results, list) or not results:
            continue  # эта форма не нашлась — пробуем следующую
        top = results[0]
        try:
            return {
                "name": str(top.get("name", candidate)),
                "country": str(top.get("country", "")),
                "latitude": float(top.get("latitude")),
                "longitude": float(top.get("longitude")),
            }
        except (TypeError, ValueError):
            continue
    return None


def describe_wmo(code: Any) -> str | None:
    """Числовой WMO-код Open-Meteo -> русское описание явления."""
    try:
        return _WMO_CODES.get(int(code))
    except (TypeError, ValueError):
        return None


def _plural_grads(n: int) -> str:
    """«1 градус / 2 градуса / 5 градусов» (учёт -11..-14)."""
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return "градусов"
    d = n % 10
    if d == 1:
        return "градус"
    if 2 <= d <= 4:
        return "градуса"
    return "градусов"


def get_current_weather(city: str) -> dict[str, Any]:
    """Текущая погода в городе: «Сейчас в Москве 15 градусов, облачно, сэр».

    Успех: {"success": True, "message", "city", "temperature", "description"}.
    Отказ (нет сети / город не найден / неполный ответ):
    {"success": False, "message"} — хендлер тихо падает на MVP
    (открыть браузер), не показывая пользователю голую ошибку.
    """
    geo = geocode_city(city)
    if geo is None:
        return {
            "success": False,
            "message": f"Не нашёл город «{city}», сэр.",
            "city": city,
        }

    url = (
        f"{_FORECAST_URL}?"
        + urllib.parse.urlencode(
            {
                "latitude": geo["latitude"],
                "longitude": geo["longitude"],
                "current_weather": "true",
            }
        )
    )
    data = _http_get_json(url)
    if not data:
        return {
            "success": False,
            "message": "Сервис погоды недоступен, сэр.",
            "city": city,
        }

    current = data.get("current_weather") or {}
    try:
        temp = round(float(current.get("temperature")))
    except (TypeError, ValueError):
        return {
            "success": False,
            "message": "Сервис погоды вернул неполные данные, сэр.",
            "city": city,
        }

    desc = describe_wmo(current.get("weathercode")) or "данные о явлении недоступны"
    place = geo["name"]
    if geo.get("country"):
        place = f"{place} ({geo['country']})"
    message = f"Сейчас в {place} {temp} {_plural_grads(temp)}, {desc}, сэр."
    return {
        "success": True,
        "message": message,
        "city": city,
        "temperature": temp,
        "description": desc,
    }
