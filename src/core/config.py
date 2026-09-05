"""Модуль управления настройками Зевса.

Работает с settings.json рядом с исполняемым файлом (или внутри .exe
через временную папку PyInstaller). Позволяет пользователю редактировать
конфигурацию без перезапуска приложения.

Новая иерархия (v1.1):
  - ai_settings: модель, температура, лимит токенов, системный промпт
  - voice_settings: включение, wake_word, tts, микрофон
  - app_settings: тема, автозапуск, путь к данным
"""
import json
import os
import sys
import threading
from copy import deepcopy
from pathlib import Path

# Единый лок на запись/чтение settings.json: файл может сохраняться
# из нескольких фоновых потоков одновременно (слайдер, тумблеры,
# кнопка «Сохранить») — без него возможна потеря/повреждение файла.
_settings_io_lock = threading.Lock()


def resource_path(relative_path: str) -> str:
    """Универсальный путь к ресурсу (модели, конфиги, ассеты).

    Порядок поиска:
      1. Папка рядом с .exe / корень проекта — там лежат изменяемые
         ресурсы (data/, models/), которые пользователь может править.
      2. sys._MEIPASS — ресурсы, зашитые PyInstaller внутрь пакета.
      3. __file__-относительный путь (режим разработки).
    Возвращает первый существующий путь; если ничего не найдено —
    вариант рядом с exe/проектом (чтобы код мог создать файл).
    """
    # 1) Рядом с exe (frozen) / корень проекта (разработка)
    base = app_base_dir()
    candidate = os.path.join(base, relative_path)
    if os.path.exists(candidate):
        return candidate

    # 2) Ресурсы, зашитые PyInstaller (onefile / datas в _internal)
    try:
        meipass = sys._MEIPASS  # type: ignore[attr-defined]
        candidate = os.path.join(meipass, relative_path)
        if os.path.exists(candidate):
            return candidate
    except Exception:
        pass

    # 3) Fallback — рядом с exe/проектом (путь будет создан при записи)
    return os.path.join(base, relative_path)


