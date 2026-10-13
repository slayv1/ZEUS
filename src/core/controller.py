"""Контроллер Зевса — ядро обработки.

Архитектура:
  1. Получает входящий текст (из голоса или из чата)
  2. Проверяет через actions.execute_command() — системная ли команда?
  3. Если ДА -> выполняет действие
  4. Если НЕТ -> отправляет в chat_engine (Ollama)

Спящий режим:
  - is_active управляет голосовым прослушиванием
  - Команды "Зевс, отключись" / "Зевс, включись"
  - В спящем режиме чат текстовый продолжает работать
"""
from __future__ import annotations

import concurrent.futures
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Callable, Any

# Модули Зевса
from modules.audio_engine import AudioEngine
from modules import sfx
from core.chat_engine import ChatEngine
from core import config
from core import actions
from core import file_scanner
from core.assistant_state import ZeusStateController
from core.controller_devices import ZeusDevicesMixin
from core.controller_voice import ZeusVoiceMixin
from core.command_router import router
# log() используется в аудио-диагностике (Спринт 1: [AUDIO]-строки в терминал).
# Раньше модуль вызывал log() без импорта — NameError при переключении
# устройств из трея (ошибка маскировалась собственным except метода).
from core.logging_service import log


def _register_default_commands() -> None:
    """Регистрация стандартных команд Зевса в асинхронном диспетчере (v1.2).

    Команды можно вызывать из async-обработчиков Flet:
        asyncio.create_task(router.dispatch("taobao_search", "пуховик"))
    или из потокового кода:
        router.dispatch_sync("taobao_search", "пуховик")
    """
    if not router.has("taobao_search"):
        from modules.taobao_core import search_taobao_smart
        router.register("taobao_search", search_taobao_smart)
    if not router.has("run_command"):
        # Универсальный прогон текста через actions (системная команда или LLM).
        router.register("run_command", actions.execute_command)
    from core import command_effects
    for _name, _handler in (
        ("open_url", command_effects.open_url),
        ("web_search", command_effects.search_web),
        ("launch_app", command_effects.launch_named),
        ("close_app", command_effects.close_named),
    ):
        if not router.has(_name):
            router.register(_name, _handler)


# Регистрация на уровне модуля: достаточно импортировать controller,
# чтобы команды были доступны в диспетчере (идемпотентно).
_register_default_commands()


# Глобальный пул для безопасных фоновых вызовов без постоянного создания потоков
_SAFE_CMD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="zeus-cmd-safely"
)


def execute_command_safely(command_func, *args, **kwargs):
    """Универсальная обёртка: запускает любую задачу/системный вызов в фоне.

    Используется для обработчиков событий Flet (нажатия, горячие клавиши,
    коллбэки), чтобы главный поток интерфейса мгновенно завершал обработку
    события и не показывал плашку «Working…». Daemon-поток или пул не блокируют
    завершение приложения.
    """
    def worker():
        try:
            command_func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            import traceback
            from core.logging_service import error
            error(f"[Controller] фоновая команда: {exc}\n{traceback.format_exc()}")

    try:
        _SAFE_CMD_EXECUTOR.submit(worker)
    except Exception:
        threading.Thread(target=worker, daemon=True, name="zeus-cmd-safely").start()


