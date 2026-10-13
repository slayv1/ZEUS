"""Тесты веб-поиска и погоды (ТЗ ZEUS_websearch_weather_TZ.md §3).

execute_command() и модуль weather вызываются по-настоящему; подменяются
только внешние эффекты: webbrowser.open (запись URL вместо открытия браузера),
сетевые запросы Open-Meteo и фоновые сервисы (taobao/steam) — чтобы тесты
были быстрыми и не зависели от интернета.
"""
import os
import time

import pytest

from core import actions, actions_search
from modules import weather


@pytest.fixture(autouse=True)
def no_pending_drive_scan(monkeypatch):
    """Страховка: execute_command() первым делом проверяет _pending_drive_scan.

    Если какой-либо тест оставит здесь «E:» (ответ на вопрос о сканировании
    флешки), execute_command вернёт переспрос вместо разбора нашей команды.
    Изоляция явная — тесты не зависят от порядка выполнения.
    """
    monkeypatch.setattr(actions_search, "_pending_drive_scan", None)


@pytest.fixture
def opened_urls(monkeypatch):
    """Подмена webbrowser.open: собираем URL, реальный браузер не открываем."""
    urls: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: urls.append(url) or True)
    return urls


def _wait_for_urls(urls: list[str], count: int = 1, timeout: float = 5.0) -> None:
    """Ждёт, пока фоновый поток веб-поиска откроет URL."""
    deadline = time.time() + timeout
    while len(urls) < count and time.time() < deadline:
        time.sleep(0.01)
    assert len(urls) >= count, f"браузер не открыт за {timeout} с: {urls}"


# ----------------------------------------------------------------------
# Веб-поиск (ТЗ §1)
# ----------------------------------------------------------------------
def test_google_search_extracts_query_and_url(opened_urls):
    result = actions.execute_command("погугли курс доллара")

    assert result["success"] is True
    assert result["action"] == "websearch"
    assert result["query"] == "курс доллара"
    assert result["url"].startswith("https://www.google.com/search?q=")
    assert "%D0%BA%D1%83%D1%80%D1%81" in result["url"]  # запрос закодирован (quote)

    _wait_for_urls(opened_urls)
    assert opened_urls == [result["url"]]  # браузер получил ровно этот URL


@pytest.mark.parametrize(
    ("phrase", "expected_query"),
    [
        ("поищи в интернете лучшие мониторы 2026", "лучшие мониторы 2026"),
        ("найди в интернете реферат по физике", "реферат по физике"),
        ("загугли погоду завтра", "погоду завтра"),
    ],
)
def test_websearch_marker_variants(opened_urls, phrase, expected_query):
    result = actions.execute_command(phrase)

    assert result["action"] == "websearch", result
    assert result["query"] == expected_query, result


def test_websearch_without_query_returns_error():
    result = actions.execute_command("погугли")

    assert result is not None
    assert result["success"] is False
    assert result["action"] == "websearch"


def test_local_search_is_not_hijacked():
    # «найди chrome» — локальный поиск программ, не веб-поиск
    result = actions.execute_command("найди chrome")
    assert result is not None and result["action"] != "websearch"


def test_taobao_and_steam_still_win_their_phrases(opened_urls, monkeypatch):
    from core import steam_store
    from modules import taobao_core

    monkeypatch.setattr(taobao_core, "search_taobao_smart", lambda query: None)
    monkeypatch.setattr(steam_store, "search_store", lambda query, limit=3: [])
    monkeypatch.setattr(steam_store, "open_store_search", lambda query: True)

    taobao = actions.execute_command("найди пуховик на таобао")
    assert taobao["action"] == "taobao", taobao

    steam = actions.execute_command("найди в стиме хеду")
    assert steam["action"] == "steam_store", steam

    # «погугли сколько стоит …» — веб-поиск, а не магазин Steam
    websearch = actions.execute_command("погугли сколько стоит дота 2")
    assert websearch["action"] == "websearch", websearch


# ----------------------------------------------------------------------
# Погода (ТЗ §2.1, §2.2)
# ----------------------------------------------------------------------
def test_weather_without_city_asks_back(opened_urls):
    result = actions.execute_command("какая погода")

    assert result["success"] is True
    assert result["action"] == "weather"
    assert "город" in result["message"]
    assert opened_urls == []  # без города браузер не открываем


def test_weather_with_city_speaks_temperature(monkeypatch, opened_urls):
    monkeypatch.setattr(
        weather,
        "get_current_weather",
        lambda city: {
            "success": True,
            "message": f"Сейчас в {city} 15 градусов, ясно, сэр.",
            "temperature": 15,
        },
    )

    result = actions.execute_command("погода в Москве")

    assert result["success"] is True
    assert result["action"] == "weather"
    assert result["city"] == "москве"  # город в исходной форме
    assert result["voice"] is True
    assert "15 градусов" in result["message"]
    assert opened_urls == []  # голосовой ответ — браузер не открывается


