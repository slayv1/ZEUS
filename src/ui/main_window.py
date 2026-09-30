"""Новое главное окно ZEUS v1.4 (ТЗ v1.4 §2).

Структура:
+--------------------------------+
| ZEUS                    ⚙️     |
|                                |
|          HOLOGRAM              |
|                                |
|        ◉  LISTENING            |
|                                |
|  History / Chat                |
|                                |
|       ◯       ◯                |
|    Listen    Power             |
+--------------------------------+
"""
from __future__ import annotations

import flet as ft

from .buttons import ListenButton, PowerButton
from .hologram import HologramWidget
from .theme import get_theme_manager
from .zeus_state import ZeusStateController


class ZeusMainWindow(ft.Container):
    """Главное окно ZEUS v1.4 с голограммой и floating buttons."""

    def __init__(
        self,
        state_controller: ZeusStateController,
        on_listen: callable | None = None,
        on_power: callable | None = None,
    ) -> None:
        super().__init__()
        self._state_ctrl = state_controller
        self._on_listen = on_listen
        self._on_power = on_power

        self._theme_manager = get_theme_manager()
        self._palette = self._theme_manager.get_palette()

        self._build()
        self._theme_manager.on_change(self._on_theme_change)

    def _build(self) -> None:
        """Сборка главного окна."""
        p = self._palette

        self._hologram = HologramWidget(
            state_controller=self._state_ctrl,
            palette=p,
            size=180,
        )

        self._status_text = ft.Text(
            "ZEUS is ready",
            size=12,
            color=p.get("text_dim", "#94A3B8"),
            text_align=ft.TextAlign.CENTER,
            opacity=0.7,
        )

        self._listen_btn = ListenButton(palette=p, on_click=self._handle_listen)
        self._power_btn = PowerButton(palette=p, on_click=self._handle_power)

        buttons_row = ft.Row(
            [self._listen_btn, ft.Container(width=24), self._power_btn],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=0,
        )

        self._chat_history = ft.Column([], scroll=ft.ScrollMode.AUTO, expand=True, spacing=4)

        chat_container = ft.Container(
            content=self._chat_history,
            bgcolor=ft.Colors.with_opacity(0.03, p.get("surface", "#111827")),
            border_radius=12,
            padding=ft.Padding(12, 12, 12, 12),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.1, p.get("border", "#1E293B"))),
            expand=True,
        )

        self.content = ft.Column(
            [
                ft.Container(content=self._hologram, alignment=ft.Alignment(0, 0), expand=True),
                ft.Container(content=self._status_text, alignment=ft.Alignment(0, 0), padding=ft.Padding(0, 0, 0, 8)),
                chat_container,
                ft.Container(height=12),
                buttons_row,
                ft.Container(height=8),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True,
        )

        self.bgcolor = p.get("bg", "#080B12")
        self.padding = ft.Padding(20, 20, 20, 20)
        self.expand = True

    def _handle_listen(self, e: ft.ControlEvent) -> None:
        """Обработчик кнопки Listen."""
        if self._on_listen:
            self._on_listen(e)

    def _handle_power(self, e: ft.ControlEvent) -> None:
        """Обработчик кнопки Power."""
        if self._on_power:
            self._on_power(e)

    def _on_theme_change(self, theme: str, palette: dict[str, str]) -> None:
        """Обработчик смены темы."""
        self._palette = palette
        self.bgcolor = palette.get("bg", "#080B12")
        self._hologram.update_palette(palette)
        self._listen_btn.update_palette(palette)
        self._power_btn.update_palette(palette)
        self._status_text.color = palette.get("text_dim", "#94A3B8")
        if hasattr(self, "update"):
            self.update()

    def set_listening(self, is_listening: bool) -> None:
        """Обновить состояние кнопки Listen."""
        self._listen_btn.set_listening(is_listening)

    def set_power(self, is_active: bool) -> None:
        """Обновить состояние кнопки Power."""
        self._power_btn.set_active(is_active)

    def set_status_text(self, text: str) -> None:
        """Обновить текст статуса."""
        self._status_text.value = text
        if hasattr(self, "update"):
            self.update()

    def add_chat_message(self, text: str, is_user: bool = True) -> None:
        """Добавить сообщение в чат."""
        p = self._palette
        bg = (
            ft.Colors.with_opacity(0.08, p.get("accent", "#00E5FF"))
            if is_user
            else ft.Colors.with_opacity(0.05, p.get("surface", "#111827"))
        )
        align = ft.MainAxisAlignment.END if is_user else ft.MainAxisAlignment.START

        msg = ft.Container(
            content=ft.Text(text, size=13, color=p.get("text", "#E2E8F0")),
            bgcolor=bg,
            border_radius=8,
            padding=ft.Padding(8, 6, 8, 6),
        )
        self._chat_history.controls.append(ft.Row([msg], alignment=align))
        if hasattr(self._chat_history, "update"):
            self._chat_history.update()

    def stop(self) -> None:
        """Остановить компоненты."""
        self._hologram.stop()
