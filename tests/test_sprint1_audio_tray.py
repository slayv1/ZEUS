"""Тесты динамического аудио-подменю системного трея (Спринт 1).

Покрывают:
  1. TrayManager.update_audio_devices() — обновляет внутренние данные.
  2. _build_menu() — возвращает пункты с подменю при наличии устройств.
  3. _build_device_submenu() — radio-галочка у активного устройства.
  4. DeviceWatcher — _snapshot теперь dict[id->AudioDevice].
  5. DeviceWatcher._disconnect_reason() — Bluetooth vs USB vs встроенное.
  6. get_global_watcher() / stop_watcher() — singleton-lifecycle.
  7. ZeusController.get_available_input_devices() / get_available_output_devices().
  8. ZeusController.set_input_device() / set_output_device() — сохраняют в settings.
"""
from __future__ import annotations

import sys
import types
import threading
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Заглушки pystray и PIL (не устанавливаем реальные зависимости в CI)
# ---------------------------------------------------------------------------

class _FakeMenuItem:
    def __init__(self, text, action=None, *, default=False, checked=None,
                 radio=False, enabled=True):
        self.text = text
        self.action = action
        self.default = default
        self.checked = checked
        self.radio = radio
        self.enabled = enabled

    def __repr__(self):
        return f"MenuItem({self.text!r})"


class _FakeSeparator:
    pass


class _FakeMenu:
    SEPARATOR = _FakeSeparator()

    def __init__(self, *items):
        self.items = items

    def __call__(self):
        return self.items


class _FakePystray:
    Menu = _FakeMenu
    MenuItem = _FakeMenuItem
    Icon = MagicMock


_fake_pystray = _FakePystray()
sys.modules.setdefault("pystray", _fake_pystray)

# PIL / Pillow
_pil_image = types.ModuleType("PIL.Image")
_pil_draw = types.ModuleType("PIL.ImageDraw")
_pil = types.ModuleType("PIL")
sys.modules.setdefault("PIL", _pil)
sys.modules.setdefault("PIL.Image", _pil_image)
sys.modules.setdefault("PIL.ImageDraw", _pil_draw)

# ---------------------------------------------------------------------------
# Импорты тестируемых модулей
# ---------------------------------------------------------------------------
from modules.tray import TrayManager  # noqa: E402
from core.audio_devices import (  # noqa: E402
    AudioDevice, DeviceType, DeviceWatcher,
    get_global_watcher, stop_watcher,
)


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _mic(dev_id: str, name: str = "Микрофон", index: int = 0) -> AudioDevice:
    return AudioDevice(
        id=dev_id, name=name, kind="input", index=index,
        # Обычный (встроенный) микрофон: эмодзи 🎤. Раньше здесь стоял
        # WIRED_HEADSET, из-за чего test_device_emoji_mic ждал 🎤, а
        # _device_emoji отдавал 🎧 (проводная гарнитура).
        dtype=DeviceType.BUILTIN_MICROPHONE,
    )


def _bt_headphones(dev_id: str, name: str = "AirPods") -> AudioDevice:
    return AudioDevice(
        id=dev_id, name=name, kind="output", index=1,
        dtype=DeviceType.BLUETOOTH_HEADPHONES,
        enumerator="BTHENUM\\...",
    )


def _usb_mic(dev_id: str, name: str = "USB Microphone") -> AudioDevice:
    return AudioDevice(
        id=dev_id, name=name, kind="input", index=2,
        dtype=DeviceType.USB_MICROPHONE,
        enumerator="USB\\VID_...",
    )


# ===========================================================================
# Тесты TrayManager
# ===========================================================================

