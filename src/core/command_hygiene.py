# -*- coding: utf-8 -*-
"""Фильтр мусорных программ в базе команд и файловом индексе.

Автосканер и полный обход диска легко записывают в «приложения» не то, чем
пользуются голосом: pip/pytest из Scripts, git из кэша, установщики, редисты
и служебные exe Unity. Такие записи шумят в нечётком поиске («запусти git»)
и раздувают commands.json.

Правило узкое: выкидываем только служебные деревья и имена установщиков.
Настоящие программы (Chrome, игры, ярлыки рабочего стола) остаются.
"""
from __future__ import annotations

import os
from typing import Any

# Компонент пути (уже в нижнем регистре), внутри которого лежат не программы,
# а зависимости, кэши и служебные бинарники.
_NOISE_DIR_PARTS = {
    "site-packages",
    "mingw64",
    "mingw32",
    "git-core",
    "node_modules",
    "__pycache__",
    ".cache",
    "codex-runtimes",
    "winsxs",
    "syswow64",
    "system32",
    "sysnative",
    "windowsapps",
    "packagecache",
    "commonredist",
    "espeak-ng-data",
    # Кэши редакторов и утилит: это не то, что запускают голосом.
    ".codex",
    ".nuget",
    ".lmstudio",
    ".kimi-code",
    ".unity",
    ".vscode",
    ".flet",
    ".cursor",
}

# Каталоги стандартной поставки CPython: не приложения, даже если лежат
# рядом с python.exe, который сам по себе командой остаётся.
_PYTHON_INTERNAL_DIRS = {"lib", "libs", "scripts", "tcl", "include"}

# Подстроки в имени файла: установщики и распространяемые пакеты.
_INSTALLER_MARKERS = ("setup", "redist", "uninstall", "dotnet-sdk")


def _parts(path: str) -> list[str]:
    norm = os.path.normpath(str(path)).lower()
    return [p for p in norm.split(os.sep) if p and not p.endswith(":")]


def _under_python_internals(parts: list[str]) -> bool:
    """True для Lib/Scripts/tcl внутри каталога Python3xx, не для python.exe."""
    for index, part in enumerate(parts):
        looks_like_runtime = part.startswith("python") and any(ch.isdigit() for ch in part)
        if not looks_like_runtime:
            continue
        if any(tail in _PYTHON_INTERNAL_DIRS for tail in parts[index + 1:]):
            return True
    return False


def _is_installer_name(path: str) -> bool:
    base = os.path.splitext(os.path.basename(str(path)))[0].lower()
    if base.startswith("unins"):
        return True
    return any(marker in base for marker in _INSTALLER_MARKERS)


def is_noise_executable(path: str) -> bool:
    """True, если путь не стоит показывать как голосовую команду."""
    if not path or not isinstance(path, str):
        return False
    # steam:// и прочие URI — это игры, не мусорные exe.
    if "://" in path:
        return False
    parts = _parts(path)
    if any(part in _NOISE_DIR_PARTS for part in parts):
        return True
    if _under_python_internals(parts):
        return True
    return _is_installer_name(path)


def is_noise_game(name: str) -> bool:
    """True для служебных «игр» Steam вроде redistributables."""
    low = (name or "").lower()
    markers = (
        "redistributable",
        "steamworks common",
        "steam linux runtime",
        "steamworks sdk",
    )
    return any(marker in low for marker in markers)


def prune_commands(data: dict[str, Any]) -> list[str]:
    """Удаляет мусорные записи из секций приложений и игр. Возвращает ключи.

    Меняет ``data`` на месте. Настоящие приложения и игры не трогает.
    """
    removed: list[str] = []
    apps = data.get("приложения")
    if isinstance(apps, dict):
        for key in list(apps):
            value = apps[key]
            path = value if isinstance(value, str) else ""
            if isinstance(value, dict):
                path = str(value.get("path") or "")
            if path and is_noise_executable(path):
                del apps[key]
                removed.append(str(key))

    for section in ("игры", "games"):
        games = data.get(section)
        if not isinstance(games, dict):
            continue
        for key in list(games):
            entry = games[key]
            name = key
            if isinstance(entry, dict):
                name = str(entry.get("name") or key)
            if is_noise_game(str(name)) or is_noise_game(str(key)):
                del games[key]
                removed.append(str(key))
    return removed
