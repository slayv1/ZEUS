"""Терминал активности 220px (ТЗ §9): SYSTEM/VOICE/WAKE/AI CORE/MEMORY/PLUGIN/ZEUS.

Цветовая кодировка категорий + автопрокрутка. Backend-логика (_log)
остаётся в ZeusUI — здесь только оформление строк и контейнер.
"""
from __future__ import annotations

import flet as ft

try:
    from ui.design_system import TERMINAL_HEIGHT, section_title
except ImportError:  # запуск из src/
    from design_system import TERMINAL_HEIGHT, section_title  # type: ignore[no-redef]

# Цвета категорий по макету.
CATEGORY_COLORS: dict[str, str] = {
    "SYSTEM": "#64748B",
    "VOICE": "#22D3EE",
    "WAKE": "#22D3EE",
    "AI CORE": "#2563EB",
    "MEMORY": "#A78BFA",
    "PLUGIN": "#38BDF8",
    "ZEUS": "#22C55E",
}


def category_color(category: str) -> str:
    return CATEGORY_COLORS.get((category or "").upper(), "#64748B")


def build_log_line(timestamp: str, category: str, message: str, palette: dict[str, str]) -> ft.Row:
    """Одна строка терминала: [время] КАТЕГОРИЯ сообщение."""
    dim = palette.get("text_dim", "#64748B")
    term_text = palette.get("terminal_text", palette.get("text", "#E2E8F0"))
    return ft.Row(
        [
            ft.Text(f"[{timestamp}]", size=11, color=dim, font_family="Consolas", width=78),
            ft.Text(category.upper(), size=11, weight=ft.FontWeight.BOLD, color=category_color(category), font_family="Consolas", width=72),
            ft.Text(message, size=11.5, color=term_text, font_family="Consolas", expand=True),
        ],
        spacing=8,
    )


def build_terminal_frame(
    log_column: ft.Column,
    palette: dict[str, str],
    on_clear=None,
) -> ft.Container:
    """Рамка терминала: заголовок + колонка логов (высота 220px)."""
    accent = palette.get("accent", "#2563EB")
    header = ft.Row(
        [
            ft.Icon(ft.Icons.TERMINAL_ROUNDED, size=16, color=accent),
            section_title("Терминал активности", palette),
            ft.Container(expand=True),
            ft.TextButton(
                "Очистить",
                icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                style=ft.ButtonStyle(color=palette.get("text_dim", "#64748B")),
                on_click=on_clear,
            ),
        ],
        spacing=8,
    )
    return ft.Container(
        content=ft.Column(
            [
                header,
                ft.Container(
                    content=log_column,
                    height=TERMINAL_HEIGHT - 64,
                    bgcolor=palette.get("terminal_background", "#020617"),
                    border_radius=10,
                    padding=ft.Padding(12, 10, 12, 10),
                ),
            ],
            spacing=8,
        ),
        bgcolor=palette.get("surface", "#0F172A"),
        border=ft.Border.all(1, palette.get("card_border", palette.get("border", "#1E293B"))),
        border_radius=14,
        padding=ft.Padding(16, 14, 16, 14),
    )