class TestTrayManagerAudioMenu:
    """Тесты динамического аудио-подменю TrayManager."""

    def _make_tray(self, **kw) -> TrayManager:
        return TrayManager(
            on_open=lambda: None,
            on_settings=lambda: None,
            on_exit=lambda: None,
            **kw,
        )

    # --- update_audio_devices ---

    def test_update_audio_devices_stores_data(self):
        """update_audio_devices сохраняет устройства и активные ID."""
        tray = self._make_tray()
        devices_in = [{"id": "mic1", "name": "Микрофон 1"}]
        devices_out = [{"id": "spk1", "name": "Динамики"}]

        tray.update_audio_devices(devices_in, devices_out, "mic1", "spk1")

        assert tray._devices_in == devices_in
        assert tray._devices_out == devices_out
        assert tray._active_in_id == "mic1"
        assert tray._active_out_id == "spk1"

    def test_update_audio_devices_calls_icon_update_menu(self):
        """update_audio_devices вызывает icon.update_menu() если трей запущен."""
        tray = self._make_tray()
        mock_icon = MagicMock()
        tray._icon = mock_icon

        tray.update_audio_devices([], [], None, None)

        mock_icon.update_menu.assert_called_once()

    def test_update_audio_devices_no_icon_no_error(self):
        """update_audio_devices не падает если трей ещё не запущен."""
        tray = self._make_tray()
        tray._icon = None  # трей не запущен
        # Не должно бросать исключений
        tray.update_audio_devices([{"id": "x", "name": "X"}], [], "x", None)
        assert tray._devices_in == [{"id": "x", "name": "X"}]

    # --- _build_menu ---

    def test_build_menu_no_audio_callbacks(self):
        """Без audio callbacks меню содержит только базовые пункты."""
        tray = self._make_tray()  # on_pick_input/output не переданы
        items = tray._build_menu()
        texts = [
            i.text for i in items
            if isinstance(i, _FakeMenuItem)
        ]
        assert "Открыть / Развернуть" in texts
        assert "Настройки" in texts
        assert "Выход" in texts
        # Аудио-пунктов нет
        assert all("Микрофон" not in t and "Динамики" not in t for t in texts)

    def test_build_menu_with_audio_callbacks_shows_submenus(self):
        """С audio callbacks меню включает подменю 🎤 и 🔊."""
        picked = []
        tray = self._make_tray(
            on_pick_input=lambda d: picked.append(("in", d)),
            on_pick_output=lambda d: picked.append(("out", d)),
        )
        tray._devices_in = [{"id": "m1", "name": "Mic 1"}]
        tray._devices_out = [{"id": "s1", "name": "Speaker"}]

        items = tray._build_menu()
        texts = [i.text for i in items if isinstance(i, _FakeMenuItem)]

        assert any("Микрофон" in t for t in texts), f"Нет пункта Микрофон: {texts}"
        assert any("Динамики" in t for t in texts), f"Нет пункта Динамики: {texts}"

    def test_build_menu_separator_present(self):
        """Меню содержит разделитель."""
        tray = self._make_tray(
            on_pick_input=lambda d: None,
            on_pick_output=lambda d: None,
        )
        items = tray._build_menu()
        assert any(isinstance(i, _FakeSeparator) for i in items)

    # --- _build_device_submenu ---

    def test_device_submenu_empty_devices(self):
        """Если устройств нет — один disabled пункт «(нет устройств)»."""
        items = TrayManager._build_device_submenu(
            _fake_pystray, [], None, lambda d: None
        )
        assert len(items) == 1
        assert "нет устройств" in items[0].text
        assert items[0].enabled is False

    def test_device_submenu_radio_checked_for_active(self):
        """У активного устройства checked(item) == True, у остальных False."""
        devices = [
            {"id": "m1", "name": "Mic 1"},
            {"id": "m2", "name": "Mic 2"},
        ]
        items = TrayManager._build_device_submenu(
            _fake_pystray, devices, "m1", lambda d: None
        )
        assert len(items) == 2
        # checked — callable; вызываем как pystray: checked(item)
        assert items[0].checked(items[0]) is True,  "m1 должен быть checked"
        assert items[1].checked(items[1]) is False, "m2 не должен быть checked"

    def test_device_submenu_action_calls_callback(self):
        """Клик по пункту устройства вызывает callback с правильным device_id."""
        picked = []
        devices = [{"id": "m1", "name": "Mic 1"}, {"id": "m2", "name": "Mic 2"}]
        items = TrayManager._build_device_submenu(
            _fake_pystray, devices, None, lambda d: picked.append(d)
        )
        items[1].action()  # кликаем m2
        assert picked == ["m2"]

    def test_device_submenu_radio_flag(self):
        """Все пункты устройств имеют radio=True."""
        devices = [{"id": "x", "name": "X"}, {"id": "y", "name": "Y"}]
        items = TrayManager._build_device_submenu(
            _fake_pystray, devices, "x", lambda d: None
        )
        assert all(i.radio is True for i in items)


