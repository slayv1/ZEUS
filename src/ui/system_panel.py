"""Правая колонка Neural Core (ТЗ §6-8):
SystemPanel 350x220 + QuickActions 140x40 + RecentCommands.
"""
from __future__ import annotations

import flet as ft

try:
    from ui.design_system import (
        QUICK_ACTION_HEIGHT,
        QUICK_ACTION_WIDTH,
        SYSTEM_PANEL_HEIGHT,
        SYSTEM_PANEL_WIDTH,
        card,
        progress_bar,
        section_title,
    )
except ImportError:  # запуск из src/
    from design_system import (  # type: ignore[no-redef]
        QUICK_ACTION_HEIGHT,
        QUICK_ACTION_WIDTH,
        SYSTEM_PANEL_HEIGHT,
        SYSTEM_PANEL_WIDTH,
        card,
        progress_bar,
        section_title,
    )


def build_system_panel(
    palette: dict[str, str],
    cpu: float = 0.18,
    ram: float = 0.42,
    disk: float = 0.63,
    voice: str = "Ready",
    ai_model: str = "Connected",
    transparent: bool = False,
) -> ft.Control:
    """System Status: CPU/RAM/Disk бары + Voice/AI строки.

    transparent=True — без подложки и рамки (референс Ref.jpg): тонкие
    строки-метрики прямо на фоне. Возвращает Column.
    """
    text = palette.get("text", "#E2E8F0")
    dim = palette.get("text_dim", "#64748B")
    green = palette.get("green", "#22C55E")

    def _metric(icon: ft.IconData, name: str, value: float) -> ft.Row:
        return ft.Row(
            [
                ft.Icon(icon, size=15, color=dim),
                ft.Text(name, size=12, color=dim, width=44),
                progress_bar(value, palette, width=150),
                ft.Text(f"{int(round(value * 100))}%", size=12, color=text, width=42, text_align=ft.TextAlign.RIGHT),
            ],
            spacing=8,
        )

    def _state_row(label: str, value: str, ok: bool = True) -> ft.Row:
        dot_color = green if ok else dim
        return ft.Row(
            [
                ft.Container(width=7, height=7, border_radius=4, bgcolor=dot_color),
                ft.Text(label, size=12, color=dim, expand=True),
                ft.Text(value, size=12, color=text),
            ],
            spacing=8,
        )

    body = ft.Column(
        [
            section_title("Состояние системы", palette),
            ft.Container(height=4),
            _metric(ft.Icons.DEVELOPER_BOARD_ROUNDED, "CPU", cpu),
            _metric(ft.Icons.MEMORY_ROUNDED, "RAM", ram),
            _metric(ft.Icons.STORAGE_ROUNDED, "Диск", disk),
            ft.Container(height=4),
            _state_row("Voice", voice),
            _state_row("AI Model", ai_model),
        ],
        spacing=8,
    )
    if transparent:
        return body
    return card(body, palette, width=SYSTEM_PANEL_WIDTH, height=SYSTEM_PANEL_HEIGHT)


def build_quick_actions(
    palette: dict[str, str],
    on_action=None,
    transparent: bool = False,
) -> ft.Control:
    """Быстрые действия 2x2: браузер / файл / музыка / VS Code (ТЗ §7).

    transparent=True — без подложки и рамки.
    """
    accent = palette.get("accent", "#2563EB")
    text = palette.get("text", "#E2E8F0")
    actions = [
        (ft.Icons.LANGUAGE_ROUNDED, "Открыть браузер", "browser"),
        (ft.Icons.FOLDER_OPEN_ROUNDED, "Найти файл", "file"),
        (ft.Icons.MUSIC_NOTE_ROUNDED, "Включить музыку", "music"),
        (ft.Icons.CODE_ROUNDED, "Запустить VS Code", "vscode"),
    ]
    buttons: list[ft.Control] = []
    for icon, label, key in actions:
        btn = ft.Container(
            content=ft.Row(
                [ft.Icon(icon, size=15, color=accent), ft.Text(label, size=12, color=text)],
                spacing=7,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            width=QUICK_ACTION_WIDTH,
            height=QUICK_ACTION_HEIGHT,
            bgcolor=ft.Colors.with_opacity(0.06, accent),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.25, accent)),
            border_radius=10,
            alignment=ft.Alignment.CENTER,
            ink=True,
            data=key,
            on_click=(lambda e, k=key: on_action(k) if on_action else None),
        )
        buttons.append(btn)
    body = ft.Column(
        [
            section_title("Быстрые действия", palette),
            ft.Container(height=2),
            ft.Row([buttons[0], buttons[1]], spacing=10, alignment=ft.MainAxisAlignment.CENTER),
            ft.Row([buttons[2], buttons[3]], spacing=10, alignment=ft.MainAxisAlignment.CENTER),
        ],
        spacing=8,
    )
    if transparent:
        return body
    return card(body, palette)


def build_recent_commands(
    palette: dict[str, str],
    items: list[tuple[str, str]] | None = None,
    transparent: bool = False,
) -> ft.Control:
    """Последние команды: время + текст (ТЗ §8).

    transparent=True — без подложки и рамки.
    """
    text = palette.get("text", "#E2E8F0")
    dim = palette.get("text_dim", "#64748B")
    accent = palette.get("accent", "#2563EB")
    items = items or [
        ("17:42", "Открыть браузер"),
        ("17:38", "Включить музыку"),
        ("17:35", "Найди файл проекта"),
        ("17:32", "Покажи погоду"),
        ("17:28", "Запусти VS Code"),
        ("17:25", "Сделай скриншот"),
    ]
    rows = [
        ft.Row(
            [
                ft.Text(ts, size=11, color=dim, width=40),
                ft.Icon(ft.Icons.CIRCLE_ROUNDED, size=8, color=ft.Colors.with_opacity(0.6, accent)),
                ft.Text(cmd, size=12, color=text, expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ],
            spacing=8,
        )
        for ts, cmd in items
    ]
    header = ft.Row(
        [
            ft.Icon(ft.Icons.HISTORY_ROUNDED, size=15, color=accent),
            section_title("Последние команды", palette),
            ft.Container(expand=True),
            ft.Text("Все", size=11, color=accent),
        ],
        spacing=6,
    )
    body = ft.Column([header, ft.Container(height=2), *rows], spacing=7)
    if transparent:
        return body
    return card(body, palette)
