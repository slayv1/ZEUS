# -*- coding: utf-8 -*-
"""Медиа и быстрые команды: громкость, яркость, время, калькулятор, заметки, блокировка."""
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
        r"какой\s+день\s+недели|(?:скажи|назови|сообщи|уточни)\s+(?:мне\s+)?"
        r"(?:дату|число|день|сегодняш\w*\s+дат\w*))",
        low,
    ))
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


# --- Белый список операций калькулятора ---------------------------------
# Раньше здесь стоял eval(expr, {"__builtins__": {}}, {}) с regex-фильтром по
# символам. Такая защита хрупкая: стоит ослабить regex (например, разрешить
# букву «e» для экспоненты), как появляется путь к выполнению кода. Теперь
# выражение разбирается парсером Python, а вычисляется только то, что
# СТРУКТУРНО является арифметикой над числами: вызовы, имена, атрибуты, срезы,
# строковые литералы отсекаются на уровне AST.
_CALC_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_CALC_UN_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# Ограничения вычислений: «9 в степени 999999999» — это легальная арифметика из
# одних цифр, но вычисление съест CPU и память. Ограничиваем основание/показатель
# и итог по модулю — гигантские int до приведения к float не доживают.
_CALC_MAX_BASE = 1_000_000.0
_CALC_MAX_EXPONENT = 10_000.0
_CALC_MAX_RESULT = 1e30
_CALC_MAX_DEPTH = 32


def _as_float(value) -> float:
    """float() без OverflowError: гигантский int -> inf (дальше отклоняется)."""
    try:
        return float(value)
    except (OverflowError, TypeError, ValueError):
        return float("inf")


def _calc_node(node: ast.AST, depth: int = 0):
    """Рекурсивно вычисляет узел AST, разрешая только арифметику над числами."""
    if depth > _CALC_MAX_DEPTH:
        raise ValueError("выражение слишком вложенное")

    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("допустимы только числа")
        return value

    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_BIN_OPS:
        left = _calc_node(node.left, depth + 1)
        right = _calc_node(node.right, depth + 1)
        if isinstance(node.op, ast.Pow):
            if abs(_as_float(right)) > _CALC_MAX_EXPONENT:
                raise ValueError("показатель степени слишком большой")
            if abs(_as_float(left)) > _CALC_MAX_BASE:
                raise ValueError("основание степени слишком большое")
        result = _CALC_BIN_OPS[type(node.op)](left, right)
        if abs(_as_float(result)) > _CALC_MAX_RESULT:
            raise ValueError("результат слишком большой")
        return result

    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_UN_OPS:
        return _CALC_UN_OPS[type(node.op)](_calc_node(node.operand, depth + 1))

    raise ValueError("недопустимая конструкция в выражении")


def _safe_calc(expr: str) -> float | None:
    """Вычисляет арифметическое выражение без eval() (через ast + белый список).

    Поддерживаются цифры, скобки, + - * / // % ** и унарный минус; экспоненциальная
    запись («1e3») тоже проходит — парсер Python её разбирает, а eval больше нет.
    """
    expr = re.sub(r"\*\*+", "**", (expr or "").strip())
    if not expr or len(expr) > 100:
        return None
    if not re.search(r"\d", expr):
        return None
    try:
        tree = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    try:
        result = float(_calc_node(tree.body))
    except (ValueError, TypeError, ZeroDivisionError, ArithmeticError,
            OverflowError, RecursionError):
        return None
    # inf/nan ответом пользователю быть не должны: «1e999» парсится в inf
    # без исключения, а 1e999-1e999 даёт nan.
    return result if math.isfinite(result) else None


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

