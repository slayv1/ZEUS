# -*- coding: utf-8 -*-
"""Управление аудиоустройствами Windows для Зевса (v1.4, Модуль A).

Различает типы устройств (BT-гарнитура, проводная гарнитура, USB-микрофон,
встроенный микрофон/динамики), группирует микрофон+наушники одного физического
устройства по ContainerId, даёт стабильный выбор по endpoint-ID (а не по
нестабильному числовому индексу sounddevice) и следит за hot-plug.

Слои (по ТЗ §2.2, §2.9):
  1. pycaw/comtypes (Core Audio) — форм-фактор, транспорт (BTHENUM), ContainerId.
  2. Эвристика по имени устройства — обязательный подстраховочный слой.
  3. sounddevice — только числовые индексы для потоков записи/вывода.

Все COM-операции выполняются в отдельном потоке; любые сбои — тихий fallback
с логированием, приложение не падает.
"""
from __future__ import annotations

import ctypes
import re
import threading
from dataclasses import dataclass, field
from enum import Enum

# ВАЖНО: только абсолютный импорт без fallback на "from src.core...".
# Fallback-ветка попадала в анализ PyInstaller и добавляла в PYZ вторую копию
# модулей под именем "src", из-за чего frozen-exe падал на старте
# (ImportError: cannot import name 'config' from 'core' — см. main.py).
from core.logging_service import log


class DeviceType(str, Enum):
    """Тип аудиоустройства (показывается в UI с иконкой)."""

    BLUETOOTH_HEADSET = "bluetooth_headset"        # BT-гарнитура (HFP/HSP)
    BLUETOOTH_HEADPHONES = "bluetooth_headphones"  # BT-наушники без мик (A2DP)
    WIRED_HEADSET = "wired_headset"                # проводная гарнитура (3.5 мм/USB)
    USB_MICROPHONE = "usb_microphone"              # отдельный USB/XLR-микрофон
    BUILTIN_MICROPHONE = "builtin_microphone"      # встроенный мик ноутбука
    BUILTIN_SPEAKERS = "builtin_speakers"          # встроенные динамики
    UNKNOWN = "unknown"


DEFAULT_DEVICE_PRIORITY: list[str] = [
    DeviceType.BLUETOOTH_HEADSET.value,
    DeviceType.WIRED_HEADSET.value,
    DeviceType.USB_MICROPHONE.value,
    DeviceType.BUILTIN_MICROPHONE.value,
    DeviceType.BLUETOOTH_HEADPHONES.value,
    DeviceType.BUILTIN_SPEAKERS.value,
    DeviceType.UNKNOWN.value,
]


@dataclass
class AudioDevice:
    """Логическое аудиоустройство (endpoint Core Audio или fallback-запись).

    id — стабильный идентификатор (endpoint GUID строкой; в fallback —
    синтетический ``sd:<index>``), index — рантайм-кэш индекса sounddevice.
    """

    id: str
    name: str
    kind: str                      # "input" | "output"
    dtype: DeviceType = DeviceType.UNKNOWN
    container_id: str | None = None  # GUID физического устройства (для пар)
    index: int | None = None         # рантайм-индекс sounddevice (нестабилен!)
    max_input_channels: int = 0
    max_output_channels: int = 0
    is_default: bool = False
    form_factor: str | None = None
    enumerator: str | None = None
    paired: bool = field(default=False, compare=False)  # есть вторая половина (мик+наушники)

    @property
    def is_input(self) -> bool:
        return self.kind == "input"

    @property
    def label(self) -> str:
        """Человекочитаемая подпись для UI (ТЗ §2.3: пара = одна карточка)."""
        base = self.name
        if self.paired and self.dtype in (
            DeviceType.BLUETOOTH_HEADSET, DeviceType.WIRED_HEADSET,
        ):
            return f"{base} — микрофон и наушники"
        return base

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "type": self.dtype.value,
            "paired": self.paired,
            "index": self.index,
            "is_default": self.is_default,
        }


