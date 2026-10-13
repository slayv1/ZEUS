# -*- coding: utf-8 -*-
"""Поиск: интернет, погода, Taobao, Steam, локальный индекс и сканирование дисков."""
from __future__ import annotations

import ast
import datetime
import json
import math
import operator
import os
import random
import re
import subprocess
import sys
import threading
import urllib.parse
from typing import Any

import psutil

from core import config
from core.atomic_json import write_json_atomic

# --- Веб-поиск: триггеры (ТЗ websearch_weather §1.2) ---
_WEBSEARCH_MARKERS = (
    "погугли", "загугли", "поищи в интернете", "найди в интернете",
    "поиск в интернете", "погуглить", "загуглить",
)

# --- Погода MVP (ТЗ websearch_weather §2.1) ---
# Строгие вопросные паттерны: слово «погода» само по себе встречается в
# обычной речи («анекдот про погоду», «погода — это тема»), поэтому
# перехватываем только явные вопросы или «погода в {город}».
_WEATHER_QUESTION_RE = re.compile(
    r"(?:^|\s)("
    r"(?:какая|какова|как)\s+(?:сейчас\s+)?(?:погода|погодка|погоду)"  # какая погода
    r"|(?:скажи|покажи|посмотри|глянь|интересно)\s+(?:мне\s+)?погоду?"  # скажи погоду
    r"|погода[у]?\s+в\s+\w+"                                            # погода в Москве
    r"|(?:^|\s)погода[у]?\s*$"                                          # просто «погода»
    r")",
    re.IGNORECASE,
)

_WEATHER_TRIGGERS = ("погода", "погоду", "погоде", "погодка", "weather")

# Служебные фразы для извлечения города из погодной команды.
_WEATHER_SERVICE_PHRASES = (
    "зевс", "пожалуйста", "какая погода", "какая сейчас погода",
    "какая погода в", "скажи погоду в", "покажи погоду в",
    "какая погодка", "сколько градусов", "как погода",
    "какая погода сейчас", "погода в", "погоду в", "погода", "погоду",
    "погоде", "погодка", "сейчас", "в городе",
)

# Обращения/вводные, которые Vosk захватывает в начале фразы и которые
# иначе «прилипают» к городу (из логов 22.09: «офицер душанбе»,
# «саша москве» → geocode не находил город → уход в браузерный фолбэк).
# Вырезаются только как отдельные слова (\b), чтобы не резать города
# (например, «Даугавпилс» не заденет \bда\b).
_WEATHER_VOCATIVE_RE = re.compile(
    r"\b(?:сэр|сэры|офицер|саша|шеф|босс|"
    r"начальник|джарвис|зевс|зевес|зевсе|помощник|ассистент|"
    r"знаешь|да|ну|давай)\b",
    re.IGNORECASE,
)

# Русские названия городов могут содержать «ё», дефисы и т.д. — просто
# схлопываем пробелы и убираем краевую пунктуацию после вырезания служебных.
_WEATHER_CITY_RE = re.compile(r"\s+")


_STORE_LAUNCH_VERBS = ("запусти", "запустить", "играй", "играть", "давай сыграем")


def _extract_store_query(text: str) -> str | None:
    """Извлекает название игры из фразы про магазин Steam.

    Порядок вырезания: длинные словосочетания, затем одиночные ключевые слова.
    """
    q = (text or "").lower().strip()
    for phrase in (
        "зевс", "пожалуйста", "сколько стоит", "сколько будет стоить",
        "найди в стиме", "найди в steam", "найди", "поищи", "поиск",
        "покажи", "открой в стиме", "открой", "цена игры", "цена",
        "добавь в корзину", "добавить в корзину", "в корзину", "корзину",
        "в стиме", "в steam", "стиме", "steam", "стим", "магазин",
        "игру", "игра",
    ):
        q = q.replace(phrase, " ")
    q = re.sub(r"\s+", " ", q).strip(" -.,!?")
    return q or None


# === Веб-поиск и погода (ТЗ websearch_weather §1, §2.1) ===

