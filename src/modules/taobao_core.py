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

import re
import sys
import threading
import urllib.parse
import webbrowser

from modules.taobao_translator import translate_to_chinese, _looks_like_error


def _safe_print(text: str) -> None:
    """print, устойчивый к консоли с не-UTF-8 кодировкой (cp1251/cp866).

    Варианты запуска с перенаправленным stdout (логи, noconsole-обёртки)
    могут иметь кодировку, в которой кириллица/китайский не кодируются:
    обычный print кидал UnicodeEncodeError и убивал фоновый поток taobao
    ДО открытия браузера. Некодируемые символы заменяются, а не роняют поток.
    """
    stream = sys.stdout
    enc = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        stream.write(text + "\n")
    except (UnicodeEncodeError, LookupError):
        stream.write(text.encode(enc, errors="replace").decode(enc, errors="replace") + "\n")
    except Exception:  # noqa: BLE001 — диагностика не должна ронять поток
        return
    try:
        stream.flush()
    except Exception:  # noqa: BLE001
        pass

# Базовый URL поисковой выдачи Taobao.
# КРИТИЧНО: s.taobao.com отдаёт Error 500 из-за рубежа (не пускает без
# китайского IP/капчи). world.taobao.com — международная витрина, работает
# из России без VPN и понимает и те же q= запросы.
TAOBAO_SEARCH_URL = "https://world.taobao.com/search/search.htm?q={encoded}"

# Служебные фразы, которые нужно вырезать из голосовой команды.
# Порядок важен: сначала длинные словосочетания (в т.ч. искажённые распознавателем
# варианты «на таобао» -> «натал бал», «на таю балу»), затем одиночные слова.
#
# ВАЖНО: это НЕ подстроки, а отдельные слова — совпадения ищутся по границам
# слов (re), чтобы «купить носки» не превращалось в «купить ски».
_SERVICE_PHRASES = (
    "зеве", "зевс", "пожалуйста", "да сэр", "сэр",
    "на таобао", "с таобао", "на тао",
    "на таю балу", "на тау балу", "на таю", "на тау", "натал бал", "натал",
    "найдется", "таобао", "тао", "таю", "тау", "бао", "балу",
    "найди мне", "найди", "поищи", "поиск", "покажи", "найда", "ищи",
    "товар", "товары", "товар на", "купить", "купи", "цена", "найду себе",
)

_SERVICE_RE = re.compile(
    "|".join(sorted((re.escape(p) for p in _SERVICE_PHRASES), key=len, reverse=True)),
)

# Последний обработанный запрос (диагностика/тесты).
_last_query: dict[str, str] = {}


def clean_query(query: str) -> str:
    """Очищает запрос от служебных фраз и лишних пробелов.

    Пример: «найди пуховик на таобао» -> «пуховик».

    Совпадения ищутся по границам слов (пробел/начало/конец/пунктуация),
    а не подстрокой — иначе «тао» выедало бы кусок слова «готов»,
    а «бао» — слово «носки» («купить носки» -> «купить ски»).
    """
    q = f" {(query or '').strip().lower()} "
    q = _SERVICE_RE.sub(" ", q)
    # Схлопываем повторяющиеся пробелы и убираем краевые знаки препинания.
    parts = q.split()
    return " ".join(parts).strip(" -.,!?")


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
        _safe_print("[Taobao] Ошибка: Пустой запрос после очистки")
        return

    # Перевод на китайский: поиск Taobao по кириллице даёт пустую выдачу.
    translated = translate_to_chinese(cleaned)
    # Гигиена: никогда не подставляем в URL текст, похожий на ошибку сервиса
    # (см. скрин: в q= попал "Error...1500...try again later"). Если перевод
    # подозрительный — используем очищенный оригинал вместо него.
    if translated != cleaned:
        if _looks_like_error(translated):
            _safe_print(f"[Taobao] Подозрительный перевод, использую оригинал: '{cleaned}'")
            translated = cleaned
        else:
            _safe_print(f"[Taobao] Перевод: '{cleaned}' -> '{translated}'")

    encoded = urllib.parse.quote(translated, safe="")
    url = TAOBAO_SEARCH_URL.format(encoded=encoded)
    _last_query["query"] = cleaned
    _last_query["translated"] = translated
    _last_query["url"] = url

    try:
        webbrowser.open(url)
        _safe_print(f"[Taobao] Запрос отправлен: '{cleaned}' -> {url}")
    except Exception as exc:  # noqa: BLE001
        _safe_print(f"[Taobao] Ошибка открытия браузера: {exc}")


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