def _guess_from_name(name: str, is_input: bool) -> DeviceType:
    """Классифицирует устройство ТОЛЬКО по имени (fallback-слой)."""
    if not name:
        return DeviceType.UNKNOWN
    low = name.lower()
    if re.search(r"bluetooth|bt[- ]?audio|\w?bt\b|airpods|galaxy buds|buds\d?|freebuds|wh-|wf-", low):
        return DeviceType.BLUETOOTH_HEADSET if is_input else DeviceType.BLUETOOTH_HEADPHONES
    if re.search(r"headset|гарнитур|hands[- ]?free", low):
        return DeviceType.WIRED_HEADSET
    if is_input:
        if re.search(r"usb|yeti|snowball|quadcast", low):
            return DeviceType.USB_MICROPHONE
        if re.search(r"microphone array|встроенн|built[- ]?in|integrated|realtek", low):
            return DeviceType.BUILTIN_MICROPHONE
        if "microphone" in low or "микрофон" in low:
            return DeviceType.USB_MICROPHONE  # неопознанный отдельный микрофон
    else:
        if re.search(r"headphone|наушник", low):
            return DeviceType.WIRED_HEADSET
        if re.search(r"встроенн|built[- ]?in|динамик|speakers?\s*\(|realtek", low):
            return DeviceType.BUILTIN_SPEAKERS
    return DeviceType.UNKNOWN


# Форм-факторы Windows (PKEY_AudioEndpoint_FormFactor) → тип.
_FORM_FACTOR_MAP = {
    "Headset": DeviceType.WIRED_HEADSET,
    "HeadsetMicrophone": DeviceType.WIRED_HEADSET,
    "Headphones": DeviceType.WIRED_HEADSET,
    "Microphone": DeviceType.USB_MICROPHONE,
    "DesktopMicrophone": DeviceType.USB_MICROPHONE,
    "PersonalMicrophone": DeviceType.USB_MICROPHONE,
    "IntegratedMicrophoneArray": DeviceType.BUILTIN_MICROPHONE,
    "UnknownDigitalMicrophone": DeviceType.BUILTIN_MICROPHONE,
    "Speakers": DeviceType.BUILTIN_SPEAKERS,
    "IntegratedSpeakers": DeviceType.BUILTIN_SPEAKERS,
    "MonitorSpeakers": DeviceType.BUILTIN_SPEAKERS,
}


def classify_device(
    name: str,
    is_input: bool,
    form_factor: str | None = None,
    enumerator: str | None = None,
) -> DeviceType:
    """Определяет тип устройства: сначала COM-данные, затем эвристика имени.

    enumerator — PKEY_Device_EnumeratorName: 'BTHENUM'/'BTHLEDevice'/'BTHHFENUM'
    означают Bluetooth-транспорт (classic/BLE/HFP).
    """
    enum_bt = bool(enumerator) and enumerator.upper().startswith(("BTHENUM", "BTHLE", "BTHHF"))
    dtype = _guess_from_name(name, is_input)

    if form_factor:
        mapped = _FORM_FACTOR_MAP.get(form_factor)
        # Form-factor не «понижает» явно встроенные устройства: имя вида
        # «Встроенный/Realtek …» — надёжный признак builtin (ТЗ §6).
        builtin = dtype in (DeviceType.BUILTIN_MICROPHONE, DeviceType.BUILTIN_SPEAKERS)
        if mapped is not None and not (builtin and mapped is DeviceType.USB_MICROPHONE):
            if not builtin:
                dtype = mapped

    # Bluetooth-транспорт переклассифицирует проводные типы.
    if enum_bt:
        if is_input:
            return DeviceType.BLUETOOTH_HEADSET
        if dtype is DeviceType.BLUETOOTH_HEADSET:
            return DeviceType.BLUETOOTH_HEADSET
        return DeviceType.BLUETOOTH_HEADPHONES

    # Form-factor «Headset» без BT-транспорта — проводная гарнитура.
    # (если BT-тип пришёл из имени — имя с маркером BT главнее)
    if (
        dtype is DeviceType.BLUETOOTH_HEADSET
        and not enum_bt
        and form_factor == "Headset"
    ):
        return DeviceType.WIRED_HEADSET
    return dtype