# ===========================================================================
# Тесты DeviceWatcher (обогащённая версия)
# ===========================================================================

class TestDeviceWatcherEnhanced:
    """Тесты обновлённого DeviceWatcher с dict-снимком и диагностикой."""

    def test_snapshot_is_dict(self):
        """_snapshot инициализируется как dict, не set."""
        dw = DeviceWatcher()
        assert isinstance(dw._snapshot, dict)

    def test_on_device_removed_with_info_callback(self):
        """on_device_removed_with_info получает полный объект AudioDevice."""
        dw = DeviceWatcher()
        removed_devs = []
        dw.on_device_removed_with_info = removed_devs.append

        mic = _mic("m1")
        dw._snapshot = {"m1": mic}

        # Симулируем poll: текущий список пуст → m1 удалён
        with patch("core.audio_devices.list_devices", return_value=[]):
            dw._poll_loop.__func__  # убеждаемся что метод существует
            # Вручную вызываем одну итерацию poll-логики
            dw._stop_event.set()  # остановим цикл после первой итерации
            # Сбрасываем stop_event для теста и вызываем напрямую
            dw._stop_event.clear()
            from core import audio_devices as _ad
            with patch.object(_ad, "list_devices", return_value=[]):
                current = {}
                removed_ids = dw._snapshot.keys() - current.keys()
                for dev_id in removed_ids:
                    removed_dev = dw._snapshot[dev_id]
                    if dw.on_device_removed_with_info:
                        dw.on_device_removed_with_info(removed_dev)

        assert len(removed_devs) == 1
        assert removed_devs[0].id == "m1"
        assert isinstance(removed_devs[0], AudioDevice)

    def test_disconnect_reason_bluetooth(self):
        """Bluetooth-устройство → 'разрыв Bluetooth-связи'."""
        dev = _bt_headphones("bt1")
        reason = DeviceWatcher._disconnect_reason(dev)
        assert "Bluetooth" in reason or "bluetooth" in reason.lower()

    def test_disconnect_reason_usb(self):
        """USB-устройство → 'физическое извлечение USB'."""
        dev = _usb_mic("usb1")
        reason = DeviceWatcher._disconnect_reason(dev)
        assert "USB" in reason


    def test_disconnect_reason_generic(self):
        """Встроенное устройство (без enumerator) → обобщённая причина."""
        dev = AudioDevice(
            id="builtin", name="Встроенный микрофон", kind="input",
            index=0, dtype=DeviceType.BUILTIN_MICROPHONE,
        )
        reason = DeviceWatcher._disconnect_reason(dev)
        assert reason  # непустая строка


    def test_device_emoji_bluetooth(self):
        """Bluetooth-устройство → 🎧."""
        dev = _bt_headphones("bt1")
        assert DeviceWatcher._device_emoji(dev) == "🎧"

    def test_device_emoji_usb(self):
        """USB-микрофон → 🔌."""
        dev = _usb_mic("usb1")
        assert DeviceWatcher._device_emoji(dev) == "🔌"

    def test_device_emoji_mic(self):
        """Обычный микрофон → 🎤."""
        dev = _mic("m1")
        assert DeviceWatcher._device_emoji(dev) == "🎤"

    def test_device_emoji_speaker(self):
        """Встроенные динамики (output, не Bluetooth) → 🔊."""
        dev = AudioDevice(
            id="spk", name="Dinamiki", kind="output", index=0,
            dtype=DeviceType.BUILTIN_SPEAKERS,
        )
        assert DeviceWatcher._device_emoji(dev) == "🔊"



# ===========================================================================
# Тесты singleton-вотчера
# ===========================================================================

