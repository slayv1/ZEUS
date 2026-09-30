"""Круглые floating buttons (ТЗ v1.4 §4).

Стиль: glassmorphism, прозрачность, тень, hover эффект.
Размер: 60x60 px.

Listen Button: микрофон, OFF=серый, ON=cyan glow
Power Button: ⏻, Active=green, Shutdown=red
"""
from __future__ import annotations

import flet as ft

from .animations import ANIMATION_DURATION, scale_to

BUTTON_SIZE = 60
BUTTON_RADIUS = BUTTON_SIZE // 2


class ListenButton(ft.Container):
    """Кнопка микрофона (ТЗ v1.4 §4)."""

    def __init__(
        self,
        palette: dict[str, str],
        on_click: callable | None = None,
    ) -> None:
        super().__init__()
        self._palette = palette
        self._on_click = on_click
        self._is_listening = False

        self._icon = ft.Icon(
            ft.Icons.MIC_ROUNDED,
            size=24,
            color=self._get_icon_color(),
        )

        self.content = self._icon
        self.width = BUTTON_SIZE
        self.height = BUTTON_SIZE
        self.border_radius = BUTTON_RADIUS
        self.alignment = ft.Alignment(0, 0)
        self.on_click = self._handle_click
        self.on_hover = self._handle_hover
        self.animate_scale = ft.Animation(ANIMATION_DURATION, ft.AnimationCurve.EASE_OUT)

        self._apply_style()

    def _get_icon_color(self) -> str:
        if self._is_listening:
            return self._palette.get("accent", "#00E5FF")
        return self._palette.get("text_dim", "#94A3B8")

    def _apply_style(self) -> None:
        """Применить glassmorphism стиль."""
        accent = self._palette.get("accent", "#00E5FF")
        surface = self._palette.get("surface", "#111827")

        if self._is_listening:
            border_color = ft.Colors.with_opacity(0.6, accent)
            bg_opacity = 0.25
        else:
            border_color = ft.Colors.with_opacity(0.2, surface)
            bg_opacity = 0.1

        self.bgcolor = ft.Colors.with_opacity(bg_opacity, surface)
        self.border = ft.Border.all(1, border_color)
        self.shadow = ft.BoxShadow(
            spread_radius=0,
            blur_radius=12 if self._is_listening else 6,
            color=ft.Colors.with_opacity(0.4 if self._is_listening else 0.2, accent),
            offset=ft.Offset(0, 2),
        )
        self._icon.color = self._get_icon_color()

    def set_listening(self, is_listening: bool) -> None:
        """Переключить состояние микрофона."""
        self._is_listening = is_listening
        self._apply_style()
        if is_listening:
            scale_to(self.animate_scale and self or self, 1.1, 150)
        if hasattr(self, "update"):
            self.update()

    def update_palette(self, palette: dict[str, str]) -> None:
        """Обновить палитру."""
        self._palette = palette
        self._apply_style()
        if hasattr(self, "update"):
            self.update()

    def _handle_click(self, e: ft.ControlEvent) -> None:
        if self._on_click:
            self._on_click(e)

    def _handle_hover(self, e: ft.ControlEvent) -> None:
        if e.data == "true":
            self.scale = 1.1
        else:
            self.scale = 1.0
        if hasattr(self, "update"):
            self.update()


class PowerButton(ft.Container):
    """Кнопка питания (ТЗ v1.4 §4)."""

    def __init__(
        self,
        palette: dict[str, str],
        on_click: callable | None = None,
    ) -> None:
        super().__init__()
        self._palette = palette
        self._on_click = on_click
        self._is_active = True

        self._icon = ft.Icon(
            ft.Icons.POWER_SETTINGS_NEW_ROUNDED,
            size=24,
            color=self._get_icon_color(),
        )

        self.content = self._icon
        self.width = BUTTON_SIZE
        self.height = BUTTON_SIZE
        self.border_radius = BUTTON_RADIUS
        self.alignment = ft.Alignment(0, 0)
        self.on_click = self._handle_click
        self.on_hover = self._handle_hover
        self.animate_scale = ft.Animation(ANIMATION_DURATION, ft.AnimationCurve.EASE_OUT)

        self._apply_style()

    def _get_icon_color(self) -> str:
        if self._is_active:
            return "#22C55E"  # green
        return "#FF3B30"  # red

    def _apply_style(self) -> None:
        """Применить glassmorphism стиль."""
        surface = self._palette.get("surface", "#111827")
        accent_color = self._get_icon_color()

        self.bgcolor = ft.Colors.with_opacity(0.1, surface)
        self.border = ft.Border.all(1, ft.Colors.with_opacity(0.2, surface))
        self.shadow = ft.BoxShadow(
            spread_radius=0,
            blur_radius=6,
            color=ft.Colors.with_opacity(0.3, accent_color),
            offset=ft.Offset(0, 2),
        )
        self._icon.color = self._get_icon_color()

    def set_active(self, is_active: bool) -> None:
        """Переключить состояние питания."""
        self._is_active = is_active
        self._apply_style()
        if hasattr(self, "update"):
            self.update()

    def update_palette(self, palette: dict[str, str]) -> None:
        """Обновить палитру."""
        self._palette = palette
        self._apply_style()
        if hasattr(self, "update"):
            self.update()

    def _handle_click(self, e: ft.ControlEvent) -> None:
        if self._on_click:
            self._on_click(e)

    def _handle_hover(self, e: ft.ControlEvent) -> None:
        if e.data == "true":
            self.scale = 1.1
        else:
            self.scale = 1.0
        if hasattr(self, "update"):
            self.update()
