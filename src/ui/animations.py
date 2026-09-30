"""Модуль анимаций для UI (ТЗ v1.4 §9).

Плавные fade, scale, glow, rotation.
Запрещены резкие переключения и блокировка главного потока.
"""
from __future__ import annotations

import math
import typing

import flet as ft


# === Константы анимации ===
ANIMATION_DURATION = 300  # ms — стандартная длительность
ANIMATION_DURATION_SLOW = 600  # ms — медленные эффекты (дыхание)
ANIMATION_DURATION_FAST = 150  # ms — быстрые переходы


def fade_in(control: ft.Control, duration: int = ANIMATION_DURATION) -> None:
    """Плавное появление элемента."""
    control.opacity = 0
    control.animate_opacity = ft.Animation(duration, ft.AnimationCurve.EASE_IN_OUT)
    control.opacity = 1
    if hasattr(control, "update"):
        control.update()


def fade_out(control: ft.Control, duration: int = ANIMATION_DURATION) -> None:
    """Плавное исчезновение элемента."""
    control.opacity = 1
    control.animate_opacity = ft.Animation(duration, ft.AnimationCurve.EASE_IN_OUT)
    control.opacity = 0
    if hasattr(control, "update"):
        control.update()


def scale_in(control: ft.Control, duration: int = ANIMATION_DURATION) -> None:
    """Плавное масштабирование от 0 к 1."""
    control.scale = 0
    control.animate_scale = ft.Animation(duration, ft.AnimationCurve.EASE_OUT)
    control.scale = 1
    if hasattr(control, "update"):
        control.update()


def scale_to(control: ft.Control, target: float, duration: int = ANIMATION_DURATION) -> None:
    """Масштабирование к целевому значению."""
    control.animate_scale = ft.Animation(duration, ft.AnimationCurve.EASE_IN_OUT)
    control.scale = target
    if hasattr(control, "update"):
        control.update()


def rotate_continuous(control: ft.Control, duration: int = 4000) -> None:
    """Непрерывное вращение (для состояния THINKING)."""
    control.rotate = 0
    control.animate_rotation = ft.Animation(duration, ft.AnimationCurve.LINEAR)
    control.rotate = math.tau  # 360 градусов
    if hasattr(control, "update"):
        control.update()


def glow_pulse(control: ft.Control, duration: int = 1500) -> None:
    """Пульсация свечения (для состояния LISTENING)."""
    control.animate_opacity = ft.Animation(duration, ft.AnimationCurve.EASE_IN_OUT)
    control.opacity = 0.6
    if hasattr(control, "update"):
        control.update()


def breathing_animation(control: ft.Control, duration: int = 3000) -> None:
    """Эффект дыхания (для состояния IDLE) — медленное масштабирование."""
    control.animate_scale = ft.Animation(duration, ft.AnimationCurve.EASE_IN_OUT)
    control.scale = 1.05
    if hasattr(control, "update"):
        control.update()


def glitch_effect(control: ft.Control, duration: int = 200) -> None:
    """Красный glitch-эффект (для состояния ERROR)."""
    control.animate_offset = ft.Animation(duration, ft.AnimationCurve.EASE_IN_OUT)
    control.offset = ft.Offset(2, 0)
    if hasattr(control, "update"):
        control.update()
    # Возврат в исходное положение
    control.offset = ft.Offset(0, 0)
    if hasattr(control, "update"):
        control.update()


def waves_expand(control: ft.Control, duration: int = 1200) -> None:
    """Расширение волн (для состояния SPEAKING)."""
    control.animate_scale = ft.Animation(duration, ft.AnimationCurve.EASE_OUT)
    control.scale = 1.15
    if hasattr(control, "update"):
        control.update()


def reset_animations(control: ft.Control) -> None:
    """Сброс всех анимаций элемента."""
    control.animate_opacity = None
    control.animate_scale = None
    control.animate_rotation = None
    control.animate_offset = None
    control.opacity = 1
    control.scale = 1
    control.rotate = 0
    control.offset = ft.Offset(0, 0)
    if hasattr(control, "update"):
        control.update()
