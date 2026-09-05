"""Модуль «Исполнитель команд» (System Controller) для Зевса.

Обрабатывает системные команды пользователя:
  * запуск приложений и игр;
  * закрытие приложений;
  * выбор случайной игры.

Архитектура:
  1. Сначала проверяем, содержит ли текст пользователя «командные» ключевые слова
     (открой, запусти, закрой, выключи).
  2. Если ключевые слова найдены — парсим команду и выполняем действие.
  3. Если ключевые слова отсутствуют — возвращаем None, и текст идёт в LLM.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import threading
import urllib.parse
from typing import Any

from core import config
import datetime
import psutil


# ============================================================
# Голосовые команды: время/дата, калькулятор, блокировка ПК
# ============================================================

def _handle_time_date_command(text: str) -> dict[str, Any] | None:
    """«Который час», «сколько времени», «какое число», «какой день» и т.п."""
    low = (text or "").lower()
    is_time = bool(re.search(
        r"(который(\s+сейчас)?\s*час|сколько\s+времени|какое(\s+сейчас)?\s*время|"
        r"сколько\s+(сейчас\s+)?времени|который\s+час\s+сейчас)", low))
    is_date = bool(re.search(
        r"(какое(\s+сегодня)?\s*число|какая(\s+сегодня)?\s*дата|какой(\s+сегодня)?\s*день|"
        r"дата|число|сегодня|какой\s+день\s+недели)", low))
    if not (is_time or is_date):
        return None
    now = datetime.datetime.now()
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
    if is_date:
        msg = (f"Сегодня {now.day} {months[now.month - 1]} {now.year} года, "
               f"{days[now.weekday()]}, сэр.")
    else:
        msg = f"Сейчас {now.hour:02d}:{now.minute:02d}, сэр."
    return {"success": True, "message": msg, "action": "time_date"}


_CALC_WORDS = (
    (" в степени ", " ** "), (" в кубе", " ** 3"), (" в квадрате", " ** 2"),
    ("умножить на", "*"), ("умножь на", "*"), ("умножить", "*"), ("умножь", "*"),
    ("разделить на", "/"), ("раздели на", "/"), ("разделить", "/"), ("раздели", "/"),
    ("плюс", "+"), ("минус", "-"), ("запятая", "."), (",", "."), ("×", "*"), ("÷", "/"),
)

# Словарь чисел прописью (0–100 + кратные 10/100) для перевода в цифры.
_NUM_WORDS = {
    "ноль": "0", "один": "1", "два": "2", "две": "2", "три": "3", "четыре": "4",
    "пять": "5", "шесть": "6", "семь": "7", "восемь": "8", "восемь": "8", "девять": "9",
    "десять": "10", "одиннадцать": "11", "двенадцать": "12", "тринадцать": "13",
    "четырнадцать": "14", "пятнадцать": "15", "шестнадцать": "16", "семнадцать": "17",
    "восемнадцать": "18", "девятнадцать": "19", "двадцать": "20", "тридцать": "30",
    "сорок": "40", "пятьдесят": "50", "шестьдесят": "60", "семьдесят": "70",
    "восемьдесят": "80", "девяносто": "90", "сто": "100", "тысяча": "1000",
}


def _safe_calc(expr: str) -> float | None:
    """Безопасно вычисляет арифметическое выражение (цифры и + - * / ** ( ))."""
    expr = re.sub(r"\*\*+", "**", expr)
    if not re.fullmatch(r"[\d+\-*/(). ]+", expr) or not re.search(r"\d", expr):
        return None
    try:
        return float(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 — вход фильтрован
    except Exception:  # noqa: BLE001
        return None


def _handle_calc_command(text: str) -> dict[str, Any] | None:
    """«Посчитай 12 умножить на 8», «сколько будет 2 плюс 2» и т.п."""
    low = (text or "").lower()
    if not any(w in low for w in ("посчитай", "сколько будет", "вычисли", "калькулятор")):
        return None
    expr = low
    for phrase, sym in _CALC_WORDS:
        expr = expr.replace(phrase, sym)
    for word, digit in _NUM_WORDS.items():
        expr = re.sub(r"\b" + re.escape(word) + r"\b", " " + digit + " ", expr)
    m = re.search(r"[\d (][\d+\-*/(). ]*[\d)]", expr)
    if not m:
        return {
            "success": False,
            "message": "Не нашёл выражение для расчёта, сэр.",
            "action": "calc",
        }
    result = _safe_calc(m.group(0))
    if result is None:
        return {
            "success": False,
            "message": "Не смог вычислить это выражение, сэр.",
            "action": "calc",
        }
    return {"success": True, "message": f"Получится {result:.6g}, сэр.", "action": "calc"}


def _handle_lock_pc_command(text: str) -> dict[str, Any] | None:
    """«Заблокируй компьютер/экран» — Win+L (безопасно, ничего не выключает)."""
    low = (text or "").lower()
    if not any(w in low for w in ("заблокируй", "заблокировать")) or not any(
        w in low for w in ("компьютер", "экран", "пк", "windows", "виндовс", "систему")
    ):
        return None
    try:
        if os.name == "nt":
            subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"], shell=False)
            return {"success": True, "message": "Блокирую компьютер, сэр.", "action": "lock_pc"}
        return {
            "success": False,
            "message": "Блокировка экрана доступна только в Windows, сэр.",
            "action": "lock_pc",
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "message": f"Не удалось заблокировать: {exc}", "action": "lock_pc"}


def _handle_minimize_all_command(text: str) -> dict[str, Any] | None:
    """«Сверни все окна» — показать рабочий стол."""
    low = (text or "").lower()
    if not ("сверни" in low and "окн" in low):
        return None
    try:
        if os.name == "nt":
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 "(New-Object -ComObject Shell.Application).MinimizeAll()"],
                shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return {"success": True, "message": "Сворачиваю все окна, сэр.", "action": "minimize_all"}
        return {"success": False, "message": "Доступно только в Windows, сэр.", "action": "minimize_all"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "message": f"Не удалось свернуть окна: {exc}", "action": "minimize_all"}


# ============================================================
# Заметки: «сделай заметку …» / «прочитай заметки»
# ============================================================

def _notes_path() -> str:
    return config.data_path("notes.txt")


def _handle_notes_command(text: str) -> dict[str, Any] | None:
    """Личный блокнот Зевса: добавление и чтение последних заметок."""
    low = (text or "").lower()
    is_read = any(w in low for w in ("прочитай заметки", "покажи заметки", "читай заметки",
                                     "какие заметки", "мои заметки"))
    is_add = "заметк" in low and any(
        w in low for w in ("сделай", "запиши", "новая", "добавь", "создай")
    )
    if not is_read and not is_add:
        return None

    path = _notes_path()
    try:
        if is_read:
            if not os.path.exists(path):
                return {"success": True, "message": "Заметок пока нет, сэр.", "action": "notes"}
            with open(path, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
            if not lines:
                return {"success": True, "message": "Заметок пока нет, сэр.", "action": "notes"}
            last = lines[-5:]
            listing = "; ".join(l.split("] ", 1)[-1] for l in last)
            return {
                "success": True,
                "message": f"Последние заметки, сэр: {listing}.",
                "action": "notes",
                "notes": last,
            }
        m = re.search(r"заметк[уаи]?\s*(?:о том,?\s*)?(.+)", text, re.IGNORECASE)
        note_text = m.group(1).strip(" .!?") if m else ""
        if not note_text:
            return {
                "success": False,
                "message": "Скажите текст заметки после слова «заметка», сэр.",
                "action": "notes",
            }
        stamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M]")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {note_text}\n")
        return {"success": True, "message": "Записал в заметки, сэр.", "action": "notes"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "message": f"Ошибка заметок: {exc}", "action": "notes"}




# === Загрузка конфигурации команд ===

def _commands_path() -> str:
    """Возвращает путь к commands.json рядом с исполняемым файлом / main.py."""
    return config.data_path("commands.json")


def _commands_map_path() -> str:
    """Возвращает путь к модульному словарю команд и синонимов."""
    return config.data_path("commands_map.json")


_COMMAND_ALIASES: dict[str, str] | None = None


def _load_command_aliases() -> dict[str, str]:
    """Загружает синонимы команд из commands_map.json с безопасным fallback."""
    global _COMMAND_ALIASES
    if _COMMAND_ALIASES is not None:
        return _COMMAND_ALIASES
    aliases: dict[str, str] = {}
    try:
        with open(_commands_map_path(), "r", encoding="utf-8") as file:
            data = json.load(file)
        for canonical, entry in data.items():
            for alias in entry.get("aliases", []) if isinstance(entry, dict) else []:
                aliases[str(alias).lower()] = str(canonical).lower()
    except (OSError, ValueError, AttributeError):
        pass
    _COMMAND_ALIASES = aliases
    return aliases


def _normalize_command_aliases(text: str) -> str:
    """Заменяет синонимы на канонические глаголы перед разбором команды."""
    normalized = text
    aliases = _load_command_aliases()
    for alias in sorted(aliases, key=len, reverse=True):
        normalized = re.sub(
            rf"(?<!\w){re.escape(alias)}(?!\w)",
            aliases[alias],
            normalized,
            flags=re.IGNORECASE,
        )
    return normalized


# Кеш commands.json: файл перечитывается ТОЛЬКО при изменении mtime,
# а не при каждом вызове load_commands() («при каждом чихе»).
_CMDS_CACHE: dict[str, Any] | None = None
_CMDS_CACHE_MTIME: float = -1.0


def load_commands() -> dict[str, Any]:
    """Загружает commands.json с кешированием по mtime.

    Повторные вызовы возвращают закешированный словарь без дискового I/O;
    любое изменение файла (в т.ч. внешним редактором) автоматически
    подхватывается по изменению времени модификации.
    """
    global _CMDS_CACHE, _CMDS_CACHE_MTIME
    path = _commands_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = -1.0
    if _CMDS_CACHE is not None and mtime == _CMDS_CACHE_MTIME:
        return _CMDS_CACHE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        # Файл временно нечитаем (запись/битый JSON) — отдаём прошлый кеш.
        return _CMDS_CACHE if _CMDS_CACHE is not None else {"приложения": {}, "игры": {}}
    first = _CMDS_CACHE is None
    _CMDS_CACHE = data
    _CMDS_CACHE_MTIME = mtime
    if first:
        apps = data.get("приложения", {})
        games = data.get("игры", {})
        print(f"[Actions] commands.json загружен в кеш: {len(apps)} приложений, {len(games)} игр")
    return data


def reload_commands() -> dict[str, Any]:
    """Явная перезагрузка commands.json (вызывается watchdog-наблюдателем).

    Команды и так читаются с диска при каждом вызове load_commands(), поэтому
    изменения, сделанные в файле (в т.ч. внешним редактором), уже подхватываются.
    reload_commands() — документированная точка перезагрузки/валидации, которая
    вызывается файловым наблюдателем при сохранении commands.json.
    """
    global _CMDS_CACHE_MTIME, _COMMAND_ALIASES
    _CMDS_CACHE_MTIME = -1.0  # форс-перечитывание при следующем вызове
    _COMMAND_ALIASES = None
    try:
        data = load_commands()
        apps = data.get("приложения", {})
        games = data.get("игры", {})
        print(f"[Actions] commands.json перезагружен: {len(apps)} приложений, {len(games)} игр")
        return data
    except Exception as e:
        print(f"[Actions] Ошибка перезагрузки commands.json: {e}")
        return {"приложения": {}, "игры": {}}


# === Парсинг команд ===

# Ключевые слова, которые указывают на системную команду
_COMMAND_KEYWORDS = re.compile(
    r"\b(открой|открыть|запусти|запустить|закрой|закрыть|выключи|запомни|найди|найти|обнови|удали|close|open|launch|start|stop|find|update|delete)\b",
    re.IGNORECASE,
)

# Паттерны для извлечения имени программы/игры
_APP_NAME_PATTERN = re.compile(
    r"(?:открой|открыть|запусти|запустить|закрой|закрыть|выключи|close|open|launch|start|stop)\s+(.+)",
    re.IGNORECASE,
)

# Паттерн для команды поиска программы
_FIND_APP_PATTERN = re.compile(
    r"(?:найди|find)\s+(.+)",
    re.IGNORECASE,
)

# Паттерн для команды обновления базы
_UPDATE_INDEX_PATTERN = re.compile(
    r"(?:обнови\s+базу|обнови\s+индекс|update\s+index|update\s+database)",
    re.IGNORECASE,
)

# Паттерн для команды запоминания программы
_REMEMBER_APP_PATTERN = re.compile(
    r"запомни\s+эту\s+программу\s+как\s+(.+)",
    re.IGNORECASE,
)

# Паттерн для команды удаления
_DELETE_COMMAND_PATTERN = re.compile(
    r"удали\s+команду\s+(.+)",
    re.IGNORECASE,
)

# ------------------------------------------------------------------
# Системные макросы: громкость и яркость
# ------------------------------------------------------------------
# Виртуальные клавиши громкости (Windows)
_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP = 0xAF
_VOLUME_MAX_STEPS = 50  # при установке абсолютного значения — шаги медиа-кнопки

# Паттерн абсолютного значения в команде ("громкость на 50", "яркость 70")
_ABSOLUTE_VALUE_PATTERN = re.compile(r"(?:на|до|равно|установи|поставь)?\s*(\d{1,3})", re.IGNORECASE)


def _press_media_key(vk_code: int, times: int = 1):
    """Нажимает медиа-клавишу (громкость) заданное число раз через WinAPI."""
    try:
        import ctypes
        from ctypes import wintypes  # noqa: F401
        for _ in range(max(1, times)):
            ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)       # KEYEVENTF_KEYDOWN
            ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)       # KEYEVENTF_KEYUP
    except Exception as e:
        print(f"[Actions] Ошибка нажатия медиа-клавиши: {e}")


def set_volume_relative(direction: str) -> dict[str, Any]:
    """Увеличивает/уменьшает/выключает громкость ОС.

    direction: "up" | "down" | "mute"
    """
    if direction == "mute":
        _press_media_key(_VK_VOLUME_MUTE)
        return {"success": True, "message": "Звук выключен, сэр.", "action": "volume_mute"}
    if direction == "up":
        _press_media_key(_VK_VOLUME_UP, 4)
        return {"success": True, "message": "Громкость увеличена, сэр.", "action": "volume_up"}
    if direction == "down":
        _press_media_key(_VK_VOLUME_DOWN, 4)
        return {"success": True, "message": "Громкость уменьшена, сэр.", "action": "volume_down"}
    return {"success": False, "message": "Не понял команду громкости.", "action": "volume"}


def set_volume_absolute(percent: int) -> dict[str, Any]:
    """Устанавливает громкость приблизительно на percent процентов.

    Лучший результат — через pycaw (Core Audio). Если пакет недоступен,
    используется приближение медиа-клавишами.
    """
    percent = min(100, max(0, int(percent)))
    target = round(percent / 100.0 * _VOLUME_MAX_STEPS)

    # Точное задание через pycaw (опционально)
    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL, CoInitialize  # type: ignore
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore
        CoInitialize()
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(percent / 100.0, None)
        return {"success": True, "message": f"Громкость установлена на {percent}%, сэр.", "action": "volume_set"}
    except Exception:
        pass  # фолбэк ниже

    # Приближение через медиа-клавиши: сброс в «тихо», затем подъём.
    for _ in range(_VOLUME_MAX_STEPS):
        _press_media_key(_VK_VOLUME_DOWN)
    for _ in range(target):
        _press_media_key(_VK_VOLUME_UP)
    return {"success": True, "message": f"Громкость установлена на {percent}%, сэр.", "action": "volume_set"}


def set_brightness(percent: int) -> dict[str, Any]:
    """Устанавливает яркость экрана (Windows, WMI) на percent процентов.

    Требует устройства с поддержкой WmiBrightnessMethods (ноутбук).
    """
    percent = min(100, max(0, int(percent)))
    ps = (
        "$s = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods; "
        f"if ($s) {{ $s | ForEach-Object {{ $_.WmiSetBrightness(1, {percent}) }} }} "
        "else { Write-Output 'NO_DEVICE' }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if "NO_DEVICE" in result.stdout:
            return {
                "success": False,
                "message": "Устройство не поддерживает управление яркостью, сэр.",
                "action": "brightness_set",
            }
        return {
            "success": True,
            "message": f"Яркость установлена на {percent}%, сэр.",
            "action": "brightness_set",
        }
    except Exception as exc:
        return {
            "success": False,
            "message": f"Не удалось изменить яркость: {exc}",
            "action": "brightness_set",
        }


def _handle_volume_command(text: str) -> dict[str, Any] | None:
    """Обрабатывает голосовые команды управления громкостью."""
    lower = text.lower()
    if not any(w in lower for w in [
        "громкость", "громче", "тише", "громкости", "потише", "погромче",
        "звук", "выключи звук", "mute", "volume",
    ]):
        return None

    if any(w in lower for w in ["выключи звук", "заглуши", "mute", "без звука", "отключи звук"]):
        return set_volume_relative("mute")

    m = _ABSOLUTE_VALUE_PATTERN.search(lower)
    if m:
        value = int(m.group(1))
        if any(w in lower for w in ["на", "до", "равно", "установи", "поставь"]):
            return set_volume_absolute(value)

    if any(w in lower for w in ["громче", "погромче", "прибавь громкость", "увеличь громкость", "выше"]):
        return set_volume_relative("up")
    if any(w in lower for w in ["тише", "потише", "убавь громкость", "уменьш громкость", "ниже"]):
        return set_volume_relative("down")

    return set_volume_relative("up")


def _handle_brightness_command(text: str) -> dict[str, Any] | None:
    """Обрабатывает голосовые команды управления яркостью экрана."""
    lower = text.lower()
    if not any(w in lower for w in ["яркость", "ярче", "темнее", "яркост", "светлее", "brightness"]):
        return None

    m = _ABSOLUTE_VALUE_PATTERN.search(lower)
    if m:
        value = int(m.group(1))
        if any(w in lower for w in ["на", "до", "установи", "поставь", "яркость "]):
            return set_brightness(value)

    if any(w in lower for w in ["ярче", "светлее", "прибавь яркость", "увеличь яркость", "выше"]):
        return set_brightness(70)
    if any(w in lower for w in ["темнее", "убавь яркость", "уменьш яркость", "ниже"]):
        return set_brightness(30)

    return set_brightness(50)


def is_system_command(text: str) -> bool:
    """Проверяет, содержит ли текст командные ключевые слова."""
    return bool(_COMMAND_KEYWORDS.search(text))


def _extract_target(text: str) -> str | None:
    """Извлекает имя программы/игры из текста команды."""
    m = _APP_NAME_PATTERN.search(text)
    if m:
        target = m.group(1).strip().lower()
        # Убираем лишние слова
        target = re.sub(r"\s+(пожалуйста|плиз|please|pls)\b", "", target, flags=re.IGNORECASE).strip()
        # Убираем знаки препинания в конце
        target = re.sub(r"[,;.]+$", "", target).strip()
        return target
    return None


def _extract_find_target(text: str) -> str | None:
    """Извлекает имя программы из команды 'найди [Имя]'."""
    m = _FIND_APP_PATTERN.search(text)
    if m:
        target = m.group(1).strip().lower()
        # Убираем лишние слова
        target = re.sub(r"\s+(пожалуйста|плиз|please|pls)\b", "", target, flags=re.IGNORECASE).strip()
        # Убираем знаки препинания в конце
        target = re.sub(r"[,;.]+$", "", target).strip()
        return target
    return None


# === Безопасность: системные процессы ===

# Процессы, которые Зевс НИКОГДА не должен закрывать
_SYSTEM_PROCESSES = {
    "explorer.exe",
    "system",
    "system.exe",
    "ollama.exe",
    "ollama serve",
    "python.exe",
    "pythonw.exe",
    "cmd.exe",
    "powershell.exe",
    "svchost.exe",
    "winlogon.exe",
    "csrss.exe",
    "smss.exe",
    "wininit.exe",
    "services.exe",
    "lsass.exe",
    "spoolsv.exe",
}

# Словарь псевдонимов для закрытия приложений
_CLOSE_ALIASES = {
    "хром": "chrome.exe",
    "браузер": "chrome.exe",
    "гугл": "chrome.exe",
    "firefox": "firefox.exe",
    "лиса": "firefox.exe",
    "дискорд": "discord.exe",
    "discord": "discord.exe",
    "телеграм": "telegram.exe",
    "telegram": "telegram.exe",
    "телега": "telegram.exe",
    "vscode": "code.exe",
    "код": "code.exe",
    "pycharm": "pycharm64.exe",
    "пичарм": "pycharm64.exe",
    "steam": "steam.exe",
    "стим": "steam.exe",
    "epic": "EpicGamesLauncher.exe",
    "эпик": "EpicGamesLauncher.exe",
    "cs2": "cs2.exe",
    "cs": "cs2.exe",
    "контру": "cs2.exe",
    "контер": "cs2.exe",
    "dota2": "dota2.exe",
    "дота": "dota2.exe",
    "dota": "dota2.exe",
    "pubg": "PUBG.exe",
    "пабг": "PUBG.exe",
    "gta5": "GTA5.exe",
    "gta": "GTA5.exe",
    "gtav": "GTA5.exe",
    "cyberpunk": "Cyberpunk2077.exe",
    "киберпанк": "Cyberpunk2077.exe",
    "eldenring": "eldenring.exe",
    "elden": "eldenring.exe",
    "элден": "eldenring.exe",
    "baldursgate3": "BaldursGate3.exe",
    "bg3": "BaldursGate3.exe",
    "starfield": "Starfield.exe",
    "старфилд": "Starfield.exe",
    "apex": "ApexLegends.exe",
    "апекс": "ApexLegends.exe",
    "rust": "RustClient.exe",
    "раст": "RustClient.exe",
}


def _resolve_close_target(target: str) -> str | None:
    """Определяет имя процесса для закрытия.
    
    Приоритет:
    1. Псевдонимы из _CLOSE_ALIASES
    2. Если target уже оканчивается на .exe — используем как есть
    3. Иначе добавляем .exe
    """
    # Проверяем псевдонимы
    if target in _CLOSE_ALIASES:
        return _CLOSE_ALIASES[target]
    
    # Если уже указано расширение
    if target.endswith(".exe"):
        return target
    
    # Добавляем .exe
    return f"{target}.exe"


def _is_system_process(process_name: str) -> bool:
    """Проверяет, является ли процесс системным (запрещённым для закрытия)."""
    name_lower = process_name.lower()
    return name_lower in _SYSTEM_PROCESSES or name_lower in {p.lower() for p in _SYSTEM_PROCESSES}


def capture_active_app() -> dict[str, Any]:
    """Определяет путь к .exe файлу текущего активного окна Windows.
    
    Использует win32gui и psutil для получения информации о активном процессе.
    
    Returns:
        dict с результатом:
        {
            "success": bool,
            "path": str | None,  # путь к .exe файлу
            "process_name": str | None,  # имя процесса
            "message": str  # сообщение об ошибке или успехе
        }
    """
    try:
        # Пытаемся импортировать win32gui
        try:
            import win32gui
            import win32process
        except ImportError:
            return {
                "success": False,
                "path": None,
                "process_name": None,
                "message": "Для работы этой функции требуется pywin32. Установите его командой: pip install pywin32"
            }
        
        # Получаем handle активного окна
        hwnd = win32gui.GetForegroundWindow()
        
        if hwnd == 0:
            return {
                "success": False,
                "path": None,
                "process_name": None,
                "message": "Не удалось получить активное окно, сэр."
            }
        
        # Получаем PID процесса
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        
        # Получаем информацию о процессе
        try:
            process = psutil.Process(pid)
            exe_path = process.exe()
            process_name = process.name()
            
            # Проверяем, что это не системный процесс
            if _is_system_process(process_name):
                return {
                    "success": False,
                    "path": None,
                    "process_name": process_name,
                    "message": f"Процесс {process_name} является системным, сэр. Не могу его запомнить."
                }
            
            return {
                "success": True,
                "path": exe_path,
                "process_name": process_name,
                "message": f"Успешно определён процесс: {process_name}"
            }
            
        except psutil.NoSuchProcess:
            return {
                "success": False,
                "path": None,
                "process_name": None,
                "message": "Процесс больше не существует, сэр."
            }
            
    except Exception as exc:
        return {
            "success": False,
            "path": None,
            "process_name": None,
            "message": f"Ошибка при определении активного процесса: {exc}"
        }


def close_application(app_name: str) -> dict[str, Any]:
    """Принудительно завершает процесс по имени.
    
    Использует taskkill /F /IM для Windows.
    Безопасность: проверяет список исключений.
    
    Args:
        app_name: имя приложения/процесса (например, "chrome", "cs2")
    
    Returns:
        dict с результатом операции
    """
    target = _resolve_close_target(app_name)
    
    # Проверка безопасности
    if _is_system_process(target):
        return {
            "success": False,
            "message": f"Не могу закрыть {target} — это системный процесс, сэр.",
            "action": "close_app",
        }
    
    try:
        if os.name == "nt":
            # /F — принудительно, /T — дочерние процессы, /IM — по имени образа
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/IM", target],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return {
                    "success": True,
                    "message": f"Закрываю {app_name.title()}, сэр.",
                    "action": "close_app",
                }
            else:
                # Процесс не найден или уже закрыт
                return {
                    "success": False,
                    "message": f"Не удалось найти процесс {target}. Возможно, он уже закрыт.",
                    "action": "close_app",
                }
        else:
            # Linux/macOS
            result = subprocess.run(
                ["pkill", "-f", target],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return {
                    "success": True,
                    "message": f"Закрываю {app_name.title()}, сэр.",
                    "action": "close_app",
                }
            else:
                return {
                    "success": False,
                    "message": f"Не удалось найти процесс {target}.",
                    "action": "close_app",
                }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": f"Таймаут при закрытии {app_name}.",
            "action": "close_app",
        }
    except Exception as exc:
        return {
            "success": False,
            "message": f"Не удалось закрыть {app_name}: {exc}",
            "action": "close_app",
        }


# === Выполнение команд ===

def save_app_command(name: str, exe_path: str) -> dict[str, Any]:
    """Сохраняет новое приложение в commands.json.
    
    Args:
        name: имя приложения (ключ)
        exe_path: полный путь к .exe файлу
    
    Returns:
        dict с результатом операции
    """
    try:
        commands = load_commands()
        
        # Нормализуем имя (убираем лишние пробелы, приводим к нижнему регистру)
        name = name.strip().lower()
        
        # Проверяем, существует ли уже такое приложение
        if name in commands.get("приложения", {}):
            return {
                "success": False,
                "message": f"Приложение '{name}' уже существует в списке команд, сэр.",
                "action": "remember_app",
            }
        
        # Добавляем новое приложение
        if "приложения" not in commands:
            commands["приложения"] = {}
        
        commands["приложения"][name] = exe_path
        
        # Сохраняем в файл
        path = _commands_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(commands, f, indent=2, ensure_ascii=False)
        
        return {
            "success": True,
            "message": f"Запомнил {name} по пути {exe_path}, сэр.",
            "action": "remember_app",
        }
        
    except Exception as exc:
        return {
            "success": False,
            "message": f"Не удалось сохранить приложение: {exc}",
            "action": "remember_app",
        }


def delete_command(name: str) -> dict[str, Any]:
    """Удаляет команду из commands.json.
    
    Ищет команду в секциях "приложения" и "игры".
    
    Args:
        name: имя команды для удаления
    
    Returns:
        dict с результатом операции
    """
    try:
        commands = load_commands()
        
        # Нормализуем имя
        name = name.strip().lower()
        
        # Ищем в приложениях
        if name in commands.get("приложения", {}):
            del commands["приложения"][name]
            path = _commands_path()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(commands, f, indent=2, ensure_ascii=False)
            return {
                "success": True,
                "message": f"Удалил команду '{name}' из списка приложений, сэр.",
                "action": "delete_command",
            }
        
        # Ищем в играх
        if name in commands.get("игры", {}):
            del commands["игры"][name]
            path = _commands_path()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(commands, f, indent=2, ensure_ascii=False)
            return {
                "success": True,
                "message": f"Удалил команду '{name}' из списка игр, сэр.",
                "action": "delete_command",
            }
        
        # Ищем по частичному совпадению в приложениях
        for key in list(commands.get("приложения", {}).keys()):
            if name in key or key in name:
                del commands["приложения"][key]
                path = _commands_path()
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(commands, f, indent=2, ensure_ascii=False)
                return {
                    "success": True,
                    "message": f"Удалил команду '{key}' из списка приложений, сэр.",
                    "action": "delete_command",
                }
        
        # Ищем по частичному совпадению в играх
        for key in list(commands.get("игры", {}).keys()):
            if name in key or key in name:
                del commands["игры"][key]
                path = _commands_path()
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(commands, f, indent=2, ensure_ascii=False)
                return {
                    "success": True,
                    "message": f"Удалил команду '{key}' из списка игр, сэр.",
                    "action": "delete_command",
                }
        
        # Не нашли
        return {
            "success": False,
            "message": f"Команда '{name}' не найдена в списке, сэр.",
            "action": "delete_command",
        }
        
    except Exception as exc:
        return {
            "success": False,
            "message": f"Не удалось удалить команду: {exc}",
            "action": "delete_command",
        }


_STORE_LAUNCH_VERBS = ("запусти", "запустить", "играй", "играть", "давай сыграем")


def _extract_store_query(text: str) -> str | None:
    """Извлекает название игры из фразы про магазин Steam.

    Порядок важен: сначала вырезаются длинные словосочетания,
    затем одиночные ключевые слова.
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


