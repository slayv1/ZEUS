# -*- coding: utf-8 -*-
"""Доработка Спринта 1: аудио-диагностика, авто-режим, мост логов в UI.

Покрывает финальные части Спринта 1 (ТЗ v1.4 §2.2/§2.3):
  1. logging_service.subscribe()/отписка — [AUDIO]-записи доходят до
     подписчиков (мост в терминал интерфейса); сбой подписчика изолирован.
  2. ZeusController.set_input_device()/set_output_device() делегируют
     AudioEngine.switch_* (а не подменяют приватный атрибут индекса).
  3. set_input_device(None) — переключение в авто-режим.
  4. _on_audio_default_changed() — смена системного маршрута ОС:
     [AUDIO]-лог с именем устройства + обновление трея.
  5. DeviceWatcher._disconnect_reason(): Bluetooth / USB / jack.
  6. DeviceWatcher._device_type_label() — человекочитаемые типы.
  7. AudioEngine.switch_input_device(None) — авто без резолва ID.
  8. _poll_loop пишет [AUDIO]-строку отключения (формат Спринта 1).
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from core import audio_devices, logging_service
from core.audio_devices import AudioDevice, DeviceType, DeviceWatcher
from core.controller import ZeusController


# ---------------------------------------------------------------------------
# Фикстуры-устройства и заглушка контроллера
# ---------------------------------------------------------------------------

def _mic(dev_id: str = "m1", name: str = "Тестовый микрофон") -> AudioDevice:
    return AudioDevice(id=dev_id, name=name, kind="input", index=0,
                       dtype=DeviceType.BUILTIN_MICROPHONE)


def _bt(dev_id: str = "bt1", name: str = "BT Headset") -> AudioDevice:
    return AudioDevice(id=dev_id, name=name, kind="input", index=1,
                       dtype=DeviceType.BLUETOOTH_HEADSET,
                       enumerator=r"BTHENUM\{0000110b}")


def _wired_jack() -> AudioDevice:
    return AudioDevice(id="w1", name="Wired Headset", kind="output", index=2,
                       dtype=DeviceType.WIRED_HEADSET)


def _shell() -> ZeusController:
    """Минимальный контроллер без __init__ (по образцу test_sprint1)."""
    ctrl = object.__new__(ZeusController)
    ctrl._is_cleaned_up = False
    ctrl.settings = {"voice_settings": {}}
    ctrl.settings_manager = MagicMock()
    ctrl.settings_manager.get_all.return_value = ctrl.settings
    ctrl.audio = MagicMock()
    ctrl.on_tray_audio_update = None
    return ctrl


# ---------------------------------------------------------------------------
# 1. logging_service.subscribe — мост [AUDIO]-записей
# ---------------------------------------------------------------------------

class TestLoggingSubscribe:
    def test_subscriber_receives_messages(self):
        from core import logging_service

        received: list[tuple[str, int]] = []
        unsub = logging_service.subscribe(
            lambda msg, lvl: received.append((msg, lvl))
        )
        try:
            logging_service.log("[AUDIO] 🎧 Диагностика: тест")
        finally:
            unsub()

        assert received, "подписчик не получил запись"
        assert received[-1][0].startswith("[AUDIO]")

    def test_unsubscribe_stops_delivery(self):
        from core import logging_service

        received: list[str] = []
        unsub = logging_service.subscribe(lambda m, l: received.append(m))
        unsub()
        unsub()  # повторная отписка идемпотентна

        logging_service.log("[AUDIO] после отписки")
        assert received == []

    def test_broken_subscriber_does_not_break_others(self):
        from core import logging_service

        delivered: list[str] = []

        def _bad(msg, lvl):
            raise RuntimeError("boom")

        unsub_bad = logging_service.subscribe(_bad)
        unsub_ok = logging_service.subscribe(lambda m, l: delivered.append(m))
        try:
            logging_service.log("[AUDIO] проверка изоляции")
        finally:
            unsub_bad()
            unsub_ok()

        assert any("проверка изоляции" in m for m in delivered)


# ---------------------------------------------------------------------------
# 2-4. Контроллер: делегирование переключения и смена системного маршрута
# ---------------------------------------------------------------------------

class TestControllerSwitching:
    def test_set_input_device_delegates_to_engine(self):
        ctrl = _shell()
        ctrl.set_input_device("mic-42")

        ctrl.audio.switch_input_device.assert_called_once_with("mic-42")
        ctrl.settings_manager.set.assert_called_once_with(
            "voice_settings", "input_device_id", "mic-42"
        )

    def test_set_input_device_none_switches_to_auto(self):
        ctrl = _shell()
        ctrl.set_input_device(None)
        ctrl.audio.switch_input_device.assert_called_once_with(None)

    def test_set_output_device_delegates_to_engine(self):
        ctrl = _shell()
        ctrl.set_output_device("spk-7")

        ctrl.audio.switch_output_device.assert_called_once_with("spk-7")
        ctrl.settings_manager.set.assert_called_once_with(
            "voice_settings", "output_device_id", "spk-7"
        )

    def test_fallback_without_engine_switch_api(self):
        """Движок без switch_*: индекс ставится напрямую (обратная совместимость)."""
        ctrl = _shell()
        ctrl.audio = MagicMock(spec=["_mic_device_index"])   # нет switch_input_device
        ctrl.audio._mic_device_index = None

        with patch("core.audio_devices.resolve_device_id_to_index", return_value=4):
            ctrl.set_input_device("mic-9")

        assert ctrl.audio._mic_device_index == 4

    def test_device_display_name_resolves(self):
        with patch("core.audio_devices.list_devices",
                   return_value=[_mic("m1", "USB Mic X")]):
            assert ZeusController._device_display_name("m1", "input") == "USB Mic X"
        assert ZeusController._device_display_name(None, "input") is None
        assert ZeusController._device_display_name("missing", "input") is None


class TestDefaultRouteChanged:
    def test_default_changed_logs_and_refreshes(self):
        ctrl = _shell()
        refreshed: list[bool] = []
        ctrl.on_tray_audio_update = lambda: refreshed.append(True)

        with patch("core.audio_devices.list_devices",
                   return_value=[_mic("m1", "USB Mic X")]):
            with patch("core.controller_devices.log") as mock_log:
                ctrl._on_audio_default_changed("input", "m1")

        assert refreshed == [True]  # подменю трея обновлено
        logged = " ".join(str(call.args[0]) for call in mock_log.call_args_list)
        assert "[AUDIO]" in logged and "USB Mic X" in logged

    def test_default_changed_skipped_after_cleanup(self):
        ctrl = _shell()
        ctrl._is_cleaned_up = True
        refreshed: list[bool] = []
        ctrl.on_tray_audio_update = lambda: refreshed.append(True)

        ctrl._on_audio_default_changed("output", "spk")

        assert refreshed == []


# ---------------------------------------------------------------------------
# 5-6. DeviceWatcher: причины отключения и человекочитаемые типы
# ---------------------------------------------------------------------------

class TestWatcherDiagnostics:
    def test_disconnect_reason_wired_jack(self):
        reason = DeviceWatcher._disconnect_reason(_wired_jack())
        assert "кабел" in reason or "jack" in reason

    def test_disconnect_reason_bluetooth(self):
        assert "Bluetooth" in DeviceWatcher._disconnect_reason(_bt())

    def test_device_type_labels(self):
        usb = AudioDevice(id="u1", name="USB Mic", kind="input", index=3,
                          dtype=DeviceType.USB_MICROPHONE)
        assert DeviceWatcher._device_type_label(_bt()) == "Bluetooth-гарнитура"
        assert DeviceWatcher._device_type_label(_mic()) == "Микрофон"
        assert DeviceWatcher._device_type_label(usb) == "USB-микрофон"
        assert DeviceWatcher._device_type_label(_wired_jack()) == "Проводная гарнитура"

    def test_poll_loop_logs_formatted_disconnect(self, monkeypatch):
        dw = DeviceWatcher()
        dw.POLL_INTERVAL = 0.01
        dw._snapshot = {"m1": _mic("m1", "Тестовый мик")}

        messages: list[str] = []
        monkeypatch.setattr(
            audio_devices, "log", lambda msg, *a, **k: messages.append(str(msg))
        )
        monkeypatch.setattr(audio_devices, "list_devices", lambda: [])

        thread = threading.Thread(target=dw._poll_loop, daemon=True)
        thread.start()
        time.sleep(0.15)
        dw._stop_event.set()
        thread.join(timeout=2.0)

        joined = "\n".join(messages)
        assert "[AUDIO]" in joined and "Тестовый мик" in joined
        assert "Отключение" in joined and "Микрофон" in joined


# ---------------------------------------------------------------------------
# 7. AudioEngine.switch_input_device — авто-режим и резолв ID
# ---------------------------------------------------------------------------

class TestAudioEngineSwitch:
    @staticmethod
    def _engine():
        from modules.audio_engine import AudioEngine
        eng = object.__new__(AudioEngine)  # без тяжёлого __init__
        eng._input_device_id = "old-device"
        eng._mic_device_index = 5
        eng.wake = None  # без wake-детектора: перезапуск циклов не требуется
        return eng

    def test_switch_none_restores_system_default(self):
        eng = self._engine()
        seen: list[int | None] = []

        def fake_check(preferred=None):
            seen.append(preferred)
            eng._mic_device_index = 3
            return True

        eng._check_microphone = fake_check
        assert eng.switch_input_device(None) is True
        assert seen == [None]                # авто: без preferred-индекса
        assert eng._input_device_id is None  # явный выбор сброшен

    def test_switch_by_id_resolves_index(self, monkeypatch):
        eng = self._engine()
        seen: list[int | None] = []

        def fake_check(preferred=None):
            seen.append(preferred)
            return True

        eng._check_microphone = fake_check
        monkeypatch.setattr(
            "core.audio_devices.resolve_device_id_to_index",
            lambda dev_id, kind="input": 7,
        )
        assert eng.switch_input_device("dev-7") is True
        assert seen == [7]
        assert eng._input_device_id == "dev-7"

    def test_switch_unknown_id_returns_false(self, monkeypatch):
        eng = self._engine()
        monkeypatch.setattr(
            "core.audio_devices.resolve_device_id_to_index",
            lambda dev_id, kind="input": None,
        )
        assert eng.switch_input_device("ghost") is False
        assert eng._input_device_id == "old-device"  # выбор не сброшен


# ---------------------------------------------------------------------------
# 8. start_device_watcher: связка с UI и автовыбор при отключении (ТЗ §2.4)
# ---------------------------------------------------------------------------

class _FakeWatcher:
    """Заглушка DeviceWatcher: фиксирует подписки и факт запуска."""

    def __init__(self):
        self.started = False
        self.on_device_added = None
        self.on_device_removed = None
        self.on_device_removed_with_info = None
        self.on_default_changed = None

    def start(self):
        self.started = True
        return True


def _spk(dev_id: str = "spk1", name: str = "Наушники") -> AudioDevice:
    return AudioDevice(id=dev_id, name=name, kind="output", index=1,
                       dtype=DeviceType.BUILTIN_SPEAKERS)


class TestHotplugWatcher:
    def _make(self, monkeypatch):
        ctrl = _shell()
        ctrl._device_watcher = None
        fake = _FakeWatcher()
        monkeypatch.setattr("core.audio_devices.get_global_watcher", lambda: fake)
        return ctrl, fake

    def test_start_wires_callbacks_and_returns_true(self, monkeypatch):
        ctrl, fake = self._make(monkeypatch)
        events: list[dict] = []

        assert ctrl.start_device_watcher(events.append) is True
        assert fake.started is True
        assert callable(fake.on_device_added)
        assert callable(fake.on_device_removed_with_info)

        fake.on_device_added(_mic("m9", "Новый мик"))
        assert events[-1]["event"] == "added"
        assert events[-1]["device"]["name"] == "Новый мик"

    def test_removed_active_input_auto_switches_by_priority(self, monkeypatch):
        ctrl, fake = self._make(monkeypatch)
        ctrl.settings["voice_settings"]["input_device_id"] = "m1"
        events: list[dict] = []
        ctrl.start_device_watcher(events.append)

        other = _mic("m2", "USB Mic")
        monkeypatch.setattr("core.audio_devices.list_devices", lambda: [other])
        fake.on_device_removed_with_info(_mic("m1", "Старый мик"))

        assert [e["event"] for e in events] == ["removed", "auto_switched_input"]
        assert events[-1]["device"]["id"] == "m2"
        ctrl.audio.switch_input_device.assert_called_once_with("m2")

    def test_removed_active_input_without_alternatives(self, monkeypatch):
        ctrl, fake = self._make(monkeypatch)
        ctrl.settings["voice_settings"]["input_device_id"] = "m1"
        events: list[dict] = []
        ctrl.start_device_watcher(events.append)

        monkeypatch.setattr("core.audio_devices.list_devices", lambda: [])
        fake.on_device_removed_with_info(_mic("m1"))

        assert [e["event"] for e in events] == ["removed", "input_lost"]
        ctrl.audio.switch_input_device.assert_called_once_with(None)

    def test_removed_inactive_device_sends_only_removed(self, monkeypatch):
        ctrl, fake = self._make(monkeypatch)
        events: list[dict] = []
        ctrl.start_device_watcher(events.append)

        fake.on_device_removed_with_info(_mic("voice"))

        assert [e["event"] for e in events] == ["removed"]

    def test_removed_active_output_resets_to_default(self, monkeypatch):
        ctrl, fake = self._make(monkeypatch)
        ctrl.settings["voice_settings"]["output_device_id"] = "spk1"
        events: list[dict] = []
        ctrl.start_device_watcher(events.append)

        fake.on_device_removed_with_info(_spk("spk1"))

        assert [e["event"] for e in events] == ["removed", "output_lost"]
        ctrl.audio.switch_output_device.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# 9. Мост [AUDIO]-диагностики в терминал интерфейса (ТЗ §3.2)
# ---------------------------------------------------------------------------

class TestAudioLogBridge:
    def _ui(self):
        import src.ui.main_flet as mf

        ui = mf.ZeusUI.__new__(mf.ZeusUI)
        ui._core_log_unsubscribe = None
        ui._post_ui = lambda fn: fn()          # синхронно, как в тестах трея
        ui.lines: list[str] = []
        ui._do_log = ui.lines.append           # «терминал» интерфейса
        return ui

    def test_audio_message_reaches_terminal(self):
        ui = self._ui()
        ui._on_core_log("[AUDIO] 🎤 Активный микрофон: «USB Mic»", 20)
        assert ui.lines == ["[AUDIO] 🎤 Активный микрофон: «USB Mic»"]

    def test_non_audio_message_is_ignored(self):
        ui = self._ui()
        ui._on_core_log("[LLM] ответ сгенерирован", 20)
        ui._on_core_log("Обычная служебная строка", 20)
        assert ui.lines == []

    def test_end_to_end_through_logging_service(self):
        """Реальный logging_service.log без моков доходит до терминала."""
        ui = self._ui()
        ui._subscribe_core_logs()
        try:
            assert callable(ui._core_log_unsubscribe)
            logging_service.log("[AUDIO] 🔊 Активный вывод: авто")
            assert any(line.startswith("[AUDIO]") for line in ui.lines)
        finally:
            ui._core_log_unsubscribe()
            ui._core_log_unsubscribe = None

    def test_double_subscribe_registers_subscriber_once(self):
        ui = self._ui()
        before = len(logging_service._subscribers)
        ui._subscribe_core_logs()
        ui._subscribe_core_logs()
        try:
            assert len(logging_service._subscribers) == before + 1
        finally:
            ui._core_log_unsubscribe()
            ui._core_log_unsubscribe = None
        assert len(logging_service._subscribers) == before