def app_base_dir() -> str:
    """Возвращает корневую папку приложения.

    - Режим .exe (PyInstaller): папка рядом с исполняемым файлом.
    - Режим разработки: корень проекта (родительская папка для src/).
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    # config.py лежит в src/core/, поэтому поднимаемся на 3 уровня до корня проекта
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def data_dir() -> str:
    """Возвращает (и при необходимости создаёт) папку data/ для конфигураций.

    Вся изменяемая конфигурация и пользовательские файлы (.json) хранятся
    в отдельной папке data/, чтобы отличать их от кода интерфейса и логики.
    """
    directory = os.path.join(app_base_dir(), "data")
    os.makedirs(directory, exist_ok=True)
    return directory


def data_path(relative_path: str) -> str:
    """Возвращает путь к изменяемому файлу внутри папки data/.

    Для файлов, которые могут изменяться в процессе работы программы:
    settings.json, commands.json, index.json, file_cache.json.
    При запуске .exe эти файлы читаются из папки data/ рядом с .exe,
    а в режиме разработки — из папки data/ в корне проекта.
    """
    return os.path.join(data_dir(), relative_path)


# Путь к файлу настроек: в папке data/ рядом с .exe (или в data/ проекта)
# Используем data_path() чтобы при запуске .exe файл открывался
# рядом с .exe, а не внутри временной _MEIPASS
SETTINGS_PATH = data_path("settings.json")

# Значения по умолчанию (новая иерархическая структура)
DEFAULT_SETTINGS = {
    # Плоские ключи v1.3 оставлены основным публичным контрактом.
    "theme": "dark",
    "auto_listening": True,
    "audio": {
        "input_device_index": None,
        "speech_rate": 1.0,
    },
    "ai_settings": {
        "model": "llama3.1",
        "temperature": 0.7,
        "max_tokens": 2048,
        "system_prompt": (
            "Ты — Зевс, могущественный интеллектуальный ассистент. "
            "Отвечай на русском языке, кратко, по делу и дружелюбно."
        ),
    },
    "voice_settings": {
        "enabled": True,
        "wake_word": "Зевс",
        "tts_enabled": True,
        "language": "ru-RU",
        "mic_index": None,
        # Параметры синтеза речи под «Джарвиса»:
        # быстрый темп 185–200 слов/мин, максимальная громкость, мужской голос.
        # voice="auto" — авто-подбор; можно указать конкретный id голоса.
        "rate": 190,
        "volume": 1.0,
        "voice": "auto",
        # Выбор локального TTS-движка: "piper" (нейросеть, модели в models/tts/)
        # или "pyttsx3" (системный SAPI5). При недоступности Piper автоматически
        # срабатывает фолбэк на pyttsx3.
        "tts_engine": "piper",
        # Контекстный диалог: уточнения без повторного слова «Зевс»
        "follow_up_enabled": True,
        "follow_up_timeout": 5,   # секунд «окна диалога» после команды (тишина -> сон)
        # Звуковые эффекты интерфейса (SFX) в стиле «Железного человека»
        "sfx_enabled": True,
        # Логирование истории команд в файл сессии (data/logs/)
        "logging_enabled": True,
        # Путь к локальной модели Piper (.onnx; рядом ожидается .onnx.json).
        "piper_model": "models/tts/voice.onnx",
        # Темп: меньше 1.0 — быстрее (Джарвис ~0.9). "Ровность" 0.6.
        "piper_length_scale": 0.9,
        "piper_noise_scale": 0.6,
        # Порог чувствительности Wake Word (энергетический фильтр / VAD):
        # ниже — детектор чувствительнее, но чаще ложные срабатывания на шум/эхо.
        "wake_energy_threshold": 30,
    },
    "app_settings": {
        "theme": "dark",
        "auto_listening": True,
        "autostart": False,
        "start_minimized": False,
        "data_path": "./data",
        # Глобальная горячая клавиша Ctrl+Alt+Space для вызова ассистента
        "hotkey_enabled": True,
        "global_hotkey_enabled": True,
        # Отладочный вывод фонового распознавания Wake Word («Зевс»)
        "debug_wake": False,
        # Автоматическое сканирование новых игр/приложений (рабочий стол + Steam)
        "auto_scan": True,
    },
    "paths": {
        "workspace": "./data",
        "dictionary": "./data/dictionary.json",
        "vosk_model_path": "models/vosk-model-small-ru-0.22",
        "piper_model_path": "models/tts/voice.onnx",
    },
}

# Плоские ключи для обратной совместимости
_OLD_KEYS = {
    "model": ("ai_settings", "model"),
    "system_prompt": ("ai_settings", "system_prompt"),
    "temperature": ("ai_settings", "temperature"),
    "voice_enabled": ("voice_settings", "enabled"),
    "language": ("voice_settings", "language"),
    "theme_mode": ("app_settings", "theme"),
    "theme": "theme_deprecated",  # декоративная тема из старой версии
}

# Список тем оформления
THEMES = {
    "Dark Gold": {
        "bg": "#0B0B10",
        "surface": "#16161F",
        "surface_2": "#1F1F2B",
        "accent": "#E8B339",
        "accent_hover": "#F5C95B",
        "accent_text": "#1A1407",
        "electric": "#3FC8FF",
        "electric_text": "#04222E",
        "text": "#EDEDF2",
        "text_dim": "#9A9AA8",
    },
    "Cyber Blue": {
        "bg": "#05080F",
        "surface": "#0B1220",
        "surface_2": "#111C30",
        "accent": "#1FB6FF",
        "accent_hover": "#4FCBFF",
        "accent_text": "#02121F",
        "electric": "#00E5C0",
        "electric_text": "#012019",
        "text": "#E6F7FF",
        "text_dim": "#7FA8C0",
    },
    "Classic Black": {
        "bg": "#000000",
        "surface": "#0D0D0D",
        "surface_2": "#1A1A1A",
        "accent": "#C0C0C0",
        "accent_hover": "#E0E0E0",
        "accent_text": "#000000",
        "electric": "#888888",
        "electric_text": "#000000",
        "text": "#F5F5F5",
        "text_dim": "#888888",
    },
}


def _migrate_old_settings(data: dict) -> dict:
    """Мигрирует старые плоские настройки в новую иерархическую структуру."""
    migrated = deepcopy(DEFAULT_SETTINGS)

    # Если уже новая структура — используем как есть
    if any(section in data for section in (
        "ai_settings", "voice_settings", "app_settings", "audio", "paths",
        "theme", "auto_listening",
    )):
        for section in ("ai_settings", "voice_settings", "app_settings", "audio", "paths"):
            if section in data:
                if isinstance(data[section], dict):
                    migrated[section].update(data[section])
        if (
            "global_hotkey_enabled" not in data.get("app_settings", {})
            and "hotkey_enabled" in data.get("app_settings", {})
        ):
            migrated["app_settings"]["global_hotkey_enabled"] = data["app_settings"][
                "hotkey_enabled"
            ]
        migrated["theme"] = data.get("theme", migrated["app_settings"].get("theme"))
        migrated["auto_listening"] = data.get(
            "auto_listening", migrated["app_settings"].get("auto_listening", True)
        )
        migrated["audio"].setdefault(
            "input_device_index", migrated["voice_settings"].get("mic_index")
        )
        migrated["audio"].setdefault(
            "speech_rate", float(migrated["voice_settings"].get("rate", 190)) / 190
        )
        migrated["paths"].setdefault(
            "piper_model_path", migrated["voice_settings"].get("piper_model")
        )
        return migrated

    # Миграция старых плоских ключей
    for old_key, new_path in _OLD_KEYS.items():
        if old_key in data:
            if isinstance(new_path, tuple):
                section, key = new_path
                migrated[section][key] = data[old_key]
            # Если значение помечено как deprecated — пропускаем

    return migrated


def _normalize_v13_settings(settings: dict) -> dict:
    """Синхронизирует плоский контракт v1.3 со старыми секциями приложения."""
    settings = deepcopy(settings)
    app = settings.setdefault("app_settings", {})
    voice = settings.setdefault("voice_settings", {})
    audio = settings.setdefault("audio", {})
    paths = settings.setdefault("paths", {})
    app["theme"] = settings.get("theme", app.get("theme", "dark"))
    app["auto_listening"] = settings.get(
        "auto_listening", app.get("auto_listening", True)
    )
    settings["theme"] = app["theme"]
    settings["auto_listening"] = app["auto_listening"]
    audio["input_device_index"] = (
        voice["mic_index"] if voice.get("mic_index") is not None
        else audio.get("input_device_index")
    )
    audio["speech_rate"] = audio.get("speech_rate", 1.0)
    paths["piper_model_path"] = paths.get(
        "piper_model_path", voice.get("piper_model", "models/tts/voice.onnx")
    )
    return settings


class SettingsManager:
    """Потокобезопасный менеджер локальных настроек приложения.

    Менеджер держит настройки в памяти, сохраняет каждое изменение атомарно
    и создаёт файл с дефолтами, если его ещё нет. Доступ к данным наружу
    возвращается копией, чтобы фоновые потоки не меняли состояние мимо API.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path or SETTINGS_PATH)
        self._lock = threading.RLock()
        self._settings = self._read()
        # Всегда сохраняем нормализованную схему: это создаёт отсутствующий
        # файл, восстанавливает повреждённый JSON и фиксирует миграции старых
        # настроек до первого обращения UI к ним.
        self.save()

    @staticmethod
    def _merge(defaults: dict, data: dict) -> dict:
        result = deepcopy(defaults)
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key].update(value)
            else:
                result[key] = value
        return result

    def _read(self) -> dict:
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError, TypeError):
            data = {}
        return _normalize_v13_settings(
            self._merge(DEFAULT_SETTINGS, _migrate_old_settings(data))
        )

    def get_all(self) -> dict:
        with self._lock:
            return deepcopy(self._settings)

    def get(self, section: str, key: str, default=None):
        with self._lock:
            return self._settings.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value) -> bool:
        with self._lock:
            target = self._settings.setdefault(section, {})
            if not isinstance(target, dict):
                return False
            target[key] = value
            if section == "app_settings" and key in ("theme", "auto_listening"):
                self._settings[key] = value
            elif section == "voice_settings" and key == "mic_index":
                self._settings.setdefault("audio", {})["input_device_index"] = value
            elif section == "voice_settings" and key == "piper_model":
                self._settings.setdefault("paths", {})["piper_model_path"] = value
            return self.save()

    def update(self, section: str, values: dict) -> bool:
        with self._lock:
            target = self._settings.setdefault(section, {})
            if not isinstance(target, dict):
                return False
            target.update(values)
            self._settings = _normalize_v13_settings(self._settings)
            return self.save()

    def save(self) -> bool:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
                with tmp_path.open("w", encoding="utf-8") as file:
                    json.dump(self._settings, file, ensure_ascii=False, indent=4)
                os.replace(tmp_path, self.path)
                return True
            except OSError:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return False

    def reset(self) -> bool:
        with self._lock:
            self._settings = deepcopy(DEFAULT_SETTINGS)
            self._settings = _normalize_v13_settings(self._settings)
            return self.save()