def execute_command(text: str) -> dict[str, Any] | None:
    """Пытается выполнить системную команду.

    Возвращает словарь с результатом:
      {
        "success": bool,
        "message": str,   # голосовой ответ
        "action": str,    # "launch_app" | "launch_game" | "close_app" | "random_game" | "remember_app"
      }

    Возвращает None, если текст не содержит командных ключевых слов.
    """
    text = _normalize_command_aliases(text)
    text_lower = text.lower()

    # --- Этап 12: ответ на вопрос о сканировании нового носителя ---
    # Если Зевс ранее спросил «сканировать ли флешку?» и пользователь
    # отвечает «да»/«сканируй» или «нет» — обрабатываем это первым.
    drive_response = _handle_drive_scan_response(text)
    if drive_response is not None:
        return drive_response

    # --- Этап 12: явная команда сканирования флешки/внешнего диска ---
    flash_cmd = _handle_scan_flash_command(text)
    if flash_cmd is not None:
        return flash_cmd

    # --- Время/дата, калькулятор, блокировка ПК, окна, заметки ---
    for handler in (
        _handle_time_date_command,
        _handle_calc_command,
        _handle_lock_pc_command,
        _handle_minimize_all_command,
        _handle_notes_command,
    ):
        quick_result = handler(text)
        if quick_result is not None:
            return quick_result

    # --- Системные макросы: громкость и яркость (голосовое управление) ---
    volume_result = _handle_volume_command(text)
    if volume_result is not None:
        return volume_result
    brightness_result = _handle_brightness_command(text)
    if brightness_result is not None:
        return brightness_result

    # --- Taobao: веб-поиск (голосовое «найди … на таобао») ---
    taobao_result = _handle_taobao_command(text)
    if taobao_result is not None:
        return taobao_result

    # --- Гибридный Steam: веб-магазин (поиск/цены/корзина) ---
    steam_result = _handle_steam_store_command(text)
    if steam_result is not None:
        return steam_result

    # --- Автономный поиск программы ---
    if any(w in text_lower for w in ["найди", "find"]):
        find_target = _extract_find_target(text)
        if find_target:
            return _handle_find_command(find_target)
        else:
            return {
                "success": False,
                "message": "Не понял, что искать. Используйте: 'найди [Имя программы]'",
                "action": "file_search",
            }
    
    # --- Обновление индекса ---
    if _UPDATE_INDEX_PATTERN.search(text):
        return _handle_update_index()
    
    # --- Запоминание активного приложения ---
    if "запомни эту программу" in text_lower:
        # Извлекаем имя из команды "запомни эту программу как [Имя]"
        m = _REMEMBER_APP_PATTERN.search(text)
        if m:
            app_name = m.group(1).strip()
            
            # Получаем активный процесс
            capture_result = capture_active_app()
            
            if not capture_result["success"]:
                return {
                    "success": False,
                    "message": capture_result["message"],
                    "action": "remember_app",
                }
            
            # Сохраняем в commands.json
            save_result = save_app_command(app_name, capture_result["path"])
            return save_result
        else:
            return {
                "success": False,
                "message": "Не понял, как назвать программу. Используйте: 'запомни эту программу как [Имя]'",
                "action": "remember_app",
            }
    
    # --- Удаление команды ---
    if any(w in text_lower for w in ["удали", "delete"]):
        m = _DELETE_COMMAND_PATTERN.search(text)
        if m:
            command_name = m.group(1).strip()
            # Возвращаем запрос на подтверждение
            return {
                "success": True,
                "message": f"Вы уверены, что хотите удалить команду '{command_name}'?",
                "action": "delete_command_confirm",
                "command_name": command_name,
            }
        else:
            return {
                "success": False,
                "message": "Не понял, какую команду удалить. Используйте: 'удали команду [Имя]'",
                "action": "delete_command",
            }
    
    # Проверяем остальные системные команды
    if not is_system_command(text):
        # Одиночное название без глагола («каэс», «гта», «вотч догс»):
        # запускаем только при ТОЧНОМ совпадении с ключом/алиасом,
        # иначе обычная речь не должна превращаться в запуск.
        _k, _e = _exact_name_match(text, load_commands())
        if _e is not None:
            if _k == "app":
                return _launch_app(text, _e)
            return _launch_game(text, _e)
        return None

    commands = load_commands()
    target = _extract_target(text)
    if not target:
        # fallback: возможно глагол распознан неточно — пробуем сопоставить
        # весь текст («контр страйк два», «включи теккен 8» и т.п.).
        kind, entry = find_best_game_or_app(text, commands)
        if entry is not None:
            if kind == "app":
                return _launch_app(text, entry)
            return _launch_game(text, entry)
        return {
            "success": False,
            "message": "Не понял, что запустить. Уточните название.",
            "action": "unknown",
        }

    # --- Закрытие приложения ---
    if any(w in text_lower for w in ["закрой", "close", "stop", "выключи"]):
        return close_application(target)

    # --- Случайная игра ---
    if any(w in text_lower for w in ["случайную", "рандом", "random", "любую игру"]):
        return _launch_random_game(commands)

    # --- Запуск приложения ---
    # Сначала ищем в commands.json
    app_entry = _find_app(target, commands.get("приложения", {}))
    if app_entry:
        return _launch_app(target, app_entry)

    # --- Запуск игры ---
    game_entry = _find_game(target, commands.get("игры", {}))
    if game_entry:
        return _launch_game(target, game_entry)

    # --- Унифицированный поиск: «каэс/каес», грубые произношения, алиасы ---
    best_kind, best_entry = find_best_game_or_app(target or text, commands)
    if best_entry is not None:
        if best_kind == "app":
            return _launch_app(target or text, best_entry)
        return _launch_game(target or text, best_entry)
    
    # Если не нашли в commands.json - ищем в индексе (fallback)
    from core.file_scanner import get_indexed_program
    indexed_path = get_indexed_program(target)
    if indexed_path:
        # Нашли в индексе - предлагаем сохранить
        return {
            "success": True,
            "message": f"Нашёл {indexed_path}. Запустить, сэр?",
            "action": "launch_app",
            "path": indexed_path,
        }

    return {
        "success": False,
        "message": f"Не нашёл '{target}' в списке команд. Добавьте его в commands.json или скажите 'найди {target}'.",
        "action": "not_found",
    }