def pair_devices(devices: list[AudioDevice]) -> list[AudioDevice]:
    """Группирует endpoint'ы по ContainerId (мик+наушники одного устройства).

    Пары (ТЗ §2.3) помечаются paired=True; у одиночных устройств флаг False.
    Возвращает новый список, вход не мутируется.
    """
    by_container: dict[str, list[AudioDevice]] = {}
    for dev in devices:
        if dev.container_id:
            by_container.setdefault(dev.container_id.lower(), []).append(dev)

    result: list[AudioDevice] = []
    for dev in devices:
        group = by_container.get(dev.container_id.lower(), []) if dev.container_id else []
        has_pair = (
            dev.container_id is not None
            and len(group) >= 2
            and any(other.kind != dev.kind for other in group)
        )
        copy = AudioDevice(**{k: v for k, v in dev.__dict__.items() if k != "paired"})
        copy.paired = has_pair
        result.append(copy)
    return result


def pick_best_device(
    devices: list[AudioDevice],
    priority: list[str] | None = None,
    kind: str = "input",
) -> AudioDevice | None:
    """Возвращает лучшее устройство направления по приоритету типов (ТЗ §2.6).

    priority — значения DeviceType от предпочтительного к крайнему.
    При равном приоритете предпочитается системное устройство по умолчанию.
    """
    order = list(priority or DEFAULT_DEVICE_PRIORITY)
    candidates = [d for d in devices if d.kind == kind]
    if not candidates:
        return None
    rank = {t: i for i, t in enumerate(order)}
    candidates.sort(key=lambda d: (rank.get(d.dtype.value, len(order) + 1), 0 if d.is_default else 1))
    return candidates[0]


# ----------------------------------------------------------------------
# Живой список устройств: Core Audio (pycaw) + fallback sounddevice
# ----------------------------------------------------------------------

# EndpointFormFactor (Windows SDK) — int → читаемое имя.
_FORM_FACTOR_NAMES = {
    0: "RemoteNetworkDevice", 1: "Speakers", 2: "LineLevel", 3: "Headphones",
    4: "Microphone", 5: "Headset", 6: "Handset", 7: "UnknownDigitalPassthrough",
    8: "SPDIF", 9: "DigitalAudioDisplayDevice", 10: "UnknownFormFactor",
}

# PROPERTYKEY (fmtid, pid) нужных свойств устройства.
_PKEY_NAME = ("A45C254E-DF1C-4EFD-8020-67D146A850E0", 14)        # DeviceFriendlyName
# PKEY_AudioEndpoint_FormFactor — fmtid из mmdeviceapi.h (проверено на живой
# машине probe_v2: {1DA9D8C0-...} в endpoint store НЕ существует, {1DA5D803-...}
# возвращает VT_UI4: Headphones/Headset/Speakers).
_PKEY_FORMAT = ("1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E", 0)       # FormFactor
_PKEY_ENUMERATOR = ("A45C254E-DF1C-4EFD-8020-67D146A850E0", 24)  # EnumeratorName
# GUID проверен PoC (poc/audio_probe/device_probe.py): именно он кладётся
# в endpoint store на Windows 11 (не «8C7ED206-3F8A-4847-...», его в store нет).
_PKEY_CONTAINER = ("8C7ED206-3F8A-4827-B3AB-AE9E1FAEFC6C", 2)    # ContainerId

# VARTYPE-константы PROPVARIANT (установленная версия pycaw не даёт `.value`).
_VT_EMPTY, _VT_I4, _VT_UI4 = 0, 3, 19
_VT_LPWSTR, _VT_BLOB, _VT_CLSID = 31, 65, 72


