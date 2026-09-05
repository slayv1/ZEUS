"""Автоматический сканер новых игр/приложений для «Зевса».

Фоном (daemon-поток) сканирует:
  - рабочий стол пользователя (User Desktop и Public Desktop);
  - Steam (через libraryfolders.vdf + steamapps/*.acf).

Найденные новые программы автоматически дописываются в data/commands.json
(секции «приложения»/«игры»). Встроенный watchdog тут же перечитывает файл,
и база команд «Зевса» обновляется на лету без перезапуска приложения.

Дедупликация — через нечёткое сравнение имён/путей (переиспользуем утилиты
actions._norm_token/_norm_ratio), чтобы не дублировать известные команды.
"""
from __future__ import annotations

import glob
import os
import re
import threading
import time
from typing import Any


def _desktop_dirs() -> list[str]:
    """Возвращает список существующих папок рабочего стола."""
    home = os.path.expanduser("~")
    userprofile = os.environ.get("USERPROFILE", home)
    candidates = [
        os.path.join(home, "Desktop"),
        os.path.join(userprofile, "Desktop"),
        os.path.join(home, "OneDrive", "Desktop"),
        os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop"),
    ]
    seen: set[str] = set()
    out: list[str] = []
    for p in candidates:
        if p and os.path.isdir(p) and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _resolve_lnk_target(lnk_path: str) -> str | None:
    """Возвращает целевой .exe-путь Windows-ярлыка (.lnk) через pywin32."""
    try:
        import win32com.client  # type: ignore
        shell = win32com.client.Dispatch("WScript.Shell")
        sc = shell.CreateShortcut(lnk_path)
        target = sc.TargetPath or ""
        if target.lower().endswith(".exe") and os.path.isfile(target):
            return target
        return None
    except Exception:  # noqa: BLE001
        return None


def _shortcut_candidates(dir_path: str) -> list[tuple[str, str]]:
    """Возвращает [(имя_приложения, путь_к_исполнимому)] из папки."""
    result: list[tuple[str, str]] = []
    try:
        for entry in os.scandir(dir_path):
            if entry.is_dir():
                continue
            low = entry.name.lower()
            if low.endswith(".exe"):
                result.append((entry.name[:-4], entry.path))
            elif low.endswith(".lnk"):
                target = _resolve_lnk_target(entry.path)
                if target:
                    base = os.path.basename(target)
                    name = os.path.splitext(base)[0]
                    result.append((name, target))
    except OSError:
        pass
    return result


def _steam_path() -> str | None:
    """Ищет каталог установки Steam (реестр + типовые пути)."""
    try:
        import winreg
        for hive, subkey in (
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\\Valve\\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\WOW6432Node\\Valve\\Steam"),
        ):
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    path, _ = winreg.QueryValueEx(k, "SteamPath")
                if path:
                    p = path.replace("/", "\\")
                    if os.path.isdir(p):
                        return p
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(p):
            return p
    return None


def _steam_library_roots(steam_path: str) -> list[str]:
    """Возвращает корневые папки библиотек Steam (включая основную)."""
    roots = [steam_path]
    vdf = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        for m in re.finditer(r'"path"\\s+"([^"]+)"', text):
            p = m.group(1).replace("\\\\", "\\")
            if p and os.path.isdir(p):
                roots.append(p)
    except Exception:  # noqa: BLE001
        pass
    return roots


def _vdf_value(text: str, key: str) -> str | None:
    m = re.search(r'"' + re.escape(key) + r'"\\s+"([^"]*)"', text)
    return m.group(1).strip() if m else None