# Таблица транслитерации кириллица -> латиница (ГОСТ 7.79 / широко принятая).
# Приводит и русскоязычный ввод («контр страйк»), и латинские ключи
# («Counter-Strike») к единому алфавиту для сопоставления.
_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "iu", "я": "ia",
}


def _translit(text: str) -> str:
    """Транслитерирует кириллицу в латиницу (остальные символы без изменений)."""
    out = []
    for ch in (text or "").lower():
        out.append(_TRANSLIT_MAP.get(ch, ch))
    return "".join(out)


# Паразитные префиксы в начале голосовой фразы (эхо TTS + вежливость):
# «да сэр», «да да сэр», «сэр», «зевс», «ну», «давай», «пожалуйста»...
_FILLER_PREFIX_RE = re.compile(
    r"^(?:"
    r"(?:да|ну|давай|пожалуйста|уважаемый)+\s*"
    r"(?:(?:сэр|сер|сир|сергей|зевс|зеве)\b[,.!?]?\s*)?"
    r"|(?:сэр|сер|сир|сергей|зевс|зеве)\b[,.!?]?\s*"
    r")",
    re.IGNORECASE,
)


def _strip_leading_fillers(text: str) -> str:
    """Убирает паразитные слова в начале фразы: «да сэр запусти теккен 8»
    -> «запусти теккен 8». Применяется циклически, т.к. Vosk любит
    дубли («да да сэр»). Само слово команды не трогает."""
    t = (text or "").lower().strip(" ,.!?")
    prev = None
    while prev != t and t:
        prev = t
        t2 = _FILLER_PREFIX_RE.sub("", t).strip(" ,.!?")
        # защита от «съедания» всего текста
        t = t2 if t2 else t
    return t


