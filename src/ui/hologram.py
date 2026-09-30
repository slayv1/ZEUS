"""Центральный голографический виджет (ТЗ v1.4 §3).

HologramWidget — отображение состояния AI с анимациями:
- IDLE: медленное дыхание, слабое свечение, вращение
- LISTENING: пульсация, волны, усиление свечения
- THINKING: вращение, частицы, динамический круг
- SPEAKING: синхронизация с голосом, расширение волн
- ERROR: красный glitch-эффект
"""
from __future__ import annotations

import threading
from typing import Optional

import flet as ft

from .animations import (
    ANIMATION_DURATION,
    ANIMATION_DURATION_SLOW,
    breathing_animation,
    glitch_effect,
    glow_pulse,
    reset_animations,
    rotate_continuous,
    waves_expand,
)
from .zeus_state import ZeusState, ZeusStateController


class HologramWidget(ft.Container):
    """Голографический виджет с анимациями по состоянию AI."""

    def __init__(
        self,
        state_controller: ZeusStateController,
        palette: dict[str, str] | None = None,
        size: int = 200,
    ) -> None:
        super().__init__()
        self._state_ctrl = state_controller
        self._palette = palette or {}
        self._size = size
        self._stop_event = threading.Event()

        # Внутренние элементы
        self._outer_ring = ft.Container()
        self._inner_circle = ft.Container()
        self._glow = ft.Container()
        self._status_text = ft.Text()

        self._build()
        self._state_ctrl.on_change(self._on_state_change)

    def _build(self) -> None:
        """Сборка структуры виджета."""
        accent = self._palette.get("accent", "#00E5FF")
        text_color = self._palette.get("text", "#E2E8F0")

        self._outer_ring = ft.Container(
            width=self._size,
            height=self._size,
            border=ft.Border.all(2, ft.Colors.with_opacity(0.3, accent)),
            border_radius=self._size // 2,
        )

        self._inner_circle = ft.Container(
            width=self._size * 0.6,
            height=self._size * 0.6,
            border_radius=int(self._size * 0.3),
            gradient=ft.RadialGradient(
                center=ft.Alignment(0, 0),
                radius=0.8,
                colors=[
                    ft.Colors.with_opacity(0.8, accent),
                    ft.Colors.with_opacity(0.2, accent),
                    ft.Colors.with_opacity(0.0, accent),
                ],
            ),
            animate_scale=ft.Animation(ANIMATION_DURATION, ft.AnimationCurve.EASE_IN_OUT),
            animate_opacity=ft.Animation(ANIMATION_DURATION, ft.AnimationCurve.EASE_IN_OUT),
        )

        self._glow = ft.Container(
            width=self._size * 1.2,
            height=self._size * 1.2,
            border_radius=int(self._size * 0.6),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.15, accent)),
            animate_scale=ft.Animation(ANIMATION_DURATION_SLOW, ft.AnimationCurve.EASE_IN_OUT),
            animate_opacity=ft.Animation(ANIMATION_DURATION_SLOW, ft.AnimationCurve.EASE_IN_OUT),
        )

        self._status_text = ft.Text(
            "IDLE",
            size=11,
            weight=ft.FontWeight.BOLD,
            color=text_color,
            text_align=ft.TextAlign.CENTER,
            opacity=0.8,
        )

        self.content = ft.Stack(
            [
                ft.Container(content=self._glow, alignment=ft.Alignment(0, 0)),
                ft.Container(content=self._outer_ring, alignment=ft.Alignment(0, 0)),
                ft.Container(content=self._inner_circle, alignment=ft.Alignment(0, 0)),
                ft.Container(content=self._status_text, alignment=ft.Alignment(0, 0.85)),
            ],
            width=self._size * 1.3,
            height=self._size * 1.3,
        )
        self.alignment = ft.Alignment(0, 0)

    def update_palette(self, palette: dict[str, str]) -> None:
        """Обновить палитру при смене темы."""
        self._palette = palette
        accent = palette.get("accent", "#00E5FF")
        text_color = palette.get("text", "#E2E8F0")

        self._outer_ring.border = ft.Border.all(2, ft.Colors.with_opacity(0.3, accent))
        self._inner_circle.gradient = ft.RadialGradient(
            center=ft.Alignment(0, 0),
            radius=0.8,
            colors=[
                ft.Colors.with_opacity(0.8, accent),
                ft.Colors.with_opacity(0.2, accent),
                ft.Colors.with_opacity(0.0, accent),
            ],
        )
        self._glow.border = ft.Border.all(1, ft.Colors.with_opacity(0.15, accent))
        self._status_text.color = text_color
        self.update()

    def _on_state_change(self, old: ZeusState, new: ZeusState) -> None:
        """Обработчик изменения состояния AI."""
        self._status_text.value = new.value.upper()
        self._apply_state_animation(new)

    def _apply_state_animation(self, state: ZeusState) -> None:
        """Применить анимацию для состояния."""
        accent = self._palette.get("accent", "#00E5FF")

        reset_animations(self._outer_ring)
        reset_animations(self._inner_circle)
        reset_animations(self._glow)

        if state == ZeusState.IDLE:
            self._inner_circle.opacity = 0.6
            self._inner_circle.scale = 1.0
            self._glow.opacity = 0.3
            self._glow.scale = 1.0
            breathing_animation(self._inner_circle, 3000)

        elif state == ZeusState.LISTENING:
            self._inner_circle.opacity = 0.9
            self._inner_circle.scale = 1.1
            self._glow.opacity = 0.6
            self._glow.scale = 1.1
            glow_pulse(self._inner_circle, 1500)

        elif state == ZeusState.THINKING:
            self._inner_circle.opacity = 0.8
            self._inner_circle.scale = 1.0
            self._glow.opacity = 0.4
            self._glow.scale = 1.0
            rotate_continuous(self._outer_ring, 3000)
            breathing_animation(self._inner_circle, 2000)

        elif state == ZeusState.SPEAKING:
            self._inner_circle.opacity = 1.0
            self._inner_circle.scale = 1.0
            self._glow.opacity = 0.7
            self._glow.scale = 1.0
            waves_expand(self._inner_circle, 1200)

        elif state == ZeusState.ERROR:
            self._inner_circle.opacity = 1.0
            self._inner_circle.scale = 1.05
            self._inner_circle.gradient = ft.RadialGradient(
                center=ft.Alignment(0, 0),
                radius=0.8,
                colors=[
                    ft.Colors.with_opacity(0.8, "#FF3B30"),
                    ft.Colors.with_opacity(0.3, "#FF3B30"),
                    ft.Colors.with_opacity(0.0, "#FF3B30"),
                ],
            )
            glitch_effect(self._inner_circle, 200)
            self.update()
            return

        self._inner_circle.gradient = ft.RadialGradient(
            center=ft.Alignment(0, 0),
            radius=0.8,
            colors=[
                ft.Colors.with_opacity(0.8, accent),
                ft.Colors.with_opacity(0.2, accent),
                ft.Colors.with_opacity(0.0, accent),
            ],
        )
        self.update()

    def stop(self) -> None:
        """Остановить анимации."""
        self._stop_event.set()
        reset_animations(self._outer_ring)
        reset_animations(self._inner_circle)
        reset_animations(self._glow)