def _extract_websearch_query(text: str, marker: str) -> str | None:
    """Вырезает из фразы триггер-маркер веб-поиска, оставляя сам запрос.

    Аналог clean_query() из taobao_core: вырезаем по подстроке маркера
    (маркеры веб-поиска достаточно уникальны), схлопываем пробелы.
    Пример: «погугли курс доллара» -> «курс доллара».
    """
    q = (text or "").lower()
    # Длинные маркеры вырезаем первыми, чтобы «найди в интернете» не оставляла «найди».
    for m in sorted(_WEBSEARCH_MARKERS, key=len, reverse=True):
        if m in q:
            q = q.replace(m, " ")
    q = q.replace(marker, " ")
    # Служебные слова-обвязка (не трогаем сам запрос)
    for svc in ("зевс", "пожалуйста", "сэр", "в интернете", "в гугле", "в google"):
        q = q.replace(svc, " ")
    q = re.sub(r"\s+", " ", q).strip(" -.,!?")
    return q or None


def _handle_websearch_command(text: str) -> dict[str, Any] | None:
    """Веб-поиск «погугли X» / «поищи в интернете X» (ТЗ websearch_weather §1).

    Открывает браузер с результатами Google (тот же паттерн, что taobao_core:
    webbrowser.open + urllib.parse.quote, фоновый daemon-поток). Результаты
    не читаются вслух — осознанное ограничение версии, как в видео-референсе.

    Возвращает None, если фраза не веб-поиск (не перехватывает обычную речь).
    """
    low = (text or "").lower()
    marker = next((m for m in _WEBSEARCH_MARKERS if m in low), None)
    if not marker:
        return None

    query = _extract_websearch_query(text, marker)
    if not query:
        return {
            "success": False,
            "message": "Не понял, что искать, сэр. Повторите запрос.",
            "action": "websearch",
        }

    from core.command_effects import open_web_search
    return open_web_search(query)


def _extract_weather_city(text: str) -> str | None:
    """Вырезает город из погодной фразы: «погода в Москве» -> «москве»."""
    q = (text or "").lower()
    # Длинные служебные фразы вырезаем первыми (аналог _extract_store_query)
    for phrase in sorted(_WEATHER_SERVICE_PHRASES, key=len, reverse=True):
        q = q.replace(phrase, " ")
    # Обращения («офицер», «саша», «сэр»...) — после служебных фраз и до
    # схлопывания пробелов: иначе они оседают в «городе» (см. логи 22.09).
    q = _WEATHER_VOCATIVE_RE.sub(" ", q)
    q = _WEATHER_CITY_RE.sub(" ", q).strip(" -.,!?")
    return q or None


def _handle_weather_command(text: str) -> dict[str, Any] | None:
    """Погода (ТЗ websearch_weather §2.1 MVP + §2.2 богатая версия).

    С городом: реальная температура голосом через Open-Meteo
    (src/modules/weather.py). Без города — переспрос «В каком городе, сэр?»
    (ТЗ §2.2: не геолоцировать по IP ради одной фичи).

    Тихий фолбэк (ТЗ §2.2): нет сети / город не найден / API недоступен —
    открываем браузер с Google-виджетом, а не показываем голую ошибку.

    Триггер — строгий вопросный паттерн (_WEATHER_QUESTION_RE): слово
    «погода» в обычной речи («анекдот про погоду») НЕ перехватывается.

    Возвращает None, если фраза не про погоду.
    """
    low = (text or "").lower()
    if not _WEATHER_QUESTION_RE.search(low):
        return None

    city = _extract_weather_city(text)
    if not city:
        # Погодный вопрос без города: явно переспрашиваем (ТЗ §2.2),
        # пользователь ответит «погода в Москве» следующим ходом.
        return {
            "success": True,
            "message": "В каком городе узнать погоду, сэр?",
            "action": "weather",
        }

    # --- Богатая версия: реальная температура через Open-Meteo ---
    try:
        from modules import weather
        result = weather.get_current_weather(city)
    except Exception:  # noqa: BLE001 — сеть/импорт: фолбэк ниже
        result = {"success": False}
    if result.get("success"):
        return {
            "success": True,
            "message": str(result.get("message", "")),
            "action": "weather",
            "city": city,
            "voice": True,  # погода произнесена голосом, браузер не открывался
        }

    # --- Тихий фолбэк на MVP (ТЗ §2.2): браузер с Google-виджетом ---
    from urllib.parse import quote

    from core.command_effects import open_url

    q = f"погода {city}".strip()
    url = f"https://www.google.com/search?q={quote(q)}"
    open_url(url)
    return {
        "success": True,
        "message": f"Онлайн-погода недоступна, сэр. Открываю браузер: погода в «{city}».",
        "action": "weather",
        "city": city,
        "url": url,
        "fallback": True,
    }


