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
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = m.group(1).replace("\\\\", "\\")
            if p and os.path.isdir(p):
                roots.append(p)
    except Exception:  # noqa: BLE001
        pass
    return roots


def _vdf_value(text: str, key: str) -> str | None:
    m = re.search(r'"' + re.escape(key) + r'"\s+"([^"]*)"', text)
    return m.group(1).strip() if m else None


# Куски StateFlags из appmanifest. Игра попадает в базу, если она уже
# установлена или прямо сейчас качается/обновляется.
_STATE_INSTALLED = 4
_STATE_DOWNLOAD = (
    2          # нужно обновление
    | 256      # обновление идёт
    | 512      # обновление на паузе
    | 1024     # обновление начато
    | 262144   # дописываются файлы
    | 524288   # выделяется место
    | 1048576  # идёт загрузка
    | 2097152  # распаковка
    | 4194304  # запись на диск
)


def app_track_state(
    flags: str | None,
    bytes_to: str | None = None,
    bytes_got: str | None = None,
) -> str | None:
    """``installed`` или ``downloading``. None — манифест не игра на диске.

    Неустановленная позиция библиотеки (флаг 1 без загрузки) не добавляется.
    Манифест без StateFlags оставляем: старые acf так и пишутся.
    """
    try:
        flag = int(flags or "0")
    except ValueError:
        flag = 0
    try:
        need = int(bytes_to or "0")
        have = int(bytes_got or "0")
    except ValueError:
        need, have = 0, 0
    downloading = bool(flag & _STATE_DOWNLOAD) or (need > 0 and have < need)
    if downloading:
        return "downloading"
    if flag == 0 or bool(flag & _STATE_INSTALLED):
        return "installed"
    return None


