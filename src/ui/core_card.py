"""Левая карточка ZEUS CORE 300x250 (ТЗ §4): glass + рамка #1E40AF.

Содержание: статус ONLINE, AI-модель, голосовой движок, память, плагины.
Значения подтягиваются из живого контроллера/настроек при наличии.
"""
from __future__ import annotations

import flet as ft

try:
    from ui.design_system import CORE_CARD_HEIGHT, CORE_CARD_WIDTH, card
except ImportError:  # запуск из src/
    from design_system import CORE_CARD_HEIGHT, CORE_CARD_WIDTH, card  # type: ignore[no-redef]


def _row(icon: ft.IconData, label: str, value: str, palette: dict[str, str]) -> ft.Row:
    dim = palette.get("text_dim", "#64748B")
    text = palette.get("text", "#E2E8F0")
    return ft.Row(
        [
            ft.Icon(icon, size=15, color=dim),
            ft.Text(label, size=12, color=dim, expand=True),
            ft.Text(value, size=12, color=text),
        ],
        spacing=8,
    )


def build_core_card(
    palette: dict[str, str],
    model: str = "llama3.1",
    voice_engine: str = "Vosk",
    memory: str = "Активна",
    plugins: str = "5 загружено",
    online: bool = True,
    transparent: bool = False,
) -> ft.Control:
    """Карточка ZEUS CORE для главной страницы.

    transparent=True — тонкая панель без подложки и рамки (референс Ref.jpg:
    текст-строки прямо на фоне, без «карточек»). Возвращает Column.
    """
    green = palette.get("green", "#22C55E")
    text = palette.get("text", "#E2E8F0")
    dim = palette.get("text_dim", "#64748B")
    status_label = "Онлайн" if online else "Офлайн"
    status_color = green if online else dim
    header = ft.Row(
        [
            ft.Container(
                width=10, height=10, border_radius=5, bgcolor=status_color,
                shadow=ft.BoxShadow(blur_radius=10, spread_radius=1, color=status_color),
            ),
            ft.Column(
                [
                    ft.Text("ZEUS CORE", size=14, weight=ft.FontWeight.BOLD, color=text),
                    ft.Text(status_label, size=11, color=status_color),
                ],
                spacing=0,
            ),
        ],
        spacing=10,
    )
    body = ft.Column(
        [
            header,
            ft.Container(height=6),
            _row(ft.Icons.PERSON_ROUNDED, "AI Модель", model, palette),
            _row(ft.Icons.MIC_ROUNDED, "Голосовой движок", voice_engine, palette),
            _row(ft.Icons.MEMORY_ROUNDED, "Память", memory, palette),
            _row(ft.Icons.EXTENSION_ROUNDED, "Плагины", plugins, palette),
        ],
        spacing=7,
    )
    if transparent:
        return body
    return card(body, palette, width=CORE_CARD_WIDTH, height=CORE_CARD_HEIGHT)
