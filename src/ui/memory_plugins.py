"""Страницы Memory и Plugins (ТЗ §10-11).

v1.4: UI-каркас готов, бэкенд памяти/плагинов — позже.
Компоненты только отображают переданные данные и колбэки.
"""
from __future__ import annotations

import flet as ft

try:
    from ui.design_system import TITLE_SIZE, card, section_title
except ImportError:  # запуск из src/
    from design_system import TITLE_SIZE, card, section_title  # type: ignore[no-redef]


def page_header(title: str, subtitle: str, palette: dict[str, str]) -> ft.Column:
    return ft.Column(
        [
            ft.Text(title, size=TITLE_SIZE, weight=ft.FontWeight.BOLD, color=palette.get("text", "#E2E8F0")),
            ft.Text(subtitle, size=13, color=palette.get("text_dim", "#64748B")),
        ],
        spacing=2,
    )


def build_memory_page(
    palette: dict[str, str],
    user_name: str = "—",
    preferences: str = "Пока пусто. Зевс запомнит важное здесь.",
    history: list[str] | None = None,
    on_clear=None,
) -> ft.Column:
    """MEMORY: пользователь / история / сохранённое + очистка."""
    history = history or []
    history_rows = [
        ft.Row(
            [
                ft.Icon(ft.Icons.HISTORY_ROUNDED, size=14, color=palette.get("accent", "#2563EB")),
                ft.Text(h, size=12, color=palette.get("text", "#E2E8F0"), expand=True),
            ],
            spacing=8,
        )
        for h in history
    ] or [ft.Text("История пока пуста.", size=12, color=palette.get("text_dim", "#64748B"))]
    user_card = card(
        ft.Column(
            [
                section_title("👤 Пользователь", palette),
                ft.Text(f"Имя: {user_name}", size=12, color=palette.get("text", "#E2E8F0")),
                ft.Text(f"Предпочтения: {preferences}", size=12, color=palette.get("text_dim", "#64748B")),
            ],
            spacing=6,
        ),
        palette,
    )
    history_card = card(
        ft.Column(
            [
                ft.Row(
                    [
                        section_title("🧠 История", palette),
                        ft.Container(expand=True),
                        ft.TextButton("Очистить всё", icon=ft.Icons.DELETE_OUTLINE_ROUNDED, on_click=on_clear),
                    ]
                ),
                *history_rows,
            ],
            spacing=7,
        ),
        palette,
    )
    return ft.Column(
        [page_header("MEMORY", "Полезный контекст, который хранит Зевс.", palette), user_card, history_card],
        spacing=14,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )


def build_plugins_page(
    palette: dict[str, str],
    plugins: list[dict] | None = None,
    on_toggle=None,
) -> ft.Column:
    """PLUGINS: название / описание / вкл-выкл / настройки."""
    plugins = plugins or [
        {"key": "browser", "icon": "🌐", "name": "Browser", "desc": "Открытие сайтов и поиск.", "active": True},
        {"key": "music", "icon": "🎵", "name": "Music", "desc": "Управление музыкой.", "active": True},
        {"key": "files", "icon": "📁", "name": "File Manager", "desc": "Поиск и открытие файлов.", "active": True},
        {"key": "telegram", "icon": "💬", "name": "Telegram", "desc": "Интеграция с Telegram.", "active": False},
    ]
    cards: list[ft.Control] = []
    for p in plugins:
        active = bool(p.get("active"))
        dot = palette.get("green", "#22C55E") if active else palette.get("text_dim", "#64748B")
        switch = ft.Switch(
            value=active,
            active_color=palette.get("accent", "#2563EB"),
            data=p.get("key"),
            on_change=(lambda e, k=p.get("key"): on_toggle(k, bool(e.control.value)) if on_toggle else None),
        )
        cards.append(
            card(
                ft.Row(
                    [
                        ft.Text(str(p.get("icon", "🧩")), size=22),
                        ft.Column(
                            [
                                ft.Text(str(p.get("name", "?")), size=13, weight=ft.FontWeight.W_600, color=palette.get("text", "#E2E8F0")),
                                ft.Text(str(p.get("desc", "")), size=11.5, color=palette.get("text_dim", "#64748B")),
                                ft.Row(
                                    [
                                        ft.Container(width=7, height=7, border_radius=4, bgcolor=dot),
                                        ft.Text("ACTIVE" if active else "OFF", size=11, color=dot),
                                    ],
                                    spacing=6,
                                ),
                            ],
                            spacing=3,
                            expand=True,
                        ),
                        switch,
                    ],
                    spacing=12,
                ),
                palette,
            )
        )
    return ft.Column(
        [page_header("PLUGINS", "Расширения Зевса.", palette), *cards],
        spacing=12,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
