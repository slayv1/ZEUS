"""Гибридный модуль Steam (веб-компонент): поиск в магазине, цены, корзина.

Компонент №2 гибридной схемы:
  - нативный запуск установленных игр остаётся в actions.py (steam:// URI);
  - здесь — публичный эндпоинт поиска магазина
    https://store.steampowered.com/api/storesearch/
    и открытие страниц/корзины в браузере под авторизованной сессией.

Все сетевые операции выполняются только из фоновых потоков
(zeus-commands) с жёсткими таймаутами — UI никогда не блокируется.
"""
from __future__ import annotations

import functools
import json
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

STORE_BASE = "https://store.steampowered.com"
SEARCH_API = f"{STORE_BASE}/api/storesearch/"
HEADERS = {"User-Agent": "Zeus-Assistant/1.1"}
DEFAULT_TIMEOUT = 6.0

# Кеш поисковых запросов: повторный «найди киберпанк» не бомбит сеть каждый раз.
# Ключ — нормализованный запрос + регион/язык; значение живёт _SEARCH_CACHE_TTL секунд.
_SEARCH_CACHE: dict[str, dict[str, Any]] = {}
_SEARCH_CACHE_TTL = 300.0


def _format_price(cents: int, currency: str) -> str:
    """Форматирует цену из центов Valve в человекочитаемый вид."""
    value = (cents or 0) / 100.0
    symbol = {
        "RUB": "₽", "USD": "$", "EUR": "€", "UAH": "₴",
        "KZT": "₸", "TRY": "₺", "BRL": "R$",
    }.get((currency or "").upper(), f" {currency}" if currency else "")
    if value == 0:
        return "бесплатно"
    return f"{value:,.0f}{symbol}".replace(",", " ")


def _cache_key(query: str, cc: str, lang: str) -> str:
    return f"{query.strip().lower()}|{cc}|{lang}"


def _cache_get(key: str) -> list[dict[str, Any]] | None:
    entry = _SEARCH_CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["ts"] > _SEARCH_CACHE_TTL:
        _SEARCH_CACHE.pop(key, None)
        return None
    return entry["items"]


def _cache_set(key: str, items: list[dict[str, Any]]) -> None:
    _SEARCH_CACHE[key] = {"ts": time.time(), "items": items}


def clear_search_cache() -> int:
    """Сбрасывает кеш поисковых запросов (удобно в тестах/отладке)."""
    n = len(_SEARCH_CACHE)
    _SEARCH_CACHE.clear()
    return n


def search_store(
    query: str,
    limit: int = 5,
    cc: str = "ru",
    lang: str = "russian",
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Ищет игры в магазине Steam (с TTL-кешем и скидками).

    Returns:
        Список словарей {id, name, price_final, price_str, currency, discount_percent,
        url}. Пустой список — ничего не найдено.
    Raises:
        RuntimeError — сетевой сбой (текст безопасен для TTS).
    """
    key = _cache_key(query, cc, lang)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    params = urllib.parse.urlencode({"term": query, "cc": cc, "l": lang})
    url = f"{SEARCH_API}?{params}"
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Магазин Steam недоступен, сэр. Проверьте подключение к интернету."
        ) from exc

    items: list[dict[str, Any]] = []
    payload = data if isinstance(data, dict) else {}
    for it in (payload.get("items", []) or [])[: max(1, limit)]:
        price = it.get("price") or {}
        if price:
            cents = price.get("final", 0)
            currency = price.get("currency", "RUB")
            price_str = _format_price(cents, currency)
        else:
            cents, currency, price_str = 0, "", "цена не указана"
        app_id = it.get("id")
        discount = price.get("discount_percent") if price else None
        items.append(
            {
                "id": app_id,
                "name": it.get("name", "?"),
                "price_final": cents,
                "price_str": price_str,
                "currency": currency,
                "discount_percent": discount,
                "url": f"{STORE_BASE}/app/{app_id}/",
            }
        )
    _cache_set(key, items)
    return items


@functools.lru_cache(maxsize=8)
def find_best_match(
    query: str, limit: int = 5, cc: str = "ru", lang: str = "russian"
) -> dict[str, Any] | None:
    """Лучшая игра по запросу или ``None`` (использует кеш поиска + LRU)."""
    results = search_store(query, limit=limit, cc=cc, lang=lang)
    return results[0] if results else None


def open_store_page(app_id: Any) -> None:
    """Открывает страницу игры в браузере (авторизованная сессия)."""
    webbrowser.open(f"{STORE_BASE}/app/{app_id}/")


def open_store_search(query: str) -> None:
    """Открывает страницу поиска магазина в браузере."""
    webbrowser.open(f"{STORE_BASE}/search/?term={urllib.parse.quote_plus(query)}")


def open_cart() -> None:
    """Открывает корзину магазина в браузере."""
    webbrowser.open(f"{STORE_BASE}/cart/")


def add_to_cart_via_session(app_id: Any) -> None:
    """Добавляет товар в корзину через веб-сессию браузера.

    Открывает страницу игры в браузере пользователя: Valve авторизует
    сессию по cookie, добавление выполняется кнопкой на странице
    (GET-эндпоинт addtorecart требует подпись и без сессии не работает).
    """
    webbrowser.open(f"{STORE_BASE}/app/{app_id}/")