def _norm_token(s: str) -> str:
    """Нормализует строку для нечёткого сравнения.

    1) нижний регистр; 2) числительные «два/три/…» -> цифры;
    3) транслит кириллица -> латиница (единый алфавит); 4) удаление
    пунктуации, пробелов и дефисов.
    """
    import re
    # Числительные распознаём ДО транслита, чтобы заменить чтение словами
    s_cyr = (s or "").lower()
    num_map_cyr = {
        "ноль": "0", "один": "1", "одна": "1", "два": "2", "две": "2",
        "три": "3", "четыре": "4", "пять": "5", "шесть": "6",
        "семь": "7", "восемь": "8", "девять": "9",
        "первый": "1", "первая": "1", "второй": "2", "вторая": "2",
        "третий": "3", "третья": "3", "четвертый": "4", "четвёртый": "4",
        "пятый": "5", "шестой": "6", "седьмой": "7",
        "восьмой": "8", "девятый": "9",
    }
    for w, d in num_map_cyr.items():
        s_cyr = re.sub(r"\b" + w + r"\b", d, s_cyr)
    # Теперь транслит и очистка
    s = _translit(s_cyr)
    return re.sub(r"[^a-z0-9]+", "", s)


def _norm_ratio(a: str, b: str) -> float:
    """Сходство двух строк (0..1) через difflib."""
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def _fuzzy_key(target: str, keys, cutoff: float = 0.72) -> str | None:
    """Нечётко находит ключ из keys, наиболее близкий к target.

    Нормализует оба аргумента (в т.ч. «два» -> 2) и сравнивает: частичное
    вхождение, затем ratio (difflib), затем get_close_matches.
    Returns:
        исходный ключ из keys или None, если нет достаточно близкого.
    """
    from difflib import get_close_matches

    nt = _norm_token(target)
    if not nt:
        return None

    norm_index = {_norm_token(k): k for k in keys if k}

    # 1) частичное вхождение нормализованных форм — только для существенно
    # длинных токенов, иначе «cs»/«tr» false-matchятся на подстроки вроде
    # ssh-pkcs11-helper / tr.exe
    for nk, orig in norm_index.items():
        if len(nt) >= 4 and len(nk) >= 4 and (nt in nk or nk in nt):
            return orig

    # 2) пороговое сходство по ratio
    best, best_ratio = None, cutoff
    for nk, orig in norm_index.items():
        if not nk:
            continue
        r = _norm_ratio(nt, nk)
        if r >= best_ratio:
            best_ratio, best = r, orig

    if best is not None:
        return best

    # 3) get_close_matches по нормализованным ключам
    norms = [k for k in norm_index if k]
    if norms:
        nm = get_close_matches(nt, norms, n=1, cutoff=cutoff)
        if nm:
            return norm_index[nm[0]]
    return None