def _decode_propvar(pv):
    """PROPVARIANT → python-значение (декодирование union вручную)."""
    vt = pv.vt
    if vt == _VT_LPWSTR:
        return pv.union.pwszVal
    addr = ctypes.addressof(pv.union)
    if vt == _VT_I4:
        return ctypes.cast(addr, ctypes.POINTER(ctypes.c_long)).contents.value
    if vt == _VT_UI4:
        return ctypes.cast(addr, ctypes.POINTER(ctypes.c_ulong)).contents.value
    if vt == _VT_CLSID:
        # VT_CLSID хранит УКАЗАТЕЛЬ на GUID (CoTaskMemAlloc), а не сам GUID.
        # Чтение union.puuid «как значения» давало байты указателя — мусорные
        # GUID вида {57A521A0-01F6-0000-...}, из-за чего пары гарнитур
        # (общий ContainerId у render+capture) не собирались.
        ptr = ctypes.cast(addr, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if not ptr:
            return None
        from comtypes import GUID as _GUID
        return str(ctypes.cast(ptr, ctypes.POINTER(_GUID)).contents)
    if vt == _VT_BLOB:
        raw = ctypes.string_at(addr, 16)
        cb = int.from_bytes(raw[0:4], "little")
        ptr = int.from_bytes(raw[8:16], "little")
        return ctypes.string_at(ptr, cb) if cb else b""
    return None


def _read_prop(store, fmtid: str, pid: int):
    """Читает одно свойство из property store (None при любой ошибке)."""
    try:
        from comtypes import GUID
        from pycaw.pycaw import PROPERTYKEY
        key = PROPERTYKEY()
        # comtypes.GUID() парсит GUID только в фигурных скобках (CLSIDFromString).
        key.fmtid = GUID("{" + fmtid.upper() + "}")
        key.pid = pid
        pv = store.GetValue(key)
        try:
            return _decode_propvar(pv)
        finally:
            try:  # освобождаем память PROPVARIANT
                pv.clear()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return None


def _get_device_properties(dev) -> dict:
    """Имя, форм-фактор, транспорт (enumerator) и ContainerId устройства."""
    props: dict = {}
    try:
        store = dev.OpenPropertyStore(0)  # STGM_READ
    except Exception:  # noqa: BLE001
        return props

    # Имя — PKEY_Device_FriendlyName (константы нет в установленной версии
    # pycaw.constants, читаем по проверенному PoC ключу).
    name_value = _read_prop(store, *_PKEY_NAME)
    props["name"] = str(name_value) if name_value else "?"

    ff = _read_prop(store, *_PKEY_FORMAT)
    if ff is not None:
        try:
            props["form_factor"] = _FORM_FACTOR_NAMES.get(int(ff))
        except Exception:  # noqa: BLE001
            pass

    enum_name = _read_prop(store, *_PKEY_ENUMERATOR)
    if enum_name:
        props["enumerator"] = str(enum_name)

    container = _read_prop(store, *_PKEY_CONTAINER)
    if container is not None:
        try:
            if isinstance(container, bytes) and len(container) == 16:
                import uuid
                props["container_id"] = str(uuid.UUID(bytes_le=container))
            elif container is not None:
                props["container_id"] = str(container)
        except Exception:  # noqa: BLE001
            pass
    return props


def _get_endpoint_id(dev) -> str:
    """Стабильный строковый ID endpoint'а (IMMDevice::GetId)."""
    try:
        dev_id = dev.GetId()
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(dev_id, bytes):
        return dev_id.decode("utf-16-le").rstrip("\x00")
    return str(dev_id)


def _list_via_pycaw() -> list[AudioDevice] | None:
    """Полный список endpoint'ов через Core Audio. None — pycaw/COM недоступны.

    Вызывается в фоновом потоке; COM инициализируется в текущем (MTA).
    """
    try:
        import comtypes
        from comtypes import CoInitializeEx
        from pycaw.pycaw import DEVICE_STATE, EDataFlow, AudioUtilities
    except Exception as exc:  # noqa: BLE001 — обязательный fallback (ТЗ §2.9)
        log(f"[AudioDevices] pycaw/COM недоступны ({exc}) — fallback на sounddevice")
        return None

    try:
        CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except Exception:  # noqa: BLE001 — уже инициализирован в этом потоке
        pass

    try:
        # Проверено PoC: в установленной версии pycaw (20251023)
        # CLSID_MMDeviceEnumerator не экспортируется из pycaw.pycaw —
        # CoCreateInstance падает. AudioUtilities.GetDeviceEnumerator() работает.
        enumerator = AudioUtilities.GetDeviceEnumerator()
    except Exception as exc:  # noqa: BLE001
        log(f"[AudioDevices] Не удалось создать MMDeviceEnumerator: {exc}")
        return None

    devices: list[AudioDevice] = []
    for flow, kind in ((EDataFlow.eCapture, "input"), (EDataFlow.eRender, "output")):
        try:
            # COM-обёртка этой версии принимает сырые int (enum.value).
            collection = enumerator.EnumAudioEndpoints(
                flow.value, DEVICE_STATE.ACTIVE.value)
            count = int(collection.GetCount())
        except Exception as exc:  # noqa: BLE001
            log(f"[AudioDevices] EnumAudioEndpoints({kind}) не удался: {exc}")
            continue
        default_id = ""
        try:
            default_dev = enumerator.GetDefaultAudioEndpoint(flow.value, 0)
            default_id = _get_endpoint_id(default_dev)
        except Exception:  # noqa: BLE001
            pass
        for i in range(count):
            try:
                dev = collection.Item(i)
            except Exception:  # noqa: BLE001
                continue
            props = _get_device_properties(dev)
            dev_id = _get_endpoint_id(dev)
            is_input = kind == "input"
            dtype = classify_device(
                props["name"], is_input,
                form_factor=props.get("form_factor"),
                enumerator=props.get("enumerator"),
            )
            devices.append(AudioDevice(
                id=dev_id,
                name=props["name"],
                kind=kind,
                dtype=dtype,
                container_id=props.get("container_id"),
                is_default=(dev_id == default_id),
                form_factor=props.get("form_factor"),
                enumerator=props.get("enumerator"),
            ))
    return devices or None


def _list_via_sounddevice() -> list[AudioDevice]:
    """Fallback: список из sounddevice + эвристика по имени (без COM)."""
    devices: list[AudioDevice] = []
    try:
        import sounddevice as sd
        raw = list(sd.query_devices())
        default_in, default_out = sd.default.device
    except Exception as exc:  # noqa: BLE001 — даже sounddevice недоступен
        log(f"[AudioDevices] sounddevice недоступен: {exc}")
        return devices
    for i, info in enumerate(raw):
        name = str(info.get("name", "?"))
        max_in = int(info.get("max_input_channels", 0) or 0)
        max_out = int(info.get("max_output_channels", 0) or 0)
        if max_in > 0:
            devices.append(AudioDevice(
                id=f"sd:{i}:in", name=name, kind="input",
                dtype=_guess_from_name(name, True), index=i,
                max_input_channels=max_in, max_output_channels=max_out,
                is_default=(i == default_in),
            ))
        if max_out > 0:
            devices.append(AudioDevice(
                id=f"sd:{i}:out", name=name, kind="output",
                dtype=_guess_from_name(name, False), index=i,
                max_input_channels=max_in, max_output_channels=max_out,
                is_default=(i == default_out),
            ))
    return devices


def _sync_sounddevice_indices(devices: list[AudioDevice]) -> None:
    """Проставляет рантайм-индексы sounddevice по совпадению имени и каналов."""
    try:
        import sounddevice as sd
        raw = list(sd.query_devices())
    except Exception:  # noqa: BLE001
        return
    for dev in devices:
        for i, info in enumerate(raw):
            if str(info.get("name", "")) != dev.name:
                continue
            if dev.kind == "input" and int(info.get("max_input_channels", 0) or 0) > 0:
                dev.index = i
                break
            if dev.kind == "output" and int(info.get("max_output_channels", 0) or 0) > 0:
                dev.index = i
                break


def list_devices() -> list[AudioDevice]:
    """Полный список устройств: Core Audio, при недоступности — sounddevice.

    Результат группируется по ContainerId (paired-пометки гарнитур, ТЗ §2.3).
    """
    devices = _list_via_pycaw()
    if not devices:
        return pair_devices(_list_via_sounddevice())
    _sync_sounddevice_indices(devices)
    return pair_devices(devices)


def match_index_for_device(device: AudioDevice) -> int | None:
    """Актуальный индекс sounddevice для устройства (свежий запрос)."""
    if device.index is not None:
        return device.index
    try:
        import sounddevice as sd
        raw = list(sd.query_devices())
        for i, info in enumerate(raw):
            if str(info.get("name", "")) != device.name:
                continue
            if device.kind == "input" and int(info.get("max_input_channels", 0) or 0) > 0:
                return i
            if device.kind == "output" and int(info.get("max_output_channels", 0) or 0) > 0:
                return i
    except Exception:  # noqa: BLE001
        pass
    return None


def resolve_device_id_to_index(device_id: str | None, kind: str = "input") -> int | None:
    """Стабильный device_id → числовой индекс sounddevice (ТЗ §2.7).

    Индексы sounddevice переезжают при hot-plug, поэтому в конфиге их
    хранить нельзя — резолвим по endpoint-ID в момент запуска/подключения.
    """
    if not device_id:
        return None
    for dev in list_devices():
        if dev.id == device_id and dev.kind == kind:
            return match_index_for_device(dev)
    # Fallback для синтетических ID fallback-слоя ("sd:<index>:<kind>")
    m = re.fullmatch(r"sd:(\d+):(in|out)", device_id)
    if m:
        return int(m.group(1))
    return None


class DeviceWatcher:
    """Наблюдатель hot-plug: подключение/отключение устройств (ТЗ §2.4).

    Слой 1 — COM-подписка IMMNotificationClient (зарезервирована: comtypes
    требует кодогенерацию интерфейса; при её недоступности включается слой 2).
    Слой 2 — поллинг каждые POLL_INTERVAL секунд: сравнение множеств device id
    и системных дефолтов. Колбэки вызываются в фоновом потоке, исключения
    изолированы и логируются.
    """

    POLL_INTERVAL = 3.0

    def __init__(self):
        self.on_device_added = None             # fn(AudioDevice)
        self.on_device_removed = None           # fn(device_id: str)
        self.on_device_removed_with_info = None # fn(AudioDevice) — с полным объектом
        self.on_default_changed = None          # fn(kind: str, default_id: str)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Снимок хранит полные объекты (не только id) — для диагностики типа
        # отключённого устройства (Bluetooth / USB / встроенное).
        self._snapshot: dict[str, "AudioDevice"] = {}  # id -> AudioDevice
        self._defaults: dict[str, str] = {}

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        try:
            self._snapshot = {d.id: d for d in list_devices()}
        except Exception:  # noqa: BLE001
            self._snapshot = {}
        self._defaults = self._current_defaults()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="zeus-device-watcher"
        )
        self._thread.start()
        # Диагностика (Спринт 1 §2.2): подтверждение, что hot-plug наблюдатель
        # реально работает — строка видна в терминале интерфейса и в zeus.log.
        ins = sum(1 for d in self._snapshot.values() if d.is_input)
        outs = len(self._snapshot) - ins
        log(f"[AUDIO] 👂 Наблюдатель устройств запущен: {ins} вход(ов), "
            f"{outs} выход(ов), опрос каждые {self.POLL_INTERVAL:.0f} с")
        return True

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    @staticmethod
    def _current_defaults() -> dict[str, str]:
        defaults: dict[str, str] = {}
        try:
            import sounddevice as sd
            d_in, d_out = sd.default.device
            if d_in is not None and d_in >= 0:
                defaults["input"] = f"sd:{d_in}"
            if d_out is not None and d_out >= 0:
                defaults["output"] = f"sd:{d_out}"
        except Exception:  # noqa: BLE001
            pass
        return defaults

    @staticmethod
    def _disconnect_reason(dev: "AudioDevice") -> str:
        """Человекочитаемая причина отключения на основе типа устройства.

        Дифференциация (Спринт 1 §2.2): разрыв Bluetooth-линка (BTHENUM),
        физическое извлечение USB-устройства, отсоединение кабеля
        проводной гарнитуры (jack), иначе — общее отключение.
        """
        enumerator = (dev.enumerator or "").upper()
        dtype = dev.dtype
        if "BTHENUM" in enumerator or dtype in (
            DeviceType.BLUETOOTH_HEADSET,
            DeviceType.BLUETOOTH_HEADPHONES,
        ):
            return "разрыв Bluetooth-связи"
        if "USB" in enumerator:
            return "физическое извлечение USB"
        if dtype == DeviceType.WIRED_HEADSET:
            return "отсоединение кабеля гарнитуры (jack)"
        return "отключение устройства"

    @staticmethod
    def _device_type_label(dev: "AudioDevice") -> str:
        """Человекочитаемый тип устройства для [AUDIO]-лог-строк."""
        mapping = {
            DeviceType.BLUETOOTH_HEADSET: "Bluetooth-гарнитура",
            DeviceType.BLUETOOTH_HEADPHONES: "Bluetooth-наушники",
            DeviceType.WIRED_HEADSET: "Проводная гарнитура",
            DeviceType.USB_MICROPHONE: "USB-микрофон",
            DeviceType.BUILTIN_MICROPHONE: "Микрофон",
            DeviceType.BUILTIN_SPEAKERS: "Динамики",
        }
        return mapping.get(
            dev.dtype, "Устройство ввода" if dev.is_input else "Устройство вывода"
        )

    @staticmethod
    def _device_emoji(dev: "AudioDevice") -> str:
        """Эмодзи для типа устройства в лог-сообщениях."""
        dtype = dev.dtype
        if dtype in (DeviceType.BLUETOOTH_HEADSET, DeviceType.BLUETOOTH_HEADPHONES):
            return "🎧"
        if dtype == DeviceType.WIRED_HEADSET:
            return "🎧"
        if dtype == DeviceType.USB_MICROPHONE:
            return "🔌"
        if dev.is_input:
            return "🎤"
        return "🔊"


    def _run(self) -> None:
        self._poll_loop()

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self.POLL_INTERVAL):
            try:
                devices = list_devices()
            except Exception:  # noqa: BLE001 — не падаем в фоновом потоке
                continue
            current: dict[str, "AudioDevice"] = {d.id: d for d in devices}
            added_ids = current.keys() - self._snapshot.keys()
            removed_ids = self._snapshot.keys() - current.keys()

            # --- Обработка отключений ---
            for dev_id in removed_ids:
                removed_dev = self._snapshot[dev_id]
                reason = self._disconnect_reason(removed_dev)
                emoji = self._device_emoji(removed_dev)
                log(
                    f"[AUDIO] {emoji} Отключение: "
                    f"{self._device_type_label(removed_dev)} "
                    f"«{removed_dev.name}» — {reason}"
                )
                if self.on_device_removed:
                    try:
                        self.on_device_removed(dev_id)
                    except Exception as exc:  # noqa: BLE001
                        log(f"[AudioDevices] Ошибка on_device_removed: {exc}")
                if self.on_device_removed_with_info:
                    try:
                        self.on_device_removed_with_info(removed_dev)
                    except Exception as exc:  # noqa: BLE001
                        log(f"[AudioDevices] Ошибка on_device_removed_with_info: {exc}")

            # --- Обработка подключений ---
            for dev_id in added_ids:
                dev = current[dev_id]
                emoji = self._device_emoji(dev)
                log(
                    f"[AUDIO] {emoji} Подключение: "
                    f"{self._device_type_label(dev)} «{dev.name}»"
                )
                if self.on_device_added:
                    try:
                        self.on_device_added(dev)
                    except Exception as exc:  # noqa: BLE001
                        log(f"[AudioDevices] Ошибка on_device_added: {exc}")

            defaults = self._current_defaults()
            if defaults != self._defaults and self.on_default_changed:
                for kind, old_id in self._defaults.items():
                    new_id = defaults.get(kind)
                    if new_id and new_id != old_id:
                        try:
                            self.on_default_changed(kind, new_id)
                        except Exception as exc:  # noqa: BLE001
                            log(f"[AudioDevices] Ошибка on_default_changed: {exc}")
            self._defaults = defaults
            self._snapshot = current


# ---------------------------------------------------------------------------
# Глобальный singleton-вотчер (удобный API для остальных модулей)
# ---------------------------------------------------------------------------
_global_watcher: DeviceWatcher | None = None


def get_global_watcher() -> DeviceWatcher:
    """Возвращает (и при необходимости создаёт) глобальный экземпляр DeviceWatcher."""
    global _global_watcher
    if _global_watcher is None:
        _global_watcher = DeviceWatcher()
    return _global_watcher


def stop_watcher() -> None:
    """Останавливает глобальный DeviceWatcher (вызывается из controller.cleanup)."""
    global _global_watcher
    if _global_watcher is not None:
        _global_watcher.stop()
        _global_watcher = None