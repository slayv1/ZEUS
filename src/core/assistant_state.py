# -*- coding: utf-8 -*-
"""Единый автомат состояния ассистента.

И микрофон (контроллер), и голограмма с кнопками смотрят на один
``ZeusStateController``. Строки IDLE/LISTENING/PROCESSING остаются
языком голосового цикла и переводятся в этот enum в одном месте.
"""
from __future__ import annotations

import threading
from enum import Enum


class ZeusState(Enum):
    """Состояния AI-ассистента (ТЗ v1.4 §8)."""
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"


# Голосовой цикл говорит строками; UI и голограмма — этим enum.
VOICE_TO_ZEUS = {
    "IDLE": ZeusState.IDLE,
    "LISTENING": ZeusState.LISTENING,
    "PROCESSING": ZeusState.THINKING,
    "SPEAKING": ZeusState.SPEAKING,
    "ERROR": ZeusState.ERROR,
}


class ZeusStateController:
    """Потокобезопасный контроллер состояния с подписчиками."""

    def __init__(self, initial: ZeusState = ZeusState.IDLE) -> None:
        self._state = initial
        self._lock = threading.Lock()
        self._listeners: list[callable] = []

    @property
    def state(self) -> ZeusState:
        with self._lock:
            return self._state

    def set_state(self, new_state: ZeusState) -> None:
        with self._lock:
            if new_state is self._state:
                return
            old_state = self._state
            self._state = new_state
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(old_state, new_state)
            except Exception as exc:  # noqa: BLE001
                from core.logging_service import error
                error(f"[State] подписчик состояния упал: {exc}")

    def apply_voice(self, voice_state: str) -> None:
        """Переводит строку голосового цикла в общее состояние."""
        self.set_state(VOICE_TO_ZEUS.get(voice_state, ZeusState.IDLE))

    def on_change(self, callback: callable) -> None:
        """Подписаться на изменение состояния: callback(old, new)."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: callable) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def is_active(self) -> bool:
        return self._state in (
            ZeusState.LISTENING,
            ZeusState.THINKING,
            ZeusState.SPEAKING,
        )