def _find_app(target: str, apps: dict[str, str]) -> str | None:
    """Ищет путь к приложению по ключу, частичному или нечёткому совпадению.

    Нечёткий шаг — только для запросов >= 5 символов (cutoff 0.75):
    короткий/мусорный голос не должен угадывать системные утилиты
    (sed, gencat, scalar...), попавшие в базу из автосканера.
    """
    target = _strip_leading_fillers(target)
    if not target:
        return None
    # Точное совпадение
    if target in apps:
        return apps[target]
    # Частичное совпадение — только для достаточно длинных запросов,
    # чтобы короткие токены вроде «cs» не ловились в подстроках exe-имен
    for key, path in apps.items():
        if len(target) >= 4 and (target in key or key in target):
            return path
    # Нечёткий поиск (в т.ч. «контра страйк два» -> «контра страйк 2»).
    # Только для достаточно длинных запросов: короткий/мусорный голос
    # не должен угадывать системные утилиты автосканера.
    if len(target) >= 5:
        key = _fuzzy_key(target, apps, cutoff=0.75)
        if key is not None:
            return apps[key]
    return None


def _find_game(target: str, games: dict[str, Any]) -> dict[str, Any] | None:
    """Ищет игру по ключу, частичному/нечёткому совпадению, а также по
    полю aliases (русскоязычные синонимы) из записи."""
    if not target:
        return None
    if target in games:
        return games[target]
    for key, data in games.items():
        name = data.get("name", "").lower()
        # Прямые совпадения по имени/ключу
        if target in key or target in name or key in target or name in target:
            return data
        # Совпадение по алиасам записи
        for alias in data.get("aliases", []) or []:
            if target == str(alias).lower() or str(alias).lower() in target \
                    or target in str(alias).lower():
                return data
    # Нечёткий поиск по ключам
    key = _fuzzy_key(target, games)
    if key is not None:
        return games[key]
    # Нечёткий поиск по полю name и по алиасам
    nt = _norm_token(target)
    if nt:
        for key, data in games.items():
            nm = (data.get("name") or "").strip()
            nk = _norm_token(nm) if nm else ""
            if nk and (nt in nk or nk in nt or _norm_ratio(nt, nk) >= 0.72):
                return data
            for alias in data.get("aliases", []) or []:
                n_alias = _norm_token(str(alias))
                if n_alias and (nt in n_alias or n_alias in nt or _norm_ratio(nt, n_alias) >= 0.72):
                    return data
    return None


