"""Единый Design System ZEUS v1.4 — Neural Core Interface.

ТЗ §12: один источник токенов для Dark/Light тем.
Все компоненты строятся через хелперы этого модуля, чтобы
переключение темы меняло весь UI консистентно.

Палитра Dark (основная):
  фон #050816, карточки #0F172A,
  Blue #2563EB, Cyan #22D3EE, Green #22C55E.
"""
from __future__ import annotations

import flet as ft

# === Токены ТЗ §12 ===
DARK_TOKENS: dict[str, str] = {
    "background_main": "#050816",
    "background_surface": "#0F172A",
    "color_primary": "#2563EB",
    "color_cyan": "#22D3EE",
    "color_green": "#22C55E",
    "text_high_emphasis": "#E2E8F0",
    "text_low_emphasis": "#64748B",
    "terminal_background": "#020617",
    "terminal_text": "#22C55E",
    "glow_primary": "#2563EB",
    "glow_cyan": "#22D3EE",
    "sidebar_bg": "#08111F",
    "card_border": "#1E293B",
    # Ядро-сфера (референс Ref.jpg: центр (24,126,205)):
    "globe": "#187ECD",
    "globe_ring": "#4DD2FF",
    # Алиасы для совместимости со старым кодом UI:
    "bg": "#050816",
    "surface": "#0F172A",
    "surface_2": "#111C33",
    "card": "#0F172A",
    "text": "#E2E8F0",
    "text_dim": "#64748B",
    "accent": "#2563EB",
    "accent_dim": "#1E40AF",
    "cyan": "#22D3EE",
    "electric": "#2563EB",
    "green": "#22C55E",
    "border": "#1E293B",
    "hover": "#16213A",
    "sidebar": "#08111F",
}

LIGHT_TOKENS: dict[str, str] = {
    "background_main": "#F1F5F9",
    "background_surface": "#FFFFFF",
    "color_primary": "#2563EB",
    "color_cyan": "#0891B2",
    "color_green": "#16A34A",
    "text_high_emphasis": "#0F172A",
    "text_low_emphasis": "#64748B",
    "terminal_background": "#0F172A",
    "terminal_text": "#4ADE80",
    "glow_primary": "#2563EB",
    "glow_cyan": "#0891B2",
    "sidebar_bg": "#FFFFFF",
    "card_border": "#E2E8F0",
    # Ядро-сфера (для светлой — чуть темнее для контраста):
    "globe": "#0B6BCB",
    "globe_ring": "#0284C7",
    # Алиасы:
    "bg": "#F1F5F9",
    "surface": "#FFFFFF",
    "surface_2": "#E8EDF6",
    "card": "#FFFFFF",
    "text": "#0F172A",
    "text_dim": "#64748B",
    "accent": "#2563EB",
    "accent_dim": "#1E40AF",
    "cyan": "#0891B2",
    "electric": "#2563EB",
    "green": "#16A34A",
    "border": "#E2E8F0",
    "hover": "#E8EFFA",
    "sidebar": "#FFFFFF",
}

PALETTES: dict[str, dict[str, str]] = {
    "dark": DARK_TOKENS,
    "light": LIGHT_TOKENS,
}

# === Размеры ТЗ ===
SIDEBAR_WIDTH = 250
LOGO_SIZE = 52
LOGO_RADIUS = 14
CORE_CARD_WIDTH = 300
CORE_CARD_HEIGHT = 250
NEURAL_CORE_MIN = 320
NEURAL_CORE_MAX = 400
SYSTEM_PANEL_WIDTH = 350
SYSTEM_PANEL_HEIGHT = 220
QUICK_ACTION_WIDTH = 140
QUICK_ACTION_HEIGHT = 40
TERMINAL_HEIGHT = 220
TITLE_SIZE = 32


def card(
    content: ft.Control,
    palette: dict[str, str],
    width: float | None = None,
    height: float | None = None,
    padding: int = 16,
    radius: int = 14,
) -> ft.Container:
    """Стеклянная карточка Neural Core: surface + тонкая рамка + скругление."""
    return ft.Container(
        content=content,
        width=width,
        height=height,
        bgcolor=palette.get("surface", "#0F172A"),
        border=ft.Border.all(1, palette.get("card_border", palette.get("border", "#1E293B"))),
        border_radius=radius,
        padding=ft.Padding(padding, padding, padding, padding),
    )


def section_title(text: str, palette: dict[str, str], size: int = 13) -> ft.Text:
    """Заголовок секции правой колонки (минимализм, ТЗ §6-8)."""
    return ft.Text(
        text,
        size=size,
        weight=ft.FontWeight.W_600,
        color=palette.get("text", "#E2E8F0"),
    )


def progress_bar(value: float, palette: dict[str, str], width: int = 150) -> ft.ProgressBar:
    """Тонкий progress bar для CPU/RAM/Disk (ТЗ §6)."""
    value = max(0.0, min(1.0, float(value)))
    return ft.ProgressBar(
        value=value,
        width=width,
        height=5,
        bgcolor=ft.Colors.with_opacity(0.15, palette.get("accent", "#2563EB")),
        color=palette.get("accent", "#2563EB"),
        border_radius=3,
    )