def _steam_games() -> list[tuple[str, str, str]] | None:
    """[(appid, имя, состояние)] для скачанных и качающихся игр Steam.

    None — Steam не найден. Пустой список в этом случае стирал бы всю базу,
    поэтому вызывающий код игры не трогает.
    """
    games: dict[str, tuple[str, str]] = {}
    steam = _steam_path()
    if not steam:
        return None
    for root in _steam_library_roots(steam):
        for acf in glob.glob(os.path.join(root, "steamapps", "*.acf")):
            try:
                with open(acf, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                appid = _vdf_value(text, "appid")
                name = _vdf_value(text, "name")
                state = app_track_state(
                    _vdf_value(text, "StateFlags"),
                    _vdf_value(text, "BytesToDownload"),
                    _vdf_value(text, "BytesDownloaded"),
                )
                if appid and name and state:
                    games.setdefault(appid, (name, state))
            except Exception:  # noqa: BLE001
                continue
    return [(appid, name, state) for appid, (name, state) in games.items()]


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
    """Атомарно пишет commands.json (общий helper tmp + fsync + os.replace)."""
    from core import config
    from core.atomic_json import write_json_atomic
    write_json_atomic(config.data_path("commands.json"), data, indent=2)


# === Русскоязычные алиасы для игр (поиск по произношению) ===

# Известные популярные игры: ручные русскоязычные синонимы для надёжного поиска
# по произношению. Ключ — нормализованное (нижний регистр) имя игры.
_POPULAR_ALIASES = {
    "counter-strike 2": ["контра", "контр страйк", "контр страйк 2", "контр страйк два", "кс", "кс2", "cs2", "cs"],
    "dota 2": ["дота", "дота 2", "дотка"],
    "cs:go": ["контра", "контр страйк", "кс", "кс го"],
    "grand theft auto v": ["гта", "гта 5", "gta 5"],
    "grand theft auto v legacy": ["гта", "гта 5"],
    "grand theft auto v enhanced": ["гта энхансед", "gta enhanced", "гта 5 энхансед"],
    "watch_dogs 2": ["watch dogs 2", "ватч догс"],
    "tekken 8": ["теккен 8"],
    "tekken 7": ["теккен 7"],
    "far cry 4": ["фар край 4", "фаркрай 4"],
    "marvel rivals": ["марвел ривалс", "марвел"],
    "the witcher 3": ["ведьмак", "ведьмак 3", "ведьмак три", "witcher", "witcher 3"],
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
    matched = [k for k in _POPULAR_ALIASES if k in low or low in k]
    if matched:
        aliases.extend(_POPULAR_ALIASES[max(matched, key=len)])
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


def _make_game_record(name: str, appid: str, state: str = "installed") -> dict[str, Any]:
    """Создаёт запись игры с полем aliases (русскоязычные синонимы)."""
    return {
        "name": name,
        "type": "steam",
        "id": appid,
        "state": state,
        "aliases": _game_aliases(name),
    }


def _drop_uninstalled_steam(games: dict[str, Any], seen_ids: set[str]) -> list[str]:
    """Убирает Steam-игры, которых больше нет на диске. Не-Steam не трогает."""
    removed: list[str] = []
    for key in list(games):
        entry = games.get(key)
        if not isinstance(entry, dict) or entry.get("type") != "steam":
            continue
        appid = str(entry.get("id") or "")
        if appid and appid not in seen_ids:
            removed.append(str(entry.get("name") or key))
            del games[key]
    return removed


def _game_key_by_id(games: dict[str, Any], appid: str) -> str | None:
    """Ключ записи с тем же Steam appid, если она уже есть."""
    for key, entry in games.items():
        if isinstance(entry, dict) and str(entry.get("id") or "") == str(appid):
            return key
    return None


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
            for folder in (root, os.path.join(root, "steamapps")):
                try:
                    total += os.path.getmtime(folder)
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
    # Хранить имеет смысл только факт «текущее состояние источников уже
    # отсканировано»: все прошлые ключи (прежние mtime) никогда не понадобятся.
    # Чистим словарь, иначе он бесконечно растёт за время работы приложения.
    _SCAN_MTIME_CACHE.clear()
    _SCAN_MTIME_CACHE[key] = True
    return res


def auto_scan_in_background(interval: int = 30) -> None:
    """Периодическое инкрементальное сканирование в daemon-потоке (не блокирует UI).

    Полный обход делается только если изменилась папка Steam или рабочий стол.
    Проверка mtime дешёвая, поэтому интервал короткий: новая загрузка
    попадает в базу в пределах половины минуты.
    """
    def _loop() -> None:
        while True:
            try:
                res = scan_if_needed()
                changed = (
                    res.get("added_apps")
                    or res.get("added_games")
                    or res.get("removed_games")
                    or res.get("updated")
                )
                if not res.get("skipped") and changed:
                    from core import actions
                    actions.reload_commands()
            except Exception as e:  # noqa: BLE001
                print(f"[Scanner] Ошибка фона: {e}")
            time.sleep(max(15, int(interval)))

    threading.Thread(target=_loop, daemon=True, name="zeus-auto-scanner").start()


# === Сканирование ===

def scan_and_update() -> dict[str, Any]:
    """Сканирует рабочий стол и Steam, дополняет commands.json.

    Returns:
        {"added_apps": [...], "added_games": [...], "updated": bool}
    """
    from core import actions
    from core.command_hygiene import is_noise_executable, is_noise_game, prune_commands

    data = actions.load_commands()
    apps = data.setdefault("приложения", {})
    games = data.setdefault("игры", {})

    added_apps: list[str] = []
    added_games: list[str] = []
    removed_games: list[str] = []
    # Уже попавший в базу мусор (pip, git, установщики) вычищается на каждом скане.
    removed = prune_commands(data)
    updated: bool = bool(removed)

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
            if not exe_path or is_noise_executable(exe_path):
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

    # 2. Steam → игры. Похожее имя не повод пропускать другой appid:
    # «GTA V Enhanced» не является копией «GTA V Legacy». Качающаяся
    # игра добавляется сразу, по манифесту, не дожидаясь конца загрузки.
    # Если Steam не найден, базу не чистим.
    live = _steam_games()
    if live is not None:
        seen_ids: set[str] = set()
        for appid, name, state in live:
            if is_noise_game(name):
                continue
            seen_ids.add(str(appid))
            existing_key = _game_key_by_id(games, appid)
            if existing_key is not None:
                entry = games[existing_key]
                if isinstance(entry, dict):
                    if entry.get("state") != state:
                        entry["state"] = state
                        updated = True
                    if not entry.get("aliases"):
                        entry["aliases"] = _game_aliases(name)
                        updated = True
                continue
            key = name.lower()
            if key in games:
                key = f"{key} [{appid}]"
            games[key] = _make_game_record(name, appid, state)
            existing_game_keys.append(key)
            existing_game_names.append(name)
            added_games.append(name)
        removed_games = _drop_uninstalled_steam(games, seen_ids)
        if removed_games:
            updated = True

    # Учитываем и добавленные записи, и дополнение алиасов у существующих
    if added_apps or added_games or removed_games or updated:
        _save_atomic(data)

    return {
        "added_apps": added_apps,
        "added_games": added_games,
        "removed_games": removed_games,
        "updated": updated,
    }

