# -*- coding: utf-8 -*-
"""Запуск и база команд: commands.json, нечёткий поиск, открытие и закрытие приложений и игр."""
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

# === Загрузка конфигурации команд ===

def _commands_path() -> str:
    """Возвращает путь к commands.json рядом с исполняемым файлом / main.py."""
    return config.data_path("commands.json")


def _save_commands(commands: dict[str, Any]) -> bool:
    """Сохраняет commands.json атомарно (аудит v1.4, §2.2 надёжности данных).

    Прежний ``open(path, "w")`` усекал файл до начала записи: процесс, убитый
    посреди ``json.dump``, оставлял пользователя без ВСЕХ ярлыков приложений и
    игр — молча, без ошибки в логе. Запись через tmp + os.replace даёт либо
    старую, либо новую версию файла, но никогда не обрезанную середину.
    """
    if not write_json_atomic(_commands_path(), commands, indent=2):
        return False
    # Кеш по mtime обязан подхватить новые данные сразу — watchdog может
    # не успеть среагировать на событие файловой системы.
    try:
        reload_commands()
    except Exception:  # noqa: BLE001 — файл сохранён, кеш обновим в следующий раз
        pass
    return True


def _command_save_failed(action: str) -> dict[str, Any]:
    """Ответ «не удалось записать commands.json» для всех операций с командами."""
    return {
        "success": False,
        "message": "Не удалось записать список команд на диск, сэр.",
        "action": action,
    }


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


def ensure_commands_file() -> None:
    """Создаёт commands.json из шаблона, если на этой машине его ещё нет.

    Шаблон пустой: приложения и игры дописывает сканер. Чужой снимок
    с абсолютными путями сюда не копируется.
    """
    path = _commands_path()
    if os.path.isfile(path):
        return
    template = os.path.join(os.path.dirname(path), "commands.template.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(template):
            with open(template, "r", encoding="utf-8") as src, open(path, "w", encoding="utf-8") as dst:
                dst.write(src.read())
        else:
            with open(path, "w", encoding="utf-8") as dst:
                json.dump({"приложения": {}, "игры": {}}, dst, ensure_ascii=False, indent=2)
        print("[Actions] commands.json создан из шаблона")
    except OSError as exc:
        print(f"[Actions] Не удалось создать commands.json: {exc}")


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
    if not isinstance(data, dict):
        return _CMDS_CACHE if _CMDS_CACHE is not None else {"приложения": {}, "игры": {}}
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
        
        # Сохраняем в файл (атомарно: tmp + os.replace, без усечения файла)
        if not _save_commands(commands):
            return _command_save_failed("remember_app")
        
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
            if not _save_commands(commands):
                return _command_save_failed("delete_command")
            return {
                "success": True,
                "message": f"Удалил команду '{name}' из списка приложений, сэр.",
                "action": "delete_command",
            }
        
        # Ищем в играх
        if name in commands.get("игры", {}):
            del commands["игры"][name]
            if not _save_commands(commands):
                return _command_save_failed("delete_command")
            return {
                "success": True,
                "message": f"Удалил команду '{name}' из списка игр, сэр.",
                "action": "delete_command",
            }
        
        # Ищем по частичному совпадению в приложениях
        for key in list(commands.get("приложения", {}).keys()):
            if name in key or key in name:
                del commands["приложения"][key]
                if not _save_commands(commands):
                    return _command_save_failed("delete_command")
                return {
                    "success": True,
                    "message": f"Удалил команду '{key}' из списка приложений, сэр.",
                    "action": "delete_command",
                }
        
        # Ищем по частичному совпадению в играх
        for key in list(commands.get("игры", {}).keys()):
            if name in key or key in name:
                del commands["игры"][key]
                if not _save_commands(commands):
                    return _command_save_failed("delete_command")
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
            if game.get("state") == "downloading":
                message = f"{display_name} ещё скачивается. Открываю загрузку, сэр."
            else:
                message = f"Запускаю {display_name}, сэр."
            return {
                "success": True,
                "message": message,
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

