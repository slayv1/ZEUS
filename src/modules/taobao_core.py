"""Модуль интеграции с Taobao для ассистента «Зевс».

Этап 1 — интерактивный веб-поиск: открывает выдачу Taobao по запросу
пользователя в браузере под текущей авторизованной сессией. Запрос
кодируется в UTF-8 (``urllib.parse.quote``), чтобы избежать иероглифов
и битых ссылок.

Потокобезопасность: вся сетевая/брaузерная работа выполняется в фоновом
daemon-потоке, чтобы UI Flet не блокировался и не показывал «Working…».

Будущие этапы (задел архитектуры):
  - Этап 2: парсинг цен/картинок и «добавить в корзину» через Selenium/Playwright.
  - Этап 3: официальный API Taobao / Alibaba Open Platform (App Key + App Secret,
    OAuth/HMAC-подпись, программный поиск по SKU, остатки, цены в юанях, заказы).
"""

from __future__ import annotations

import threading
import urllib.parse
import webbrowser

from modules.taobao_translator import translate_to_chinese

# Базовый URL поисковой выдачи Taobao.
TAOBAO_SEARCH_URL = "https://s.taobao.com/search?q={encoded}"

# Служебные фразы, которые нужно вырезать из голосовой команды.
# Порядок важен: сначала длинные словосочетания (в т.ч. искажённые распознавателем
# варианты «на таобао» -> «натал бал», «на таю балу»), затем одиночные слова.
_SERVICE_PHRASES = (
    "зеве", "зевс", "пожалуйста", "да сэр", "сэр",
    "на таобао", "с таобао", "на тао",
    "на таю балу", "на тау балу", "на таю", "на тау", "натал бал", "натал",
    "найдется", "таобао", "тао", "таю", "тау", "бао", "балу",
    "найди мне", "найди", "поищи", "поиск", "покажи", "найда", "ищи",
    "товар", "товары", "товар на", "купить", "купи", "цена", "найду себе",
)

# Последний обработанный запрос (диагностика/тесты).
_last_query: dict[str, str] = {}


def clean_query(query: str) -> str:
    """Очищает запрос от служебных фраз и лишних пробелов.

    Пример: «найди пуховик на таобао» -> «пуховик».
    """
    q = (query or "").strip().lower()
    for phrase in _SERVICE_PHRASES:
        q = q.replace(phrase, " ")
    # Схлопываем повторяющиеся пробелы и убираем краевые знаки препинания.
    parts = q.split()
    q = " ".join(parts).strip(" -.,!?")
    return q


def search_taobao_smart(query: str) -> None:
    """Интеллектуальный поиск по Taobao (асинхронно, без блокировки UI).

    Формирует закодированную ссылку и открывает её в браузере. Выполняется
    в фоновом daemon-потоке, чтобы Flet не показывал «Working…».
    """
    def _run() -> None:
        _do_search(query)

    threading.Thread(target=_run, daemon=True, name="zeus-taobao").start()


def _do_search(query: str) -> None:
    """Синхронное ядро поиска: очистка -> перевод -> кодирование -> браузер."""
    cleaned = clean_query(query)
    if not cleaned:
        print("[Taobao] Ошибка: Пустой запрос после очистки")
        return

    # Перевод на китайский: поиск Taobao по кириллице даёт пустую выдачу.
    translated = translate_to_chinese(cleaned)
    if translated != cleaned:
        print(f"[Taobao] Перевод: '{cleaned}' -> '{translated}'")

    encoded = urllib.parse.quote(translated)
    url = TAOBAO_SEARCH_URL.format(encoded=encoded)
    _last_query["query"] = cleaned
    _last_query["translated"] = translated
    _last_query["url"] = url

    try:
        webbrowser.open(url)
        print(f"[Taobao] Запрос отправлен: '{cleaned}' -> {url}")
    except Exception as exc:  # noqa: BLE001
        print(f"[Taobao] Ошибка открытия браузера: {exc}")


async def search_taobao_async(query: str) -> None:
    """Асинхронная версия для async-обработчиков Flet (asyncio.create_task).

    Все блокирующие операции (сетевой перевод, открытие браузера) выносятся
    через asyncio.to_thread — event loop не блокируется, и UI мгновенно
    освобождается. ВАЖНО: нельзя вызывать sync-версии прямо внутри корутины —
    это заблокирует цикл событий так же, как и главный поток.
    """
    import asyncio

    # Мгновенно возвращаем управление циклу событий, чтобы Flet успел
    # сбросить любые индикаторы загрузки.
    await asyncio.sleep(0)

    await asyncio.to_thread(_do_search, query)


def get_last_query() -> dict[str, str]:
    """Возвращает последний обработанный запрос (query + url)."""
    return dict(_last_query)