def _sounds_like_taobao(word: str) -> bool:
    """Фонетически похоже ли слово на «таобао»/«тао»/«бао» (устойчиво к Vosk)."""
    from difflib import SequenceMatcher
    refs = ("таобао", "тао", "тау", "бао", "балу")
    best = max((SequenceMatcher(None, word, ref).ratio() for ref in refs), default=0.0)
    return best >= 0.5


def _handle_taobao_command(text: str) -> dict[str, Any] | None:
    """Голосовой поиск по Taobao: «найди пуховик на таобао».

    Возвращает None, если фраза не относится к Taobao (чтобы обычная речь
    не перехватывалась). Распознавание устойчиво: ловит искажённые варианты
    «на таобао» — «натал бал», «на таю балу» и т.п. (Vosk часто коверкает
    китайское название маркетплейса). Запуск в фоне — браузер открывается
    асинхронно, UI Flet не блокируется.
    """
    low = (text or "").lower()
    has_marker = any(w in low for w in ("таобао", "тао", "таю", "тау", "taobao", "бао", "балу"))
    if not has_marker:
        # Устойчивость к искажённому распознаванию: Vosk часто коверкает
        # «на таобао» в «натал бал» / «на таю балу». Реагируем ТОЛЬКО на слово,
        # за которым идёт предлог «на», либо на слово с префиксом «на»
        # (склеенное «натал»), фонетически близкое к «таобао». Это исключает
        # обычные слова («контакты», «папку»), где поиск "просто" есть.
        search_verb = any(w in low for w in ("найди", "поищи", "ищи", "купить", "надо найти"))
        if search_verb:
            words = low.split()
            for i, w in enumerate(words):
                if len(w) < 2:
                    continue
                prev = words[i - 1] if i > 0 else ""
                after_na = prev in ("на", "найдем")
                starts_na = w.startswith("на") and len(w) >= 4
                if (after_na or starts_na) and _sounds_like_taobao(w):
                    has_marker = True
                    break
    if not has_marker:
        return None

    from modules import taobao_core

    query = taobao_core.clean_query(text)
    if not query:
        return {
            "success": False,
            "message": "Не понял, что искать на Taobao, сэр. Повторите запрос.",
            "action": "taobao",
        }

    taobao_core.search_taobao_smart(query)
    return {
        "success": True,
        "message": f"Ищу «{query}» на Taobao, сэр. Открываю страницу.",
        "action": "taobao",
        "query": query,
    }