def _exact_name_match(text: str, commands: dict[str, Any]) -> tuple[str, Any] | None:
    """Точное совпадение текста с названием/алиасом игры или приложения.

    Используется для запуска по «голому» названию без глагола («каэс», «гта»).
    Только точное равенство после нормализации — обычная разговорная речь
    («да сэр», «всё хорошо») под это не подпадает.
    """
    q = _norm_token(text)
    if not q:
        return None, None
    for key, entry in (commands.get("приложения", {}) or {}).items():
        if q == _norm_token(key):
            return "app", entry
    for key, entry in (commands.get("игры", {}) or {}).items():
        name = entry.get("name", key)
        if q == _norm_token(name):
            return "game", entry
        for a in entry.get("aliases", []) or []:
            if q == _norm_token(str(a)):
                return "game", entry
    return None, None


def find_best_game_or_app(user_query: str, commands: dict[str, Any]) -> tuple[str, Any] | None:
    """Унифицированный поиск игры/приложения по произвольному запросу.
    Возвращает (kind, entry), где kind ∈ {"game", "app"}, а entry — запись игры
    (dict) или путь к приложению (str). Логика:
      1. Отрезает глаголы и «каэс/каес».
      2. Точное совпадение ключей и алиасов (нормализация через _norm_token).
      3. Частичное вхождение по нормализованным (транслит снимает кириллица↔латиница).
      4. Нечёткое совпадение (difflib, порог 0.75) — только по играм:
         «вотч догс»→watch_dogs 2 и т.п. Системные утилиты исключены.
    """
    from difflib import get_close_matches

    _PREFIXES = ["запусти", "запустить", "открой", "открыть", "найди", "найти",
                 "включи", "разаблокируй", "играть в"]
    q = _strip_leading_fillers(user_query)
    for p in _PREFIXES:
        q = q.replace(p, " ").strip()
    q = re.sub(r"\s+", " ", q).strip()
    if not q:
        return None, None

    games = commands.get("игры", {}) if isinstance(commands, dict) else {}
    apps = commands.get("приложения", {}) if isinstance(commands, dict) else {}
    nq = _norm_token(q)

    # Собираем кандидатов: (норм-имя, norm-токен, kind, entry)
    candidates: list[tuple[str, str, str, Any]] = []
    for key, entry in apps.items():
        candidates.append((key, _norm_token(key), "app", entry))
    for key, entry in games.items():
        name = entry.get("name", key)
        candidates.append((name, _norm_token(name), "game", entry))
        for a in entry.get("aliases", []) or []:
            candidates.append((str(a), _norm_token(str(a)), "game", entry))

    # 1) Точное совпадение по нормализованным (снимает дефисы/пробелы/транслит)
    for name, nname, kind, entry in candidates:
        if nq and nname and nq == nname:
            return kind, entry
        if nname and nq == _norm_token(str(name)):
            return kind, entry

    # 2) Частичное вхождение — ищем «контра», «гта», «вотч догс» среди имени/алиасов.
    # Для приложений требуется имя >= 5 символов: иначе системные утилиты
    # автосканера (sed, psl, w64...) ловят мусорные совпадения.
    for name, nname, kind, entry in candidates:
        if not (nq and nname):
            continue
        if kind == "game" and len(nq) >= 3 and (nq in nname or nname in nq):
            return kind, entry
        if kind == "app" and len(nname) >= 5 and len(nq) >= 3 and (nq in nname or nname in nq):
            return kind, entry

    # 3) Нечёткое совпадение (difflib): порог 0.75 и ТОЛЬКО по играм.
    # Системные утилиты автосканера (sed, gencat, git-*) исключены: раньше
    # «запусти энд зал» -> gencat, «ведьмак три» -> sed. Незнакомую игру
    # честнее отдать в LLM, чем запустить случайную утилиту.
    game_names = [nname for _n, nname, k, _e in candidates if nname and k == "game"]
    nm = get_close_matches(nq, game_names, n=1, cutoff=0.75) if game_names else None
    if nm:
        for name, nname, kind, entry in candidates:
            if nname == nm[0]:
                print(f"[Actions] Нечёткое совпадение: '{q}' -> '{name}'")
                return kind, entry
    return None, None