def load_settings() -> dict:
    """Загружает настройки из settings.json, дополняя дефолтами.

    Автоматически мигрирует старый формат в новый.
    Возвращает плоский словарь с иерархическими ключами для обратной совместимости.
    """
    settings = deepcopy(DEFAULT_SETTINGS)
    try:
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Миграция и обновление
            migrated = _migrate_old_settings(data)
            for section in ("ai_settings", "voice_settings", "app_settings", "audio", "paths"):
                settings[section].update(migrated[section])
            settings = _normalize_v13_settings(settings)
    except Exception:
        pass

    return settings


def save_settings(settings: dict) -> bool:
    """Сохраняет настройки в settings.json. Возвращает успех операции.

    Запись атомарная (tmp + os.replace) и сериализуется локом: параллельные
    сохранения из разных фоновых потоков не повреждают файл.
    """
    try:
        with _settings_io_lock:
            # Резервная копия предыдущей версии (защита от порчи файла
            # при сбое питания/диска в момент записи).
            if os.path.exists(SETTINGS_PATH):
                try:
                    import shutil
                    shutil.copy2(SETTINGS_PATH, SETTINGS_PATH + ".bak")
                except Exception:  # noqa: BLE001 — бэкап не критичен
                    pass
            tmp_path = SETTINGS_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, SETTINGS_PATH)
        return True
    except Exception:
        # Убираем «застрявший» tmp при ошибке
        try:
            if os.path.exists(SETTINGS_PATH + ".tmp"):
                os.remove(SETTINGS_PATH + ".tmp")
        except Exception:  # noqa: BLE001
            pass
        return False


def get_theme(name: str) -> dict:
    """Возвращает цветовую схему темы по имени."""
    return THEMES.get(name, THEMES["Dark Gold"])


def get_available_themes() -> list[str]:
    """Возвращает список доступных тем."""
    return list(THEMES.keys())