def _handle_steam_store_command(text: str) -> dict[str, Any] | None:
    """Веб-компонент гибридного Steam: поиск, цены, корзина магазина.

    Возвращает None, если фраза не относится к магазину (локальный запуск
    установленной игры имеет приоритет и обрабатывается штатной веткой).
    Сетевые вызовы выполняются в потоке zeus-commands — UI не блокируется.
    """
    low = (text or "").lower()

    # Локальный запуск важнее веб-поиска
    if any(w in low for w in _STORE_LAUNCH_VERBS):
        return None

    has_store_context = any(
        w in low for w in ("стим", "steam", "корзин", "цена", "стоит", "магазин")
    )
    if not has_store_context:
        return None

    from core import steam_store

    query = _extract_store_query(text)

    # --- Корзина ---
    if "корзин" in low:
        if query and len(query) >= 3:
            try:
                items = steam_store.search_store(query, limit=1)
            except RuntimeError as exc:
                return {"success": False, "message": str(exc), "action": "steam_store"}
            if items:
                steam_store.add_to_cart_via_session(items[0]["id"])
                return {
                    "success": True,
                    "message": (
                        f"Открываю {items[0]['name']} для добавления в корзину, "
                        "сэр. Подтвердите добавление на странице."
                    ),
                    "action": "steam_store",
                }
            return {
                "success": False,
                "message": f"В магазине Steam не найдено «{query}», сэр.",
                "action": "steam_store",
            }
        steam_store.open_cart()
        return {
            "success": True,
            "message": "Открываю корзину Steam, сэр.",
            "action": "steam_store",
        }

    # --- Поиск / цена ---
    if not query:
        steam_store.open_store_search("")
        return {
            "success": True,
            "message": "Открываю магазин Steam, сэр.",
            "action": "steam_store",
        }

    try:
        items = steam_store.search_store(query, limit=3)
    except RuntimeError as exc:
        return {"success": False, "message": str(exc), "action": "steam_store"}

    if not items:
        return {
            "success": False,
            "message": f"В магазине Steam ничего не найдено по запросу «{query}», сэр.",
            "action": "steam_store",
        }

    top = items[0]
    steam_store.open_store_page(top["id"])
    listing = "; ".join(f"{i['name']} — {i['price_str']}" for i in items)
    return {
        "success": True,
        "message": f"Нашёл в Steam: {listing}. Открываю {top['name']}, сэр.",
        "action": "steam_store",
        "items": items,
    }


def _handle_find_command(name: str) -> dict[str, Any]:
    """Обрабатывает команду поиска программы.
    
    Args:
        name: имя программы для поиска
    
    Returns:
        dict с результатом поиска
    """
    try:
        # Импортируем здесь чтобы избежать циклического импорта
        from core.file_scanner import search_program, confirm_and_save
        
        # Запускаем поиск
        result = search_program(name)
        
        # Если индекс пустой — автоматически запускаем индексацию
        if not result["success"] and result.get("needs_indexing"):
            # Запускаем индексацию в фоне
            from core.file_scanner import rebuild_system_index
            rebuild_result = rebuild_system_index()
            
            return {
                "success": True,
                "message": f"{rebuild_result['message']} Я сообщу, когда закончу, сэр.",
                "action": "file_search_indexing",
            }
        
        if result["success"] and result["paths"]:
            paths = result["paths"]
            if len(paths) == 1:
                # Найдён один вариант — предлагаем подтвердить
                path = paths[0]
                return {
                    "success": True,
                    "message": f"Я нашел {path}. Это нужная программа, сэр?",
                    "action": "file_search_confirm",
                    "paths": paths,
                    "found_name": name,
                }
            else:
                # Найдено несколько вариантов
                paths_list = "\n".join([f"{i+1}. {p}" for i, p in enumerate(paths)])
                return {
                    "success": True,
                    "message": f"Я нашел несколько вариантов для '{name}', сэр:\n{paths_list}\nКакой использовать?",
                    "action": "file_search_multiple",
                    "paths": paths,
                    "found_name": name,
                }
        else:
            return {
                "success": False,
                "message": result["message"],
                "action": "file_search",
            }
    except Exception as exc:
        return {
            "success": False,
            "message": f"Ошибка при поиске программы: {exc}",
            "action": "file_search",
        }


def _handle_update_index() -> dict[str, Any]:
    """Обрабатывает команду обновления индекса.
    
    Returns:
        dict с результатом операции
    """
    try:
        from core.file_scanner import rebuild_system_index
        
        result = rebuild_system_index()
        
        return {
            "success": True,
            "message": result["message"],
            "action": "file_search_indexing",
        }
    except Exception as exc:
        return {
            "success": False,
            "message": f"Ошибка при обновлении индекса: {exc}",
            "action": "file_search_indexing",
        }


# === Этап 12: Реактивный сканер (мониторинг подключений) ===

# Интервал проверки списка дисков (секунды)
_DRIVE_CHECK_INTERVAL = 7

# Глобальное состояние ожидающего подтверждения сканирования
_pending_drive_scan: str | None = None  # буква диска, ожидающая подтверждения
_drive_monitor_instance: "DriveMonitor | None" = None


