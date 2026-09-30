"""Единый менеджер состояния AI (ТЗ v1.4 §8).

UI подписывается на изменения состояния и обновляет голограмму/кнопки.
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
            except Exception:
                pass

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
