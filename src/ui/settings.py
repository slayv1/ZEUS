"""Панель настроек (ТЗ v1.4 §11).

Разделы:
- Appearance (Dark/Light theme)
- Voice (Microphone, Speaker)
- AI (Model)
- System (Auto start)
"""
from __future__ import annotations

import flet as ft

from .theme import get_theme_manager


class SettingsPanel(ft.Container):
    """Панель настроек приложения."""

    def __init__(
        self,
        palette: dict[str, str],
        on_theme_toggle: callable | None = None,
        on_mic_change: callable | None = None,
        on_speaker_change: callable | None = None,
        on_model_change: callable | None = None,
        on_autostart_toggle: callable | None = None,
    ) -> None:
        super().__init__()
        self._palette = palette
        self._on_theme_toggle = on_theme_toggle
        self._on_mic_change = on_mic_change
        self._on_speaker_change = on_speaker_change
        self._on_model_change = on_model_change
        self._on_autostart_toggle = on_autostart_toggle

        self._theme_manager = get_theme_manager()
        self._build()

    def _build(self) -> None:
        """Сборка панели настроек."""
        theme_section = self._build_section(
            title="Appearance",
            icon=ft.Icons.PALETTE_ROUNDED,
            controls=[self._build_theme_toggle()],
        )
        voice_section = self._build_section(
            title="Voice",
            icon=ft.Icons.RECORD_VOICE_OVER_ROUNDED,
            controls=[
                self._build_dropdown("Microphone", ft.Icons.MIC_ROUNDED, ["Default", "USB Mic"]),
                self._build_dropdown("Speaker", ft.Icons.SPEAKER_ROUNDED, ["Default", "Headphones"]),
            ],
        )
        ai_section = self._build_section(
            title="AI",
            icon=ft.Icons.PSYCHOLOGY_ROUNDED,
            controls=[self._build_dropdown("Model", ft.Icons.MODEL_TRAINING_ROUNDED, ["llama3", "mistral", "gemma"])],
        )
        system_section = self._build_section(
            title="System",
            icon=ft.Icons.SETTINGS_ROUNDED,
            controls=[self._build_switch("Auto start", True, self._on_autostart_toggle)],
        )

        self.content = ft.Column(
            [
                theme_section,
                ft.Container(height=16),
                voice_section,
                ft.Container(height=16),
                ai_section,
                ft.Container(height=16),
                system_section,
            ],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        self.padding = ft.Padding(16, 16, 16, 16)

    def _build_section(self, title: str, icon: ft.Icon, controls: list[ft.Control]) -> ft.Container:
        """Секция настроек с заголовком."""
        p = self._palette
        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(icon, size=18, color=p.get("accent", "#00E5FF")),
                            ft.Text(title, weight=ft.FontWeight.BOLD, size=14, color=p.get("text", "#E2E8F0")),
                        ],
                        spacing=8,
                    ),
                    ft.Container(height=8),
                    *controls,
                ]
            ),
            bgcolor=ft.Colors.with_opacity(0.05, p.get("surface", "#111827")),
            border_radius=12,
            padding=ft.Padding(16, 16, 16, 16),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.1, p.get("border", "#1E293B"))),
        )

    def _build_theme_toggle(self) -> ft.Control:
        """Переключатель темы Dark/Light."""
        p = self._palette
        is_dark = self._theme_manager.get_theme() == "dark"

        return ft.Row(
            [
                ft.Icon(ft.Icons.DARK_MODE_ROUNDED, size=18, color=p.get("text_dim", "#94A3B8")),
                ft.Text("Dark", size=13, color=p.get("text", "#E2E8F0")),
                ft.Switch(
                    value=is_dark,
                    active_color=p.get("accent", "#00E5FF"),
                    on_change=self._handle_theme_toggle,
                ),
                ft.Icon(ft.Icons.LIGHT_MODE_ROUNDED, size=18, color=p.get("text_dim", "#94A3B8")),
                ft.Text("Light", size=13, color=p.get("text", "#E2E8F0")),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )

    def _build_dropdown(self, label: str, icon: ft.Icon, options: list[str]) -> ft.Control:
        """Выпадающий список настроек."""
        p = self._palette

        return ft.Row(
            [
                ft.Icon(icon, size=18, color=p.get("text_dim", "#94A3B8")),
                ft.Text(label, size=13, color=p.get("text", "#E2E8F0"), expand=True),
                ft.Dropdown(
                    value=options[0] if options else None,
                    options=[ft.dropdown.Option(o) for o in options],
                    width=160,
                    text_size=12,
                    bgcolor=ft.Colors.with_opacity(0.05, p.get("surface", "#111827")),
                    border_color=ft.Colors.with_opacity(0.2, p.get("border", "#1E293B")),
                    color=p.get("text", "#E2E8F0"),
                    on_select=self._handle_dropdown_change(label),
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )

    def _build_switch(self, label: str, value: bool, on_change: callable | None) -> ft.Control:
        """Переключатель настроек."""
        p = self._palette

        return ft.Row(
            [
                ft.Text(label, size=13, color=p.get("text", "#E2E8F0"), expand=True),
                ft.Switch(
                    value=value,
                    active_color=p.get("accent", "#00E5FF"),
                    on_change=on_change,
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )

    def _handle_theme_toggle(self, e: ft.ControlEvent) -> None:
        """Обработчик переключения темы."""
        if self._on_theme_toggle:
            self._on_theme_toggle(e)

    def _handle_dropdown_change(self, label: str) -> callable:
        """Обработчик изменения выпадающего списка."""
        def _handler(e: ft.ControlEvent) -> None:
            if label == "Microphone" and self._on_mic_change:
                self._on_mic_change(e)
            elif label == "Speaker" and self._on_speaker_change:
                self._on_speaker_change(e)
            elif label == "Model" and self._on_model_change:
                self._on_model_change(e)
        return _handler

    def update_palette(self, palette: dict[str, str]) -> None:
        """Обновить палитру при смене темы."""
        self._palette = palette
        self._build()
        if hasattr(self, "update"):
            self.update()
