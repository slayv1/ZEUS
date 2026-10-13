# -*- coding: utf-8 -*-
"""Устройства ввода и вывода и горячее подключение."""
from __future__ import annotations

import threading
from typing import Any

from core.logging_service import log


class ZeusDevicesMixin:
    def stop_device_watcher(self):
        """Останавливает наблюдатель аудиоустройств, если он был запущен."""
        try:
            from core import audio_devices
            if hasattr(audio_devices, "stop_watcher"):
                audio_devices.stop_watcher()
        except Exception:
            pass

    def start_device_watcher(
        self, on_event: Callable[[dict[str, Any]], None] | None = None
    ) -> bool:
        """Запускает hot-plug наблюдатель и связывает UI-колбэк (ТЗ §2.4/3.1).

        Вызывается из main_flet сразу после подключения UI-колбэков.
        Идемпотентно: повторный вызов лишь обновляет колбэк и гарантирует,
        что наблюдатель запущен.

        on_event получает payload-словари:
          {"event": "added", "device": {...}}
          {"event": "removed", "device": {...}}
          {"event": "auto_switched_input", "device": {...}}  — активный мик пропал
          {"event": "input_lost"} / {"event": "output_lost"} — устройств не осталось

        Returns:
            True, если наблюдатель запущен.
        """
        self._ui_device_event_cb = on_event
        try:
            from core import audio_devices as _ad
            dw = self._device_watcher or _ad.get_global_watcher()
            dw.start()  # идемпотентно внутри DeviceWatcher
            # Расширенные колбэки: трей-обновление + UI-payload + автовыбор.
            # Заменяют ранние лямбды из __init__ (тот же _on_audio_device_changed
            # вызывается внутри _hotplug_notify — поведение сохраняется).
            dw.on_device_added = self._on_hotplug_added
            dw.on_device_removed_with_info = self._on_hotplug_removed
            self._device_watcher = dw
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] Ошибка start_device_watcher: {exc}")
            return False

    def _hotplug_notify(self, event: str, dev: "Any | None" = None) -> None:
        """Единая точка hot-plug уведомлений: трей + UI-колбэк."""
        self._on_audio_device_changed()  # обновление аудио-подменю трея
        cb = getattr(self, "_ui_device_event_cb", None)
        if cb is None:
            return
        payload: dict[str, Any] = {"event": event}
        if dev is not None:
            try:
                payload["device"] = dev.to_dict()
            except Exception:  # noqa: BLE001
                payload["device"] = {}
        try:
            cb(payload)
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] Ошибка UI-события аудио ({event}): {exc}")

    def _on_hotplug_added(self, dev) -> None:
        """Подключено новое аудиоустройство."""
        self._hotplug_notify("added", dev)

    def _on_hotplug_removed(self, dev) -> None:
        """Отключено устройство: автовыбор, если ушёл активный микрофон/вывод.

        ТЗ §2.4: при исчезновении выбранного микрофона — автовыбор по
        приоритету типов (pick_best_device); если ввода не осталось —
        событие input_lost. Для вывода — возврат к системному дефолту.
        """
        self._hotplug_notify("removed", dev)
        try:
            voice = self.settings.get("voice_settings", {}) or {}
            if dev.is_input and voice.get("input_device_id") == dev.id:
                from core.audio_devices import list_devices, pick_best_device
                best = pick_best_device(list_devices(), kind="input")
                if best is not None:
                    self.set_input_device(best.id)
                    self._hotplug_notify("auto_switched_input", best)
                else:
                    self.set_input_device(None)
                    self._hotplug_notify("input_lost")
            elif (not dev.is_input) and voice.get("output_device_id") == dev.id:
                self.set_output_device(None)
                self._hotplug_notify("output_lost")
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] Ошибка автопереключения после отключения: {exc}")

    # ------------------------------------------------------------------
    # Управление аудиоустройствами
    # ------------------------------------------------------------------

    def get_available_input_devices(self) -> list[dict]:
        """Возвращает список доступных устройств ввода (микрофонов).

        Returns:
            Список словарей [{"id": ..., "name": ..., "label": ..., ...}].
        """
        try:
            from core.audio_devices import list_devices
            return [d.to_dict() for d in list_devices() if d.is_input]
        except Exception:  # noqa: BLE001
            return []

    def get_available_output_devices(self) -> list[dict]:
        """Возвращает список доступных устройств вывода (динамики/наушники).

        Returns:
            Список словарей [{"id": ..., "name": ..., "label": ..., ...}].
        """
        try:
            from core.audio_devices import list_devices
            return [d.to_dict() for d in list_devices() if not d.is_input]
        except Exception:  # noqa: BLE001
            return []

    def set_input_device(self, device_id: str | None) -> None:
        """Переключает активный микрофон (трей/настройки, Спринт 1 §2.3).

        Сохраняет выбор в settings.json и применяет его штатным API
        AudioEngine.switch_input_device: резолв ID→индекс, проверка
        микрофона, обновление wake-детектора и перезапуск циклов
        прослушивания. device_id=None — авто-режим (системный дефолт).

        Args:
            device_id: ID устройства из AudioDevice.id, либо None — авто.
        """
        self.update_settings("voice_settings", "input_device_id", device_id)
        try:
            switch = getattr(self.audio, "switch_input_device", None)
            if callable(switch):
                switch(device_id)
            else:
                # Заглушки/старые движки: прямой индекс без перезапуска циклов
                from core.audio_devices import resolve_device_id_to_index
                idx = resolve_device_id_to_index(device_id, "input") if device_id else None
                self.audio._mic_device_index = idx
            name = self._device_display_name(device_id, "input")
            shown = f"«{name}»" if name else "авто (системный по умолчанию)"
            log(f"[AUDIO] 🎤 Активный микрофон: {shown}")
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] Ошибка set_input_device: {exc}")

    def set_output_device(self, device_id: str | None) -> None:
        """Переключает активное устройство вывода (Спринт 1 §2.3).

        Сохраняет выбор в settings.json и применяет его штатным API
        AudioEngine.switch_output_device; TTS-движок подхватывает индекс
        при следующем speak(). device_id=None — авто-режим.

        Args:
            device_id: ID устройства из AudioDevice.id, либо None — авто.
        """
        self.update_settings("voice_settings", "output_device_id", device_id)
        try:
            switch = getattr(self.audio, "switch_output_device", None)
            if callable(switch):
                switch(device_id)
            elif hasattr(self.audio, "_output_device_index"):
                from core.audio_devices import resolve_device_id_to_index
                idx = resolve_device_id_to_index(device_id, "output") if device_id else None
                self.audio._output_device_index = idx
            name = self._device_display_name(device_id, "output")
            shown = f"«{name}»" if name else "авто (системный по умолчанию)"
            log(f"[AUDIO] 🔊 Активный вывод: {shown}")
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] Ошибка set_output_device: {exc}")

    @staticmethod
    def _device_display_name(device_id: str | None, kind: str) -> str | None:
        """Имя аудиоустройства по стабильному ID (для [AUDIO]-диагностики)."""
        if not device_id:
            return None
        try:
            from core.audio_devices import list_devices
            for dev in list_devices():
                if dev.id == device_id and dev.kind == kind:
                    return dev.name
        except Exception:  # noqa: BLE001
            pass
        return None

    def _on_audio_default_changed(self, kind: str, device_id: str) -> None:
        """Системный аудио-маршрут ОС сменился (Спринт 1, диагностика).

        Вызывается из DeviceWatcher в фоновом потоке. Пишет причину в
        [AUDIO]-строку терминала и обновляет аудио-подменю трея: активная
        отметка может «уехать» вслед за системным дефолтом.

        Примечание: явный выбор пользователя (settings.voice_settings)
        не перезаписывается — меняется лишь системный дефолт Windows.
        """
        if self._is_cleaned_up:
            return
        try:
            label = "ввода" if kind == "input" else "вывода"
            name = self._device_display_name(device_id, kind) or device_id
            log(f"[AUDIO] 🔀 Системный маршрут {label} переключён: «{name}»")
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] Ошибка _on_audio_default_changed: {exc}")
        self._on_audio_device_changed()

    def _on_audio_device_changed(self) -> None:
        """Callback горячего подключения/отключения аудиоустройства.

        Вызывается из DeviceWatcher в фоновом потоке. Уведомляет трей
        об обновлении списка устройств через on_tray_audio_update.
        """
        if self._is_cleaned_up:
            return
        cb = self.on_tray_audio_update
        if cb is not None:
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                log(f"[Controller] Ошибка on_tray_audio_update: {exc}")

