"""Sidebar Neural Core (ТЗ §2): 250px, логотип 52x52 с glow, меню.

Пункты: Главная / Системные логи / Память / Плагины / Настройки.
Память и Плагины в v1.4 — заглушки-страницы (UI готов, бэкенд позже).
"""
from __future__ import annotations

import flet as ft

try:
    from ui.design_system import SIDEBAR_WIDTH, LOGO_RADIUS, LOGO_SIZE
except ImportError:  # запуск из src/
    from design_system import SIDEBAR_WIDTH, LOGO_RADIUS, LOGO_SIZE  # type: ignore[no-redef]

# Индексы разделов — единый контракт с ZeusUI._select_section.
SECTION_HOME = 0
SECTION_LOGS = 1
SECTION_MEMORY = 2
SECTION_PLUGINS = 3
SECTION_SETTINGS = 4

SECTIONS: list[dict] = [
    {"icon": ft.Icons.HOME_ROUNDED, "label": "Главная"},
    {"icon": ft.Icons.TERMINAL_ROUNDED, "label": "Системные логи"},
    {"icon": ft.Icons.PSYCHOLOGY_ROUNDED, "label": "Память"},
    {"icon": ft.Icons.EXTENSION_ROUNDED, "label": "Плагины"},
    {"icon": ft.Icons.SETTINGS_ROUNDED, "label": "Настройки"},
]


def build_logo(palette: dict[str, str], icon_src: str | None = None) -> ft.Row:
    """Логотип ⚡ ZEUS v1.4: иконка 52x52 с синим glow + текст."""
    accent = palette.get("accent", "#2563EB")
    if icon_src:
        icon_content: ft.Control = ft.Image(
            src=icon_src, width=28, height=28, fit=ft.BoxFit.CONTAIN,
        )
    else:
        icon_content = ft.Text("⚡", size=26)
    logo_icon = ft.Container(
        content=icon_content,
        width=LOGO_SIZE,
        height=LOGO_SIZE,
        alignment=ft.Alignment.CENTER,
        border_radius=LOGO_RADIUS,
        bgcolor=ft.Colors.with_opacity(0.12, accent),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.35, accent)),
        shadow=ft.BoxShadow(blur_radius=18, spread_radius=1, color=ft.Colors.with_opacity(0.45, accent)),
    )
    return ft.Row(
        [
            logo_icon,
            ft.Column(
                [
                    ft.Text("ZEUS", size=20, weight=ft.FontWeight.BOLD, color=palette.get("text", "#E2E8F0")),
                    ft.Text("v1.4", size=11, color=palette.get("text_dim", "#64748B")),
                ],
                spacing=0,
            ),
        ],
        spacing=12,
    )


def build_nav_item(
    icon: ft.IconData,
    label: str,
    selected: bool,
    palette: dict[str, str],
) -> ft.Container:
    """Пункт меню: выбранный — акцентная заливка, иначе прозрачный + hover."""
    accent = palette.get("accent", "#2563EB")
    text_color = "#FFFFFF" if selected else palette.get("text_dim", "#64748B")
    return ft.Container(
        content=ft.Row(
            [
                ft.Icon(icon, size=19, color=text_color),
                ft.Text(label, size=13.5, weight=ft.FontWeight.W_600 if selected else ft.FontWeight.NORMAL, color=text_color),
            ],
            spacing=10,
        ),
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        border_radius=10,
        bgcolor=ft.Colors.with_opacity(0.9, accent) if selected else ft.Colors.TRANSPARENT,
        shadow=ft.BoxShadow(blur_radius=12, color=ft.Colors.with_opacity(0.35, accent)) if selected else None,
    )


def build_status_footer(palette: dict[str, str]) -> ft.Container:
    """Низ сайдбара: пульс-сигнал + 'Система активна' (ТЗ: премиальный минимум)."""
    green = palette.get("green", "#22C55E")
    return ft.Container(
        content=ft.Row(
            [
                ft.Container(
                    width=9, height=9, border_radius=5, bgcolor=green,
                    shadow=ft.BoxShadow(blur_radius=8, spread_radius=1, color=green),
                ),
                ft.Column(
                    [
                        ft.Text("Система активна", size=12, weight=ft.FontWeight.W_600, color=palette.get("text", "#E2E8F0")),
                        ft.Text("Peak & Perf", size=10, color=palette.get("text_dim", "#64748B")),
                    ],
                    spacing=1,
                ),
            ],
            spacing=9,
        ),
        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
        bgcolor=ft.Colors.with_opacity(0.05, palette.get("surface", "#0F172A")),
        border=ft.Border.all(1, palette.get("card_border", palette.get("border", "#1E293B"))),
        border_radius=12,
    )