class DriveMonitor:
    """Фоновый монитор «горячего» подключения съёмных носителей.

    Раз в несколько секунд сравнивает текущий список дисков (через
    psutil.disk_partitions) с тем, что был ранее. Если появляется новая
    буква диска — вызывает callback, который должен спросить пользователя,
    нужно ли сканировать этот носитель.

    Также отслеживает отключение дисков и очищает помеченные source="external"
    записи из index.json, чтобы Зевс не пытался запустить недоступный файл.
    """

    def __init__(self, on_new_drive: "callable[[str], None] | None" = None,
                 on_drive_removed: "callable[[str], None] | None" = None):
        """
        Args:
            on_new_drive: callback(буква_диска) — вызывается при обнаружении
                нового диска. Должен задать вопрос пользователю.
            on_drive_removed: callback(буква_диска) — вызывается при
                отключении диска (для очистки индекса).
        """
        self.on_new_drive = on_new_drive
        self.on_drive_removed = on_drive_removed
        self._known_drives: set[str] = set()
        self._running = False
        self._thread: "threading.Thread | None" = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Запускает фоновый поток мониторинга."""
        with self._lock:
            if self._running:
                return
            self._running = True

        # Инициализируем известный список текущими дисками
        try:
            from core.file_scanner import list_available_drives
            self._known_drives = set(list_available_drives())
        except Exception:
            self._known_drives = set()

        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Останавливает мониторинг."""
        with self._lock:
            self._running = False

    def _monitor_loop(self) -> None:
        """Основной цикл мониторинга."""
        while True:
            with self._lock:
                if not self._running:
                    break

            try:
                from core.file_scanner import list_available_drives, remove_external_for_drive

                current = set(list_available_drives())

                # --- Новые диски ---
                new_drives = current - self._known_drives
                for drive in new_drives:
                    # Не трогаем основные диски C:/D: — они уже проиндексированы
                    if drive.upper() in ("C:\\", "D:\\"):
                        continue
                    if self.on_new_drive is not None:
                        try:
                            self.on_new_drive(drive)
                        except Exception:
                            pass

                # --- Отключённые диски ---
                removed_drives = self._known_drives - current
                for drive in removed_drives:
                    try:
                        removed = remove_external_for_drive(drive)
                        if removed and self.on_drive_removed is not None:
                            self.on_drive_removed(drive)
                    except Exception:
                        pass

                self._known_drives = current
            except Exception:
                pass

            # Ждём до следующей проверки
            for _ in range(_DRIVE_CHECK_INTERVAL):
                with self._lock:
                    if not self._running:
                        break
                threading.Event().wait(1)


def start_drive_monitor(on_new_drive: "callable[[str], None] | None" = None,
                        on_drive_removed: "callable[[str], None] | None" = None) -> None:
    """Запускает глобальный монитор дисков.

    Args:
        on_new_drive: callback(буква_диска) при обнаружении нового носителя
        on_drive_removed: callback(буква_диска) при отключении носителя
    """
    global _drive_monitor_instance
    if _drive_monitor_instance is None:
        _drive_monitor_instance = DriveMonitor(on_new_drive, on_drive_removed)
    _drive_monitor_instance.start()


def stop_drive_monitor() -> None:
    """Останавливает глобальный монитор дисков."""
    global _drive_monitor_instance
    if _drive_monitor_instance is not None:
        _drive_monitor_instance.stop()


def get_pending_drive_scan() -> str | None:
    """Возвращает букву диска, ожидающего подтверждения сканирования."""
    return _pending_drive_scan


def set_pending_drive_scan(drive: str | None) -> None:
    """Устанавливает/сбрасывает ожидающий диск."""
    global _pending_drive_scan
    _pending_drive_scan = drive


