# -*- coding: utf-8 -*-
"""Общая палитра и разделы навигации оболочки Flet.

boot, layout и handlers импортируют один и тот же словарь ``C``.
Смена темы — ``C.clear(); C.update(...)``, без перепривязки имени,
иначе модули увидели бы разные копии.
"""
from __future__ import annotations

import os
import sys

import flet as ft

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_SRC_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from ui.design_system import PALETTES as _CANON_PALETTES
except ImportError:  # запуск из src/
    from design_system import PALETTES as _CANON_PALETTES  # type: ignore[no-redef]

PALETTES = _CANON_PALETTES
# Копия, а не сам токен: clear/update не должны портить каноническую палитру.
C = dict(PALETTES["dark"])

SECTIONS = [
    {"label": "Главная", "icon": ft.Icons.DASHBOARD_ROUNDED},
    {"label": "Системные логи", "icon": ft.Icons.TERMINAL_ROUNDED},
    {"label": "Память", "icon": ft.Icons.PSYCHOLOGY_ROUNDED},
    {"label": "Плагины", "icon": ft.Icons.EXTENSION_ROUNDED},
    {"label": "Настройки", "icon": ft.Icons.SETTINGS_ROUNDED},
]