def _launch_app(name: str, path: str) -> dict[str, Any]:
    """Запускает приложение по пути."""
    try:
        if not os.path.isfile(path):
            return {
                "success": False,
                "message": f"Файл не найден: {path}. Проверьте путь в commands.json.",
                "action": "launch_app",
            }
        if os.name == "nt":
            # DETACHED_PROCESS: консольная утилита (zen и т.п.) не получает
            # унаследованные std-потоки и не может заблокировать родителя,
            # выводя справку в общий канал. DEVNULL закрывает пайпы явно.
            DETACHED = 0x00000008  # DETACHED_PROCESS
            NEW_GROUP = 0x00000200  # CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(
                [path], shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=DETACHED | NEW_GROUP,
            )
        else:
            subprocess.Popen([path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        return {
            "success": True,
            "message": f"Открываю {name.title()}, сэр.",
            "action": "launch_app",
        }
    except Exception as exc:
        return {
            "success": False,
            "message": f"Не удалось запустить {name}: {exc}",
            "action": "launch_app",
        }

def launch_installed_game(path_or_uri: str) -> None:
    """Нативный запуск установленной игры (компонент №1 гибридного Steam).

    steam://-URI открывается через системный обработчик ОС (клиент Steam),
    обычные пути — через os.startfile. Выполняется в фоновом потоке
    (zeus-commands) и не блокирует UI.
    """
    if path_or_uri.startswith("steam://"):
        if os.name == "nt":
            # os.system() ждёт завершения cmd.exe — заменяем на отсоединённый
            # Popen: cmd скрыт (CREATE_NO_WINDOW), родитель не ждёт.
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                ["cmd", "/c", "start", "", path_or_uri], shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            subprocess.Popen(["xdg-open", path_or_uri],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        if os.name == "nt":
            os.startfile(path_or_uri)  # noqa: S606
        else:
            subprocess.Popen([path_or_uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _launch_game(name: str, game: dict[str, Any]) -> dict[str, Any]:
    """Запускает игру через Steam или напрямую."""
    game_type = game.get("type", "steam")
    display_name = game.get("name", name)

    if game_type == "steam":
        game_id = game.get("id")
        if not game_id:
            return {
                "success": False,
                "message": f"У игры {display_name} не указан Steam ID.",
                "action": "launch_game",
            }
        steam_url = f"steam://rungameid/{game_id}"
        try:
            if os.name == "nt":
                subprocess.Popen(["cmd", "/c", "start", "", steam_url], shell=False)
            else:
                subprocess.Popen(["xdg-open", steam_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {
                "success": True,
                "message": f"Запускаю {display_name}, сэр.",
                "action": "launch_game",
            }
        except Exception as exc:
            return {
                "success": False,
                "message": f"Не удалось запустить {display_name}: {exc}",
                "action": "launch_game",
            }
    else:
        # Прямой запуск (если в будущем появятся не-Steam игры)
        path = game.get("path")
        if not path:
            return {
                "success": False,
                "message": f"У игры {display_name} не указан путь.",
                "action": "launch_game",
            }
        return _launch_app(name, path)


def _launch_random_game(commands: dict[str, Any]) -> dict[str, Any]:
    """Выбирает случайную игру из списка и запускает её."""
    games = commands.get("игры", {})
    if not games:
        return {
            "success": False,
            "message": "Список игр пуст. Добавьте игры в commands.json.",
            "action": "random_game",
        }
    key = random.choice(list(games.keys()))
    game = games[key]
    result = _launch_game(key, game)
    # Сохраняем action="random_game" для отслеживания
    result["action"] = "random_game"
    return result


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
    positive = any(w in text_lower for w in ["да", "сканируй", "скан", "yes", "scan", "просканируй", "ищи", "ищи там"])
    negative = any(w in text_lower for w in ["нет", "не надо", "отмен", "no", "не нужно", "пропусти"])

    if positive:
        return confirm_drive_scan()
    if negative:
        return cancel_drive_scan()

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