def test_weather_falls_back_to_browser(monkeypatch, opened_urls):
    monkeypatch.setattr(
        weather, "get_current_weather", lambda city: {"success": False, "message": "недоступно"}
    )

    result = actions.execute_command("погода в Москве")

    assert result["success"] is True
    assert result["action"] == "weather"
    assert result["fallback"] is True
    _wait_for_urls(opened_urls)
    assert opened_urls[-1].startswith("https://www.google.com/search?q=")
    assert "%D0%BC%D0%BE%D1%81%D0%BA%D0%B2" in opened_urls[-1]  # «москва» в запросе


def test_weather_city_extraction():
    assert actions._extract_weather_city("какая погода в санкт-петербурге") == "санкт-петербурге"
    assert actions._extract_weather_city("погода в Москве") == "москве"


def test_weather_phrases_do_not_hijack_small_talk():
    # Обычная речь со словом «погода» уходит в LLM (None), браузер не открывается
    assert actions.execute_command("расскажи анекдот про погоду") is None
    assert actions.execute_command("погода — это интересная тема") is None


# ----------------------------------------------------------------------
# Модуль weather: чистые функции и сетевой слой с подменой
# ----------------------------------------------------------------------
def test_wmo_description_mapping():
    assert weather.describe_wmo(0) == "ясно"
    assert weather.describe_wmo(3) == "пасмурно"
    assert weather.describe_wmo(95) == "гроза"
    assert weather.describe_wmo(9999) is None
    assert weather.describe_wmo(None) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, "градус"), (2, "градуса"), (5, "градусов"), (15, "градусов"),
     (21, "градус"), (11, "градусов"), (-3, "градуса")],
)
def test_plural_grads(value, expected):
    assert weather._plural_grads(value) == expected


def test_city_candidates_cover_naive_cases():
    assert "москва" in weather._city_candidates("москве")
    assert "омск" in weather._city_candidates("омске")
    assert weather._city_candidates("сочи")[0] == "сочи"  # несклоняемое — сразу


def test_geocode_tries_next_candidate_when_first_not_found(monkeypatch):
    seen: list[str] = []

    def fake_http_get_json(url: str):
        seen.append(url)
        if len(seen) == 1:
            return {"results": []}  # первая форма не нашлась — пробуем следующую
        return {
            "results": [
                {"name": "Москва", "country": "Россия", "latitude": 55.75, "longitude": 37.62}
            ]
        }

    monkeypatch.setattr(weather, "_http_get_json", fake_http_get_json)
    geo = weather.geocode_city("москве")

    assert len(seen) >= 2
    assert geo["name"] == "Москва" and geo["country"] == "Россия"


def test_geocode_returns_none_without_network(monkeypatch):
    monkeypatch.setattr(weather, "_http_get_json", lambda url: None)
    assert weather.geocode_city("москва") is None


def test_get_current_weather_builds_voice_message(monkeypatch):
    monkeypatch.setattr(
        weather,
        "geocode_city",
        lambda city: {"name": "Москва", "country": "Россия", "latitude": 55.75, "longitude": 37.62},
    )
    monkeypatch.setattr(
        weather,
        "_http_get_json",
        lambda url: {"current_weather": {"temperature": 15.4, "weathercode": 0}},
    )

    result = weather.get_current_weather("москве")

    assert result["success"] is True
    assert result["temperature"] == 15  # 15.4 округляется
    assert "Москва (Россия)" in result["message"]
    assert "15 градусов" in result["message"]
    assert "ясно" in result["message"]


def test_get_current_weather_rejects_incomplete_response(monkeypatch):
    monkeypatch.setattr(
        weather,
        "geocode_city",
        lambda city: {"name": "Москва", "country": "Россия", "latitude": 1.0, "longitude": 2.0},
    )
    monkeypatch.setattr(weather, "_http_get_json", lambda url: {"current_weather": {}})

    assert weather.get_current_weather("москве")["success"] is False


@pytest.mark.skipif(
    not os.environ.get("ZEUS_NETWORK_TESTS"),
    reason="сетевые проверки отключены (запуск: ZEUS_NETWORK_TESTS=1)",
)
def test_real_network_weather_end_to_end(opened_urls):
    """Реальный Open-Meteo: голосовой ответ ИЛИ тихий фолбэк — оба валидны."""
    result = actions.execute_command("погода в Сочи")
    assert result["success"] is True, result