def _steam_games() -> list[tuple[str, str]]:
    """Возвращает [(appid, имя_игры)] для всех установленных игр Steam."""
    games: dict[str, str] = {}
    steam = _steam_path()
    if not steam:
        return []
    for root in _steam_library_roots(steam):
        for acf in glob.glob(os.path.join(root, "steamapps", "*.acf")):
            try:
                with open(acf, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                appid = _vdf_value(text, "appid")
                name = _vdf_value(text, "name")
                if appid and name:
                    games.setdefault(appid, name)
            except Exception:  # noqa: BLE001
                continue
    return list(games.items())


# === Дубликаты: нечёткое сравнение ===

def _norm(name: str) -> str:
    from core.actions import _norm_token
    return _norm_token(name)


def _exists_fuzzy(name: str, existing_keys: list[str], existing_names: list[str]) -> bool:
    from core.actions import _norm_ratio

    nt = _norm(name)
    for k in existing_keys:
        nk = _norm(k)
        if nk and (nt == nk or nt in nk or nk in nt or _norm_ratio(nt, nk) >= 0.8):
            return True
    for nm in existing_names:
        if not nm:
            continue
        nk = _norm(nm)
        if nk and (nt == nk or nt in nk or nk in nt or _norm_ratio(nt, nm) >= 0.8):
            return True
    return False



def _save_atomic(data: dict[str, Any]) -> None:
    """Атомарно пишет commands.json (temp + replace)."""
    import json
    from core import config
    path = config.data_path("commands.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# === Русскоязычные алиасы для игр (поиск по произношению) ===

# Известные популярные игры: ручные русскоязычные синонимы для надёжного поиска
# по произношению. Ключ — нормализованное (нижний регистр) имя игры.
_POPULAR_ALIASES = {
    "counter-strike 2": ["контра", "контр страйк", "контр страйк 2", "контр страйк два", "кс", "кс2", "cs2", "cs"],
    "dota 2": ["дота", "дота 2", "дотка"],
    "cs:go": ["контра", "контр страйк", "кс", "кс го"],
    "grand theft auto v": ["гта", "гта 5", "gta 5"],
    "grand theft auto v legacy": ["гта", "гта 5"],
    "watch_dogs 2": ["watch dogs 2", "ватч догс"],
    "tekken 8": ["теккен 8"],
    "tekken 7": ["теккен 7"],
    "far cry 4": ["фар край 4", "фаркрай 4"],
    "marvel rivals": ["марвел ривалс", "марвел"],
}


def _latin_to_cyr(text: str) -> str:
    """Символьно транслитерирует латиницу в кириллицу (для алиасов)."""
    m = {
        "a": "а", "b": "б", "c": "с", "d": "д", "e": "е", "f": "ф",
        "g": "г", "h": "х", "i": "и", "j": "ж", "k": "к", "l": "л",
        "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р",
        "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс",
        "y": "й", "z": "з", "_": " ", "-": " ", ".": "",
    }
    out = []
    for ch in str(text):
        out.append(m.get(ch.lower(), ch))
    return "".join(out).strip().capitalize()


def _game_aliases(name: str) -> list[str]:
    """Формирует список русскоязычных алиасов для игры: ручная карта
    популярных игр + кириллизация названия. Алиасы хранятся в естественном виде
    (кириллица/латиница) — поиск нормализует их через транслитерацию.
    """
    aliases: list[str] = []
    low = name.lower()
    for k, v in _POPULAR_ALIASES.items():
        if k in low or low in k:
            aliases.extend(v)
    ru = _latin_to_cyr(name)
    if ru and ru.lower() != low:
        aliases.append(ru)
    seen: set[str] = set()
    result: list[str] = []
    for a in aliases:
        a = a.strip()
        if a and a.lower() not in seen:
            seen.add(a.lower())
            result.append(a)
    return result


def _make_game_record(name: str, appid: str) -> dict[str, Any]:
    """Создаёт запись игры с полем aliases (русскоязычные синонимы)."""
    return {
        "name": name,
        "type": "steam",
        "id": appid,
        "aliases": _game_aliases(name),
    }


# === Инкрементальное сканирование (по mtime) ===

_SCAN_MTIME_CACHE: dict[str, bool] = {}


def _scan_sources_mtime() -> float:
    """Складывает mtime десктопов и папок Steam-библиотек (дешёвая проверка)."""
    total = 0.0
    for p in _desktop_dirs():
        try:
            total += os.path.getmtime(p)
        except OSError:
            pass
    steam = _steam_path()
    if steam:
        for root in _steam_library_roots(steam):
            try:
                total += os.path.getmtime(root)
            except OSError:
                pass
    return total


def scan_if_needed(force: bool = False) -> dict[str, Any]:
    """Инкрементальный скан: полный обход ТОЛЬКО при изменении mtime источника.

    Иначе дешёво возвращает {'skipped': True} без дискового walk.
    """
    key = f"scan:{_scan_sources_mtime():.6f}"
    if not force and _SCAN_MTIME_CACHE.get(key):
        return {"skipped": True, "added_apps": [], "added_games": [], "updated": False}
    res = scan_and_update()
    _SCAN_MTIME_CACHE[key] = True
    return res


def auto_scan_in_background(interval: int = 300) -> None:
    """Периодическое инкрементальное сканирование в daemon-потоке (не блокирует UI)."""
    def _loop() -> None:
        while True:
            try:
                res = scan_if_needed()
                if not res.get("skipped") and (res.get("added_apps") or res.get("added_games")):
                    from core import actions
                    actions.reload_commands()
            except Exception as e:  # noqa: BLE001
                print(f"[Scanner] Ошибка фона: {e}")
            time.sleep(max(30, int(interval)))

    threading.Thread(target=_loop, daemon=True, name="zeus-auto-scanner").start()


# === Сканирование ===

def scan_and_update() -> dict[str, Any]:
    """Сканирует рабочий стол и Steam, дополняет commands.json.

    Returns:
        {"added_apps": [...], "added_games": [...], "updated": bool}
    """
    from core import actions

    data = actions.load_commands()
    apps = data.setdefault("приложения", {})
    games = data.setdefault("игры", {})

    added_apps: list[str] = []
    added_games: list[str] = []
    updated: bool = False

    existing_app_keys = list(apps.keys())
    existing_game_keys = list(games.keys())
    existing_game_names = [g.get("name", "") for g in games.values() if isinstance(g, dict)]

    # 1a. Дополняем русскоязычные алиасы у существующих игр (независимо от Steam)
    for key, gdata in games.items():
        if isinstance(gdata, dict) and not gdata.get("aliases"):
            nm = gdata.get("name") or key
            gdata["aliases"] = _game_aliases(nm)
            updated = True

    # 1. Рабочий стол → приложения
    for d in _desktop_dirs():
        for name, exe_path in _shortcut_candidates(d):
            if not exe_path:
                continue
            key = _norm(name) or name.lower()
            if not key:
                continue
            if key in apps:
                continue
            if _exists_fuzzy(key, existing_app_keys, []):
                continue
            apps[name.lower()] = exe_path
            existing_app_keys.append(name.lower())
            added_apps.append(name)

    # 2. Steam → игры
    for appid, name in _steam_games():
        key = name.lower()
        if key in games:
            if isinstance(games[key], dict) and not games[key].get("aliases"):
                games[key]["aliases"] = _game_aliases(name)
                updated = True
            continue
        if _exists_fuzzy(key, existing_game_keys, existing_game_names):
            continue
        games[key] = _make_game_record(name, appid)
        existing_game_keys.append(key)
        existing_game_names.append(name)
        added_games.append(name)

    # Учитываем и добавленные записи, и дополнение алиасов у существующих
    if added_apps or added_games or updated:
        _save_atomic(data)

    return {"added_apps": added_apps, "added_games": added_games, "updated": updated}