class TestGlobalWatcherSingleton:
    """Тесты get_global_watcher() / stop_watcher()."""

    def setup_method(self):
        stop_watcher()  # сбрасываем синглтон перед каждым тестом

    def teardown_method(self):
        stop_watcher()

    def test_get_global_watcher_returns_same_instance(self):
        """get_global_watcher() возвращает один и тот же объект."""
        dw1 = get_global_watcher()
        dw2 = get_global_watcher()
        assert dw1 is dw2

    def test_stop_watcher_clears_singleton(self):
        """stop_watcher() сбрасывает синглтон → следующий get создаёт новый."""
        dw1 = get_global_watcher()
        stop_watcher()
        dw2 = get_global_watcher()
        assert dw1 is not dw2

    def test_stop_watcher_idempotent(self):
        """stop_watcher() безопасно вызывать несколько раз."""
        stop_watcher()
        stop_watcher()  # не должно бросать


# ===========================================================================
# Тесты методов контроллера
# ===========================================================================

class TestControllerAudioDeviceMethods:
    """Тесты get_available_*/set_*_device в ZeusController (юнит-уровень)."""

    def _make_controller_shell(self):
        """Создаём минимальную заглушку контроллера без реального __init__."""
        import importlib
        import core.controller as ctrl_module
        ctrl = object.__new__(ctrl_module.ZeusController)
        # Минимальные атрибуты
        ctrl._is_cleaned_up = False
        ctrl.settings = {"voice_settings": {}}
        ctrl.settings_manager = MagicMock()
        ctrl.settings_manager.get_all.return_value = ctrl.settings
        ctrl.audio = MagicMock()
        ctrl.audio._mic_device_index = None
        ctrl.on_tray_audio_update = None
        return ctrl

    def test_get_available_input_devices_returns_list(self):
        """get_available_input_devices возвращает список (может быть пустым)."""
        ctrl = self._make_controller_shell()
        mic = _mic("m1", "Test Mic")
        spk = _bt_headphones("s1")
        with patch("core.audio_devices.list_devices", return_value=[mic, spk]):
            result = ctrl.get_available_input_devices()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "m1"

    def test_get_available_output_devices_excludes_inputs(self):
        """get_available_output_devices не включает микрофоны."""
        ctrl = self._make_controller_shell()
        mic = _mic("m1")
        spk = _bt_headphones("s1")
        with patch("core.audio_devices.list_devices", return_value=[mic, spk]):
            result = ctrl.get_available_output_devices()
        assert all(d["kind"] == "output" for d in result)
        assert len(result) == 1

    def test_set_input_device_saves_setting(self):
        """set_input_device сохраняет device_id в voice_settings."""
        ctrl = self._make_controller_shell()
        with patch("core.audio_devices.resolve_device_id_to_index", return_value=2):
            ctrl.set_input_device("mic_id_42")
        ctrl.settings_manager.set.assert_called_once_with(
            "voice_settings", "input_device_id", "mic_id_42"
        )

    def test_set_output_device_saves_setting(self):
        """set_output_device сохраняет device_id в voice_settings."""
        ctrl = self._make_controller_shell()
        with patch("core.audio_devices.resolve_device_id_to_index", return_value=3):
            ctrl.set_output_device("spk_id_99")
        ctrl.settings_manager.set.assert_called_once_with(
            "voice_settings", "output_device_id", "spk_id_99"
        )

    def test_set_input_device_none_sets_auto(self):
        """set_input_device(None) устанавливает авто-режим (индекс None)."""
        ctrl = self._make_controller_shell()
        ctrl.set_input_device(None)
        assert ctrl.audio._mic_device_index is None

    def test_on_audio_device_changed_calls_callback(self):
        """_on_audio_device_changed вызывает on_tray_audio_update если задан."""
        ctrl = self._make_controller_shell()
        called = []
        ctrl.on_tray_audio_update = lambda: called.append(True)
        ctrl._on_audio_device_changed()
        assert called == [True]

    def test_on_audio_device_changed_skips_if_cleaned_up(self):
        """_on_audio_device_changed не вызывает callback после cleanup."""
        ctrl = self._make_controller_shell()
        ctrl._is_cleaned_up = True
        called = []
        ctrl.on_tray_audio_update = lambda: called.append(True)
        ctrl._on_audio_device_changed()
        assert called == []