def confirm_drive_scan() -> dict[str, Any]:
    """Подтверждает сканирование ранее обнаруженного нового диска.

    Запускает локальное сканирование ТОЛЬКО этого диска (не трогая C:/D:)
    и добавляет найденные программы в index.json с тегом source="external".

    Returns:
        dict с результатом операции
    """
    global _pending_drive_scan
    drive = _pending_drive_scan
    if not drive:
        return {
            "success": False,
            "message": "Нет ожидающего сканирования диска, сэр.",
            "action": "drive_scan_none",
        }

    _pending_drive_scan = None

    from core.file_scanner import scan_drive, merge_external_entries

    def _scan_thread():
        try:
            entries = scan_drive(drive, max_workers=4)
            added = merge_external_entries(drive, entries)
        except Exception:
            added = 0
        # Сохраняем результат в глобальную переменную для последующего чтения
        confirm_drive_scan._last_result = {
            "success": True,
            "added": added,
            "drive": drive,
        }

    threading.Thread(target=_scan_thread, daemon=True).start()

    return {
        "success": True,
        "message": f"Сканирую {drive}, сэр. Добавлю найденные программы в индекс как внешние.",
        "action": "drive_scan_started",
        "drive": drive,
    }


def cancel_drive_scan() -> dict[str, Any]:
    """Отменяет ожидающее сканирование нового диска."""
    global _pending_drive_scan
    drive = _pending_drive_scan
    _pending_drive_scan = None
    return {
        "success": True,
        "message": "Пропускаю сканирование носителя, сэр.",
        "action": "drive_scan_cancelled",
        "drive": drive,
    }


def _handle_drive_scan_response(text: str) -> dict[str, Any] | None:
    """Обрабатывает голосовой/текстовый ответ на вопрос о сканировании флешки.

    Если есть ожидающий диск и пользователь говорит «да»/«сканируй» —
    запускает локальное сканирование. Если «нет» — отменяет.

    Returns:
        dict с результатом или None, если нет ожидающего диска.
    """
    if _pending_drive_scan is None:
        return None

    text_lower = text.lower()
    positive = bool(re.search(
        r"(?<!\w)(?:да|yes|scan|скан|ищи)(?!\w)|просканируй|сканируй",
        text_lower,
    ))
    negative = bool(re.search(
        r"(?<!\w)(?:нет|no)(?!\w)|не\s+надо|не\s+нужно|отмен|пропусти",
        text_lower,
    ))

    if negative:
        return cancel_drive_scan()
    if positive:
        return confirm_drive_scan()

    # Непонятный ответ — оставляем ожидание
    return {
        "success": True,
        "message": "Не понял, сэр. Сказать «да» или «сканируй», чтобы проиндексировать носитель, или «нет» — чтобы пропустить.",
        "action": "drive_scan_clarify",
    }


def _handle_scan_flash_command(text: str) -> dict[str, Any] | None:
    """Обрабатывает явную команду сканирования флешки/нового диска.

    Поддерживает фразы:
        - «сканируй флешку» / «просканируй флешку»
        - «сканируй диск E» / «просканируй диск E:»
        - «найди программы на флешке»

    Returns:
        dict с результатом или None, если это не команда сканирования флешки.
    """
    text_lower = text.lower()
    is_flash_cmd = any(
        w in text_lower
        for w in ["флешк", "флеш", "flash", "съёмн", "внешн", "usb"]
    ) and any(w in text_lower for w in ["сканируй", "скан", "просканируй", "найди", "индексируй", "проиндексируй"])

    if not is_flash_cmd:
        return None

    # Пытаемся извлечь конкретную букву диска (например «E»)
    import re as _re
    m = _re.search(r"диск\s+([a-z])\b", text_lower)
    target_drive = f"{m.group(1).upper()}:\\" if m else None

    try:
        from core.file_scanner import list_available_drives
        drives = list_available_drives()
    except Exception:
        drives = []

    # Исключаем основные диски C:/D:
    external_drives = [d for d in drives if d.upper() not in ("C:\\", "D:\\")]

    if target_drive and target_drive in drives:
        set_pending_drive_scan(target_drive)
        return confirm_drive_scan()

    if external_drives:
        # Берём первый внешний диск
        set_pending_drive_scan(external_drives[0])
        return confirm_drive_scan()

    return {
        "success": False,
        "message": "Съёмных носителей не обнаружено, сэр. Подключите флешку и попробуйте снова.",
        "action": "drive_scan_no_drive",
    }