class ZeusController(ZeusVoiceMixin, ZeusDevicesMixin):
    """Главный контроллер Зевса.

    Связывает audio_engine, chat_engine и actions в единую логику.
    Реализует фильтрацию: сначала actions -> потом chat_engine.

    Attributes:
        audio: AudioEngine (голосовой ввод/вывод)
        chat: ChatEngine (Ollama LLM)
        is_active: флаг спящего режима
        settings: dict с настройками из settings.json
        on_system_command: callback(command_result) при системной команде
        on_chat_start: callback(text) при старте отправки в LLM
        on_chat_token: callback(token) при стриминге токена
        on_chat_complete: callback(full_text) при завершении LLM
        on_chat_error: callback(error) при ошибке LLM
        on_status: callback(status_text) при изменении статуса
        on_voice_command: callback(text) при голосовой команде

    Конечный автомат (state):
        IDLE       — «Ожидание»: микрофон слушает ТОЛЬКО Wake Word «Зевс» (Vosk)
        LISTENING  — «Слушатель»: озвучено «Да, сэр?», микрофон слушает команду
        PROCESSING — «Выполняю»: команда передана в actions/Ollama, затем возврат в IDLE
    """

    def __init__(self):
        # Регистрируем команды в асинхронном диспетчере v1.2
        _register_default_commands()

        # Единый владелец настроек: создаёт settings.json при первом запуске
        # и сериализует изменения из UI и фоновых потоков.
        self.settings_manager = config.SettingsManager()
        self.settings = self.settings_manager.get_all()

        # --- Callbacks для UI (объявлены в самом начале, до любых вызовов методов) ---
        # Важно: инициализируются здесь, чтобы избежать AttributeError при вызове _set_status
        # до регистрации обработчиков из UI
        self.on_system_command: Callable[[dict[str, Any]], None] | None = None
        self.on_chat_start: Callable[[str], None] | None = None
        self.on_chat_token: Callable[[str], None] | None = None
        self.on_chat_complete: Callable[[str], None] | None = None
        self.on_chat_error: Callable[[str], None] | None = None
        self.on_status: Callable[[str], None] | None = None
        self.on_voice_command: Callable[[str], None] | None = None
        self.on_state: Callable[[str], None] | None = None

        # Потокобезопасная блокировка переключения аудиорежимов.
        # Предотвращает гонку за микрофон (race condition) при быстрых
        # переходах IDLE <-> LISTENING между потоками Wake Word и распознавания.
        self._audio_lock = threading.Lock()
        # Счётчик поколений переключений аудиорежима: каждая новая команда
        # (wake/command) увеличивает его; отложенные/устаревшие переключения
        # отбрасываются, чтобы устаревший wake не «перебил» свежий command.
        self._switch_seq = 0
        # Гарантированное окно удержания активного режима: после «Зевс»
        # не возвращаемся в wake-режим минимум ~7 секунд, чтобы пользователь
        # успел продиктовать команду целиком.
        self._active_hold_until = 0.0
        self._active_hold_timeout = 5.0  # ТЗ: актив без команды -> сон через 4-5 c
        # Таймер авто-возврата в IDLE: если после «Зевс» команда не продиктована
        # в течение hold_time, ассистент сам возвращается в ожидание слова.
        self._idle_return_timer: threading.Timer | None = None
        # Минимальная длительность «активной сессии»: после пробуждения и после
        # каждой команды не уходим в спокойный режим мгновенно — хвост речи и
        # следующая короткая фраза пользователя должны быть услышаны, а не
        # сброшены. Иначе первое же срабатывание Vosk «усыпляет» ассистента.
        self._min_active_session = 3.0
        self._session_active_until = 0.0
        # Отложенный повтор возврата в IDLE, когда сессия ещё «молодая».
        self._session_retry_timer: threading.Timer | None = None

        # Пул рабочих потоков для вспомогательных асинхронных задач (вместо пложения потоков)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="zeus-worker"
        )
        self._is_cleaned_up = False

        # --- Фоновое исполнение команд (защита UI от зависаний) ---
        # Тяжёлая работа (загрузка/поиск commands.json, нечёткое
        # сопоставление, запуск внешних процессов, HTTP к Ollama)
        # выполняется в выделенном потоке-потребителе через очередь —
        # ни поток Flet, ни коллбэки аудио-движка ничего не блокируют.
        # Очередь сохраняет порядок команд, как при синхронной обработке.
        self._cmd_queue: queue.Queue = queue.Queue()
        self._cmd_thread = threading.Thread(
            target=self._command_loop, daemon=True, name="zeus-commands"
        )
        self._cmd_thread.start()

        # Пустая база команд на новой машине — из шаблона, не из чужого снимка.
        try:
            actions.ensure_commands_file()
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] база команд: {exc}")

        # Инициализация движков
        voice = self.settings.get("voice_settings", {})
        self.audio = AudioEngine(
            language=voice.get("language", "ru-RU"),
            mic_index=voice.get("mic_index"),
        )
        self.audio.on_speaking_changed = self._on_speaking_changed
        self.chat = ChatEngine()

        # Регистрируем голосовое уведомление о системных ошибках (Crash Handler).
        # При любом непредвиденном исключении Зевс мягко сообщит: «Произошла
        # системная ошибка, сэр», а не аварийно закроет окно.
        try:
            from core import crash_handler
            crash_handler.set_crash_notifier(self.audio.speak)
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] crash_handler не подключился: {exc}")

        # Применяем настройки к движкам
        self._apply_settings()

        # Watchdog: динамическая перезагрузка commands.json при его изменении.
        # Наблюдатель запускается прямо в инициализаторе контроллера (ядро);
        # при сохранении файла вызывается actions.reload_commands(). UI позже
        # подключает свой колбэк через start_watching(callback=...) — он
        # применяется даже при уже запущенном наблюдателе.
        self._watcher = None
        try:
            from core.file_watcher import get_watcher
            watcher = get_watcher()
            watcher.register_reload("commands.json", actions.reload_commands)
            watcher.start()
            self._watcher = watcher
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] наблюдатель commands.json не запустился: {exc}")

        # Наблюдатель горячего подключения аудиоустройств (hot-plug).
        # Колбэки on_device_added / on_device_removed_with_info обновляют
        # аудио-подменю системного трея без перезапуска.
        # Ссылка on_tray_audio_update — назначается из main_flet после создания трея.
        self.on_tray_audio_update: Callable[[], None] | None = None
        # UI-колбэк hot-plug событий (payload-словари для main_flet, §3.1)
        self._ui_device_event_cb: Callable[[dict[str, Any]], None] | None = None
        self._device_watcher: "DeviceWatcher | None" = None
        try:
            from core import audio_devices as _ad
            dw = _ad.get_global_watcher()
            dw.on_device_added = lambda dev: self._on_audio_device_changed()
            dw.on_device_removed = lambda dev_id: self._on_audio_device_changed()
            dw.on_device_removed_with_info = lambda dev: self._on_audio_device_changed()
            # Смена системного аудио-маршрута ОС (Спринт 1, диагностика):
            # логируем причину в терминал и обновляем аудио-подменю трея.
            dw.on_default_changed = (
                lambda kind, dev_id: self._on_audio_default_changed(kind, dev_id)
            )
            dw.start()
            self._device_watcher = dw
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] наблюдатель аудиоустройств не запустился: {exc}")

        # Фоновый авто-сканер новых игр/приложений (рабочий стол + Steam).
        # Периодически дописывает найденное в commands.json; watchdog тут же
        # перечитывает файл, и команды обновляются без перезапуска.
        try:
            if self.settings.get("app_settings", {}).get("auto_scan", True):
                from core import auto_scanner
                auto_scanner.auto_scan_in_background(interval=30)
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] автосканер не запустился: {exc}")

        # Спящий режим
        self.is_active = True

        # Конечный автомат голосового потока. Строка self.state — язык
        # микрофона; state_controller — тот же автомат для голограммы и кнопок.
        self.state = "IDLE"  # IDLE, LISTENING, PROCESSING, SPEAKING, ERROR
        self.state_controller = ZeusStateController()
        self._after_speech: str | None = None

        # Голосовое управление из настроек
        app_settings = self.settings.get("app_settings", {})
        if (not self.settings["voice_settings"].get("enabled", True)
            or not app_settings.get("auto_listening", True)):
            self.is_active = False

        # --- Новое: контекстный диалог (память сессии) ---
        voice = self.settings.get("voice_settings", {})
        self.follow_up_enabled = bool(voice.get("follow_up_enabled", True))
        self.follow_up_timeout = int(voice.get("follow_up_timeout", 8))
        self._follow_up_timer: threading.Timer | None = None
        # Отложенное открытие окна уточнений: флаг ставится при распознанной
        # голосовой команде (_on_command_heard), а снимается ПОСЛЕ фактического
        # исполнения — в _finish_voice_command() (из _command_loop для системных
        # команд или из _on_chat_complete/_on_chat_error для ответов LLM).
        # Раньше окно открывалось сразу, и follow-up-таймер тикал, пока команда
        # ещё стояла в очереди/генерировалась.
        self._pending_follow_up = False
        # Страховочный таймер: если генерация LLM зависла, принудительно
        # завершаем голосовую команду, чтобы FSM не остался в PROCESSING.
        self._followup_watchdog: threading.Timer | None = None

        # --- Новое: звуковые эффекты и логирование ---
        sfx.set_enabled(bool(voice.get("sfx_enabled", True)))
        self.logging_enabled = bool(voice.get("logging_enabled", True))
        self._session_log_path = os.path.join(
            config.data_dir(), "logs", f"session_{time.strftime('%Y%m%d')}.log"
        )

        # Настройка аудио-движка
        self.audio.on_command = self._on_voice_input

        # Настройка чат-движка
        self.chat.on_token = self._on_chat_token
        self.chat.on_complete = self._on_chat_complete
        self.chat.on_error = self._on_chat_error

    # ------------------------------------------------------------------
    # Настройки
    # ------------------------------------------------------------------
    def _apply_settings(self):
        """Применяет текущие настройки к движкам."""
        ai = self.settings.get("ai_settings", {})
        voice = self.settings.get("voice_settings", {})

        # Применяем AI настройки
        self.chat.set_model(ai.get("model", "llama3.1"))
        self.chat.set_temperature(ai.get("temperature", 0.7))
        self.chat.set_system_prompt(ai.get("system_prompt", ""))

        # Агентный каркас (Harness, ТЗ v1.4): инструменты оркестрации LLM.
        self.chat.set_agent_config(
            enabled=bool(ai.get("agent_enabled", False)),
            max_steps=ai.get("agent_max_steps", 5),
            allow_write=bool(ai.get("agent_allow_write", False)),
            allow_actions=bool(ai.get("agent_allow_actions", False)),
        )

    def get_settings(self) -> dict:
        """Возвращает текущие настройки."""
        return self.settings

    def rebuild_file_index(self) -> dict:
        """Запускает переиндексацию файлов в фоне (для кнопки "Пересканировать
        сейчас" в настройках). Не блокирует UI — сам rebuild_index() уже
        асинхронный, здесь просто прокидываем вызов из UI-слоя в file_scanner.
        """
        return file_scanner.rebuild_system_index()

    def get_file_index_stats(self) -> dict:
        """Статистика индекса файлов для отображения в UI (сколько файлов,
        когда сканировали в последний раз, идёт ли сканирование сейчас)."""
        return file_scanner.get_scanner().get_index_stats()

    def _apply_noise_gate_settings(self) -> None:
        """Доводит настройки шумового гейта до живого детектора (ТЗ v1.4 §2.2).

        Порог Wake Word и общий выключатель применяются мгновенно. Строка
        статуса печатается с префиксом [AUDIO] — такие строки мост UI показывает
        в терминале, поэтому пользователь сразу видит фон, порог и счётчики
        вместо гадания «Зевс меня не слышит» vs «Зевс не хочет отвечать».
        """
        vs = self.settings.get("voice_settings", {}) or {}
        enabled = bool(vs.get("noise_gate_enabled", True))
        threshold = int(vs.get("wake_energy_threshold", 30) or 0)
        try:
            wake = getattr(self.audio, "wake", None)
            if wake is None:
                return
            wake.set_gate_config(enabled=enabled, threshold=threshold)
            print(f"[AUDIO] {wake.gate_status()}")
        except Exception as exc:  # noqa: BLE001 — настройка сохранена, применение вторично
            print(f"[AUDIO] Не удалось применить шумовой гейт: {exc}")

    def update_settings(self, section: str, key: str, value: Any) -> bool:
        """Обновляет конкретную настройку и сохраняет в JSON.

        Args:
            section: раздел ('ai_settings', 'voice_settings', 'app_settings')
            key: ключ внутри раздела
            value: новое значение

        Returns:
            True при успешном сохранении
        """
        if section not in self.settings or not isinstance(self.settings[section], dict):
            self.settings[section] = {}

        self.settings[section][key] = value
        saved = self.settings_manager.set(section, key, value)
        self.settings = self.settings_manager.get_all()

        # Мгновенное применение критических параметров
        if section == "ai_settings":
            if key == "model":
                self.chat.set_model(value)
            elif key == "temperature":
                self.chat.set_temperature(value)
            elif key == "system_prompt":
                self.chat.set_system_prompt(value)
            elif key in (
                "agent_enabled", "agent_max_steps",
                "agent_allow_write", "agent_allow_actions",
            ):
                # Агентный каркас: применяется к следующему же запросу модели,
                # без перезапуска — как шумовой гейт в voice_settings.
                ai = self.settings.get("ai_settings", {}) or {}
                self.chat.set_agent_config(
                    enabled=bool(ai.get("agent_enabled", False)),
                    max_steps=ai.get("agent_max_steps", 5),
                    allow_write=bool(ai.get("agent_allow_write", False)),
                    allow_actions=bool(ai.get("agent_allow_actions", False)),
                )
        elif section == "voice_settings":
            if key == "language":
                self.audio.language = value
            elif key == "enabled":
                if value and not self.is_active:
                    self.set_active(True)
                elif not value and self.is_active:
                    self.set_active(False)
            elif key == "sfx_enabled":
                sfx.set_enabled(bool(value))
            elif key == "follow_up_enabled":
                self.follow_up_enabled = bool(value)
                if not value:
                    self._cancel_follow_up_timer()
            elif key == "follow_up_timeout":
                self.follow_up_timeout = int(value or 8)
            elif key == "logging_enabled":
                self.logging_enabled = bool(value)
            elif key in ("noise_gate_enabled", "wake_energy_threshold",
                         "command_energy_threshold"):
                # Шумовой гейт (ТЗ v1.4 §2.2) применяем сразу, без перезапуска:
                # пользователь крутит порог и сразу видит, что Зевс отсекает шум,
                # а не глохнет вместе с ним. Порог команды подхватится следующим
                # стартом прослушивания (гейт создаётся в _listen_loop).
                self._apply_noise_gate_settings()
        elif section == "app_settings" and key == "auto_listening":
            if value and not self.is_active:
                self.set_active(True)
            elif not value and self.is_active:
                self.set_active(False)

        return saved

    def reset_settings(self) -> bool:
        """Сбрасывает настройки до стандартных."""
        saved = self.settings_manager.reset()
        self.settings = self.settings_manager.get_all()
        self._apply_settings()
        return saved

    # ------------------------------------------------------------------
    # Список моделей Ollama
    # ------------------------------------------------------------------
    def get_available_models(self) -> list[str]:
        """Возвращает список доступных моделей Ollama."""
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                models = []
                for line in lines[1:]:  # Пропускаем заголовок
                    if line.strip():
                        name = line.split()[0]
                        models.append(name)
                return models if models else ["llama3.1"]
        except Exception:
            pass
        return ["llama3.1"]

    # ------------------------------------------------------------------
    # Список микрофонов
    # ------------------------------------------------------------------
    def get_available_mics(self) -> list[str]:
        """Возвращает список доступных микрофонов."""
        mics = []
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            for i, device in enumerate(devices):
                if device['max_input_channels'] > 0:
                    mics.append(f"{i}: {device['name']}")
        except Exception:
            pass
        return mics

    def get_current_mic_index(self) -> int | None:
        """Возвращает текущий индекс микрофона."""
        return self.audio._mic_device_index

    def set_mic_index(self, index: int | None):
        """Устанавливает индекс микрофона."""
        self.audio._mic_device_index = index
        self.audio._mic_device_name = None
        if index is not None:
            try:
                import sounddevice as sd
                devices = sd.query_devices()
                if 0 <= index < len(devices):
                    self.audio._mic_device_name = devices[index]['name']
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Автозапуск
    # ------------------------------------------------------------------
    def set_autostart(self, enabled: bool) -> bool:
        """Добавляет/удаляет приложение из автозагрузки ОС.

        Returns:
            True при успехе
        """
        if os.name != "nt":
            return False

        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "Zeus"
        if getattr(sys, 'frozen', False):
            exe_path = sys.executable
        else:
            # Режим разработки: регистрируем интерпретатор + main.py.
            # Раньше здесь подставлялся None и команда тихо ничего не делала,
            # хотя возвращала True (UI показывал успех).
            main_py = os.path.join(config.app_base_dir(), "main.py")
            if not os.path.isfile(main_py):
                return False
            exe_path = f'"{sys.executable}" "{main_py}"'

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0,
                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
            ) as reg_key:
                if enabled and exe_path:
                    winreg.SetValueEx(reg_key, app_name, 0, winreg.REG_SZ, exe_path)
                elif not enabled:
                    try:
                        winreg.DeleteValue(reg_key, app_name)
                    except FileNotFoundError:
                        pass
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Статус
    # ------------------------------------------------------------------
    def _set_status(self, text: str):
        """Обновляет статус в UI, если колбэк зарегистрирован."""
        if hasattr(self, 'on_status') and self.on_status:
            try:
                self.on_status(text)
            except Exception as exc:
                log(f"[Controller] статус в UI не обновился: {exc}")

    def _log_session(self, event: str, detail: str, source: str = "text",
                     success: bool = True, action: str = ""):
        """Логирует событие сессии в файл history (data/logs/).

        Формат строки:
            [ЧЧ:ММ:СС] EVENT(источник) success/error action=... detail

        Аргументы:
            event: тип события (COMMAND, SYSTEM, WAKE, FOLLOW_UP, LLM_OK, LLM_ERR)
            detail: текст/описание
            source: voice / text / system / llm
        """
        if not self.logging_enabled:
            return
        try:
            os.makedirs(os.path.dirname(self._session_log_path), exist_ok=True)
            line = (
                f"[{time.strftime('%H:%M:%S')}] {event}({source}) "
                f"{'OK' if success else 'ERR'}"
                f"{f' action={action}' if action else ''}: {detail}\n"
            )
            with open(self._session_log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as exc:
            log(f"[Controller] не записал журнал сессии: {exc}")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def cleanup(self):
        """Освобождает ресурсы, останавливает фоновые потоки и таймеры."""
        self._is_cleaned_up = True
        self._cancel_follow_up_timer()
        self._cancel_idle_return_timer()
        self._cancel_session_retry()
        self._cancel_followup_watchdog()
        self.stop_device_watcher()

        # Корректная остановка очереди команд
        try:
            self._cmd_queue.put(None)
        except Exception:
            pass

        # Остановка вотчера файлов
        watcher = getattr(self, "_watcher", None)
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:
                pass

        # Остановка пула воркеров
        executor = getattr(self, "_executor", None)
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

        self.audio.cleanup()