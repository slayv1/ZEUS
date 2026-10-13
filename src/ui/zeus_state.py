"""Единый менеджер состояния AI (ТЗ v1.4 §8).

Класс живёт в ``core.assistant_state``. Этот модуль оставлен, чтобы
прежние импорты ``from ui.zeus_state import ZeusState`` не менялись.
"""
from core.assistant_state import VOICE_TO_ZEUS, ZeusState, ZeusStateController

__all__ = ["VOICE_TO_ZEUS", "ZeusState", "ZeusStateController"]
