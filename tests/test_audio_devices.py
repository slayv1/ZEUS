# -*- coding: utf-8 -*-
"""Юнит-тесты модуля аудиоустройств v1.4 (ТЗ §5).

Чистые функции без реального железа: классификация типа, группировка
гарнитур в пары по ContainerId, приоритетный выбор, настройки v1.4.
"""
from src.core.audio_devices import (
    AudioDevice,
    DeviceType,
    classify_device,
    pair_devices,
    pick_best_device,
)
from src.core.config import DEFAULT_SETTINGS, _migrate_old_settings


def _dev(name, kind="input", dtype=DeviceType.UNKNOWN, container=None, **kw):
    return AudioDevice(
        id=kw.pop("id", f"id-{name}"), name=name, kind=kind, dtype=dtype,
        container_id=container, **kw
    )


def test_classify_bluetooth_headset():
    assert classify_device("AirPods Pro Hands-Free", True) is DeviceType.BLUETOOTH_HEADSET
    assert classify_device("AirPods Pro Stereo", False) is DeviceType.BLUETOOTH_HEADPHONES
    assert classify_device("JBL Tune 520BT", True) is DeviceType.BLUETOOTH_HEADSET


def test_classify_bluetooth_transport_overrides_name():
    # COM говорит BTHENUM — значит BT, даже если имя без "bluetooth"
    assert classify_device("WH-1000XM5", True, enumerator="BTHENUM") is DeviceType.BLUETOOTH_HEADSET
    assert classify_device("WH-1000XM5", False, enumerator="BTHENUM") is DeviceType.BLUETOOTH_HEADPHONES


def test_classify_wired_headset_without_bt_transport():
    assert classify_device("Gaming Headset", True) is DeviceType.WIRED_HEADSET


def test_classify_usb_and_builtin_microphones():
    assert classify_device("Blue Yeti USB Microphone", True) is DeviceType.USB_MICROPHONE
    assert classify_device("Microphone Array (Intel Smart Sound)", True) is DeviceType.BUILTIN_MICROPHONE
    assert classify_device("Встроенный микрофон", True) is DeviceType.BUILTIN_MICROPHONE


def test_classify_builtin_speakers_and_unknown():
    assert classify_device("Динамики (Realtek Audio)", False) is DeviceType.BUILTIN_SPEAKERS
    assert classify_device("Digital Input (SPDIF)", True) is DeviceType.UNKNOWN


def test_classify_form_factor_with_builtin_name():
    # FormFactor=Microphone, но имя явно встроенное → builtin (ТЗ §6:
    # эвристика остаётся постоянным подстраховочным слоем)
    assert classify_device(
        "Microphone (Realtek Audio)", True, form_factor="Microphone"
    ) is DeviceType.BUILTIN_MICROPHONE


# ----------------------------------------------------------------------
# Пары гарнитур (ContainerId) и приоритетный выбор (ТЗ §2.3, §2.6)
# ----------------------------------------------------------------------
def test_pair_devices_groups_headset():
    cont = "11111111-2222-3333-4444-555555555555"
    devices = [
        _dev("AirPods Hands-Free", "input", DeviceType.BLUETOOTH_HEADSET, cont),
        _dev("AirPods Stereo", "output", DeviceType.BLUETOOTH_HEADPHONES, cont),
        _dev("Blue Yeti", "input", DeviceType.USB_MICROPHONE, None),
    ]
    paired = pair_devices(devices)
    assert paired[0].paired and paired[1].paired
    assert not paired[2].paired
    assert "микрофон и наушники" in paired[0].label


def test_pair_devices_same_kind_not_pair():
    cont = "aaaa-bbbb"
    devices = [
        _dev("Mic 1", "input", DeviceType.USB_MICROPHONE, cont),
        _dev("Mic 2", "input", DeviceType.USB_MICROPHONE, cont),
    ]
    assert not any(d.paired for d in pair_devices(devices))


def test_pick_best_prefers_headset_over_builtin():
    devices = [
        _dev("Built-in mic", "input", DeviceType.BUILTIN_MICROPHONE),
        _dev("USB Yeti", "input", DeviceType.USB_MICROPHONE),
    ]
    best = pick_best_device(devices, kind="input")
    assert best is not None and best.dtype is DeviceType.USB_MICROPHONE


def test_pick_best_respects_custom_priority():
    devices = [
        _dev("BT headset", "input", DeviceType.BLUETOOTH_HEADSET),
        _dev("USB Yeti", "input", DeviceType.USB_MICROPHONE),
    ]
    best = pick_best_device(
        devices, priority=["usb_microphone", "bluetooth_headset"], kind="input"
    )
    assert best is not None and best.dtype is DeviceType.USB_MICROPHONE


def test_pick_best_kind_filter_and_empty():
    output = [_dev("Speakers", "output", DeviceType.BUILTIN_SPEAKERS)]
    assert pick_best_device(output, kind="input") is None
    assert pick_best_device([], kind="input") is None


def test_pick_best_default_device_tiebreak():
    a = _dev("Yeti A", "input", DeviceType.USB_MICROPHONE, id="a")
    b = _dev("Yeti B", "input", DeviceType.USB_MICROPHONE, id="b", is_default=True)
    assert pick_best_device([a, b], kind="input").id == "b"


# ----------------------------------------------------------------------
# Настройки v1.4 (ТЗ §2.7)
# ----------------------------------------------------------------------
def test_default_settings_contain_device_keys():
    voice = DEFAULT_SETTINGS["voice_settings"]
    assert "input_device_id" in voice and "output_device_id" in voice
    assert voice["device_auto_switch"] is True
    assert isinstance(voice["device_priority"], list)


def test_migrate_v13_settings_keeps_new_keys():
    data = {
        "voice_settings": {"mic_index": 2, "input_device_id": "endpoint-guid"},
        "theme": "dark",
    }
    migrated = _migrate_old_settings(data)
    assert migrated["voice_settings"]["mic_index"] == 2
    assert migrated["voice_settings"]["input_device_id"] == "endpoint-guid"