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
        "autostart": False,
        "data_path": "./data",
        # Глобальная горячая клавиша Ctrl+Alt+Space для вызова ассистента
        "hotkey_enabled": True,
        # Отладочный вывод фонового распознавания Wake Word («Зевс»)
        "debug_wake": False,
        # Автоматическое сканирование новых игр/приложений (рабочий стол + Steam)
        "auto_scan": True,
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
    migrated = dict(DEFAULT_SETTINGS)

    # Если уже новая структура — используем как есть
    if "ai_settings" in data or "voice_settings" in data or "app_settings" in data:
        for section in ("ai_settings", "voice_settings", "app_settings"):
            if section in data:
                migrated[section].update(data[section])
        return migrated

    # Миграция старых плоских ключей
    for old_key, new_path in _OLD_KEYS.items():
        if old_key in data:
            if isinstance(new_path, tuple):
                section, key = new_path
                migrated[section][key] = data[old_key]
            # Если значение помечено как deprecated — пропускаем

    return migrated


def load_settings() -> dict:
    """Загружает настройки из settings.json, дополняя дефолтами.

    Автоматически мигрирует старый формат в новый.
    Возвращает плоский словарь с иерархическими ключами для обратной совместимости.
    """
    settings = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Миграция и обновление
            migrated = _migrate_old_settings(data)
            for section in ("ai_settings", "voice_settings", "app_settings"):
                settings[section].update(migrated[section])
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
