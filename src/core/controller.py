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


class ZeusController:
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
                auto_scanner.auto_scan_in_background(interval=300)
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
    # Спящий режим
    # ------------------------------------------------------------------
    def set_active(self, active: bool, confirm: bool = True):
        """Включает/выключает спящий режим.

        При активации из спящего режима даёт короткое аудио-
        подтверждение (если confirm=True и TTS доступен) и возвращает
        конечный автомат в IDLE (ожидание Wake Word «Зевс»).
        """
        was_active = self.is_active
        self.is_active = active

        if not active:
            # Останавливаем ВСЁ прослушивание (Wake и полное)
            self._cancel_follow_up_timer()
            self._transition_state("IDLE")
            # stop_listening() ждёт завершения потока прослушивания
            # (_listen_thread.join(timeout=3)) — выполняем его в пуле потоков,
            # чтобы не блокировать поток Flet (кнопка «Отключиться») и коллбэки аудио-движка.
            try:
                self._executor.submit(self.audio.stop_listening)
            except Exception:
                threading.Thread(
                    target=self.audio.stop_listening,
                    daemon=True,
                    name="zeus-audio-stop",
                ).start()
        else:
            # Возврат в «ожидание»: микрофон слушает только «Зевс»
            self._reset_to_idle()

        if active and not was_active and confirm:
            # Короткое аудио-подтверждение активации
            self._speak("Да, сэр.")
        elif not active and was_active and confirm:
            # Короткое подтверждение отключения
            self._speak("Есть, сэр.")

        status = "активирован" if active else "отключён (спящий режим)"
        self._set_status(f"Зевс {status}")

    def toggle_active(self):
        """Переключает спящий режим."""
        self.set_active(not self.is_active)

    # ------------------------------------------------------------------
    # Обработка входящего текста (фильтрация)
    # ------------------------------------------------------------------
    def process_text(self, text: str, source: str = "text"):
        """Фильтрация входящего текста.

        1. Проверка на команды спящего режима
        2. Проверка на системные команды (actions)
        3. Если ничего не подошло -> отправка в LLM

        Args:
            text: входящий текст
            source: источник ("voice" | "text")
        """
        if not text.strip():
            return

        self._log_session("COMMAND", text, source=source)

        # --- Стоп-слово («молчи»/«стоп»/«хватит» и т.д.) ---
        # ВАЖНО: раньше эта проверка была только внутри
        # AudioEngine._handle_listen_text() (основной цикл «разбудили по
        # Зевс -> слушаем команду»). Всё, что попадает сюда любым ДРУГИМ
        # путём — например кнопка «Прослушать один раз»
        # (controller.listen_once() -> process_text(source="voice")), —
        # проверку не проходило вообще, и «стоп» улетало в LLM как обычная
        # фраза вместо прерывания речи. _is_stop_command()/_handle_stop_command()
        # уже существовали в этом классе, но были фактически мертвы: ничего
        # их не вызывало на реальном входящем тексте. Теперь вызывается здесь,
        # так что работает независимо от того, как текст попал в process_text().
        if self._is_stop_command(text):
            self._handle_stop_command(text)
            return

        # --- Проверка команд спящего режима ---
        text_lower = text.lower().strip()

        if "отключись" in text_lower and ("зеве" in text_lower or "зевс" in text_lower):
            self.set_active(False)
            # Короткое аудио-подтверждение уже дано в set_active()
            self._system_response({
                "success": True,
                "message": "Зевс отключён, сэр. Чтобы включить, скажите «Зевс, включись».",
                "action": "sleep_off",
            }, speak=False)
            return

        if "включись" in text_lower and ("зеве" in text_lower or "зевс" in text_lower):
            self.set_active(True)
            # Короткое аудио-подтверждение уже дано в set_active()
            self._system_response({
                "success": True,
                "message": "Зевс активирован, сэр. Чем могу помочь?",
                "action": "sleep_on",
            }, speak=False)
            return

        # --- Если спящий режим и текст (не голос) -> всё равно пропускаем ---
        # Если спящий режим и голос -> игнорируем
        if source == "voice" and not self.is_active:
            self._set_status("Спящий режим — команда проигнорирована")
            return

        # --- Этап 1+2: постановка команды в фоновую очередь ---
        # Тяжёлая работа (поиск/запуск, обращение к ФС, HTTP к Ollama)
        # выполняется в потоке-потребителе _command_loop(), не блокируя
        # ни поток Flet (UI остаётся отзывчивым), ни аудио-поток Vosk.
        self._enqueue_command(text)

    def _enqueue_command(self, text: str):
        """Ставит команду в очередь фонового исполнителя.

        UI/аудио-потоки мгновенно возвращаются после постановки; порядок
        команд сохраняется благодаря последовательному потреблению.
        """
        try:
            self._cmd_queue.put(text)
        except Exception as e:  # noqa: BLE001
            self._system_response({
                "success": False,
                "message": f"Не удалось поставить команду в очередь: {e}",
                "action": "unknown",
            })

    def _command_loop(self):
        """Поток-потребитель: последовательно исполняет команды из очереди.

        Здесь выполняется вся потенциально долгая работа:
          1. actions.execute_command() — сопоставление по commands.json,
             запуск игр/приложений (Popen/os.system), сканы файловой системы;
          2. отправка в Ollama (_send_to_llm), если системная команда
             не найдена.
        Любое исключение перехватывается — приложение не падает и не висит,
        пользователь получает голосовое сообщение об ошибке.
        """
        while True:
            text = self._cmd_queue.get()
            if text is None:  # корректное завершение при закрытии приложения
                # Важно пометить и sentinel как «обработанный», иначе
                # queue.join() останется заблокированным навсегда.
                self._cmd_queue.task_done()
                break
            try:
                try:
                    command_result = router.call("run_command", text)
                except Exception as e:  # noqa: BLE001
                    import traceback
                    log(f"[Controller] Ошибка команды: {traceback.format_exc()}")
                    self._transition_state("ERROR")
                    command_result = {
                        "success": False,
                        "message": f"Ошибка выполнения команды: {e}",
                        "action": "unknown",
                    }

                try:
                    if command_result is not None:
                        self._handle_system_command(command_result)
                        # Системная команда исполнена — только теперь открываем
                        # окно уточнений / возвращаемся в IDLE.
                        self._finish_voice_command()
                    else:
                        self._send_to_llm(text)
                        # Ответ LLM приходит асинхронно (on_complete/on_error);
                        # follow-up откроется там. Страховка на случай, если
                        # генерация зависнет:
                        self._cancel_followup_watchdog()
                        self._followup_watchdog = threading.Timer(
                            self.follow_up_timeout + 90.0, self._finish_voice_command
                        )
                        self._followup_watchdog.daemon = True
                        self._followup_watchdog.start()
                except Exception:  # noqa: BLE001
                    # Ошибка доставки результата (UI/TTS/LLM) НЕ должна
                    # убивать поток-потребитель: иначе ассистент навсегда
                    # перестанет реагировать на последующие команды.
                    import traceback
                    log(f"[Controller] Доставка результата: {traceback.format_exc()}")
                    self._transition_state("ERROR")
            finally:
                try:
                    self._cmd_queue.task_done()
                except Exception:  # noqa: BLE001
                    pass

    _STOP_TRIGGERS = ("молчи", "замолчи", "тихо", "хватит", "стоп", "хватит говорить",
                     "прекрати", "заткнись")

    def _is_stop_command(self, text: str) -> bool:
        """True, если управляющая фраза просит Зевса замолчать/прекратить."""
        if not text:
            return False
        import re
        t = text.lower().strip()
        return any(re.search(rf"(?<!\w){re.escape(tr)}(?!\w)", t)
                   for tr in self._STOP_TRIGGERS)

    def _handle_stop_command(self, text: str):
        """Немедленно прерывает озвучку и возвращает Зевса в ожидание «Зевс»."""
        print(f"[Actions] Команда прерывания: '{text}'. Возврат в сон.")
        # Экстренно останавливаем воспроизведение (Piper / любой движок)
        try:
            if hasattr(self.audio, "stop_speaking"):
                self.audio.stop_speaking()
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] не остановил озвучку: {exc}")
        try:
            from modules.tts_piper import set_stop
            set_stop()
        except Exception as exc:  # noqa: BLE001
            log(f"[Controller] не остановил Piper: {exc}")
        # Сбрасываем состояние сразу (без ожидания таймеров)
        self._pending_follow_up = False
        self._cancel_followup_watchdog()
        self._cancel_follow_up_timer()
        self._cancel_idle_return_timer()
        self._cancel_session_retry()
        self._after_speech = None
        self._transition_state("IDLE", force=True)
        self._set_status("Ожидание «Зевс»...")
        self._switch_audio_mode("wake")

    def _on_voice_input(self, text: str):
        """Обработчик голосового ввода (конечный автомат IDLE/LISTENING/PROCESSING).

        Различает служебные события аудио-движка и обычную распознанную
        фразу, направляя их по соответствующей ветке конечного автомата.
        """
        # Служебные события аудио-движка
        if text == "__WAKE__":
            self._on_wake_word()
            return
        if text == "__SLEEP_OFF__":
            self.set_active(False)
            return
        if text == "__SLEEP_ON__":
            self.set_active(True)
            return

        # Команда «молчи / стоп / хватит» — немедленное прерывание (ДО очереди)
        if self._is_stop_command(text):
            self._handle_stop_command(text)
            return

        # Уведомляем UI о распознанной фразе
        if self.on_voice_command:
            self.on_voice_command(text)

        # В режиме «слушателя команд» обрабатываем команду через FSM
        if self.state in ("LISTENING", "PROCESSING"):
            self._on_command_heard(text)
            return

        # Прочий голосовой ввод (вне автоматического цикла) — обычный путь
        self.process_text(text, source="voice")

    # ------------------------------------------------------------------
    # Конечный автомат голосового потока
    # ------------------------------------------------------------------
    def _transition_state(self, state: str, *, force: bool = False):
        """Переводит конечный автомат и тот же ZeusStateController, что у UI.

        Пока идёт озвучка, переход в IDLE/LISTENING откладывается:
        голограмма остаётся в SPEAKING, пока движок не закончит фразу.
        """
        audio = getattr(self, "audio", None)
        speaking = bool(getattr(audio, "is_speaking", False))
        if speaking and not force and state != "SPEAKING":
            self._after_speech = state
            if self.state == "SPEAKING":
                return
            state = "SPEAKING"
        if state == self.state and not force:
            sc = getattr(self, "state_controller", None)
            if sc is not None:
                sc.apply_voice(state)
            return
        self.state = state
        sc = getattr(self, "state_controller", None)
        if sc is not None:
            sc.apply_voice(state)
        if getattr(self, "on_state", None):
            try:
                self.on_state(state)
            except Exception as exc:  # noqa: BLE001
                log(f"[Controller] on_state: {exc}")

    def _on_speaking_changed(self, speaking: bool) -> None:
        """Движок TTS включил или выключил речь — общее состояние SPEAKING."""
        if speaking:
            if self.state != "SPEAKING":
                self._after_speech = self.state
                self._transition_state("SPEAKING", force=True)
            return
        nxt = self._after_speech
        self._after_speech = None
        if self.state == "SPEAKING" and nxt and nxt != "SPEAKING":
            self._transition_state(nxt, force=True)

    def _speak(self, text: str) -> None:
        """Ставит фразу в очередь озвучки. SPEAKING выставляет движок TTS."""
        if text:
            self.audio.speak(text)

    def _on_wake_word(self):
        """Wake Word «Зевс»: IDLE -> LISTENING («Да, сэр?»).

        Воспроизводит фразу подтверждения, SFX-эффект активации и
        переключает микрофон на полное распознавание команды.
        """
        if not self.is_active or self.state != "IDLE":
            return
        # Запускаем гарантированное окно удержания активного режима
        import time as _t
        self._active_hold_until = _t.time() + self._active_hold_timeout
        self._session_active_until = _t.time() + self._min_active_session
        # Авто-возврат в IDLE, если команда не продиктована за hold_timeout
        self._cancel_idle_return_timer()
        self._idle_return_timer = threading.Timer(
            self._active_hold_timeout, self._reset_to_idle
        )
        self._idle_return_timer.daemon = True
        self._idle_return_timer.start()
        self._transition_state("LISTENING")
        self._set_status("Да, сэр?")
        sfx.play_activation()
        self._speak("Да, сэр?")
        # Микрофон -> полное распознавание команды
        self._switch_audio_mode("command")
        self._log_session("WAKE", "Ключевое слово «Зевс» распознано", source="voice")

    def _on_command_heard(self, text: str):
        """Распознана команда: -> PROCESSING («Выполняю.», исполнение)

        После исполнения, если включён контекстный диалог, помощник не
        возвращается сразу в IDLE, а открывает короткое «окно» (5–10 c),
        в котором можно задавать уточнения БЕЗ повторного слова «Зевс».

        Холд активного режима снимается здесь: как только пользователь
        продиктовал саму команду (эхо «да сэр?» уже отфильтровано выше),
        возврат в IDLE/Wake Word после выполнения больше не блокируется.
        """
        # Команда распознана — снимаем защиту от преждевременного засыпания
        self._active_hold_until = 0.0
        # …но продлеваем минимальную «активную сессию», чтобы после первой же
        # команды не уйти в спокойный режим мгновенно (защитная пауза).
        self._session_active_until = time.time() + self._min_active_session
        self._cancel_follow_up_timer()
        self._transition_state("PROCESSING")
        self._speak("Выполняю.")
        # Окно уточнений откроется ПОСЛЕ фактического исполнения команды —
        # см. _finish_voice_command(): из _command_loop (системная команда)
        # или из _on_chat_complete/_on_chat_error (ответ LLM).
        self._pending_follow_up = True
        self.execute_command(text)

    def _cancel_followup_watchdog(self):
        """Отменяет страховочный таймер завершения голосовой команды."""
        timer = getattr(self, "_followup_watchdog", None)
        if timer is not None:
            timer.cancel()
            self._followup_watchdog = None

    def _finish_voice_command(self):
        """Завершает голосовую команду: окно уточнений или возврат в IDLE.

        Вызывается ПОСЛЕ фактического исполнения команды: из _command_loop
        (системная команда исполнена), из _on_chat_complete/_on_chat_error
        (ответ LLM получен/ошибка) либо из страховочного watchdog, если
        генерация зависла. Идемпотентна: повторный вызов — no-op.
        """
        if not getattr(self, "_pending_follow_up", False):
            return
        self._pending_follow_up = False
        self._cancel_followup_watchdog()
        if self.follow_up_enabled and self.is_active:
            self._enter_follow_up()
        else:
            self._reset_to_idle()

    def _enter_follow_up(self):
        """Открывает «окно диалога»: микрофон остаётся в режиме команды.

        Если в течение follow_up_timeout секунд пользователь скажет ещё
        что-то — это воспримется как уточнение (без «Зевс»). По таймауту
        помощник возвращается в IDLE (ожидание «Зевс»).
        """
        self._cancel_follow_up_timer()
        # Если за время обработки кто-то вернул в wake-режим — включаем command
        if self.state == "IDLE":
            self._switch_audio_mode("command")
        self._transition_state("LISTENING")
        self._set_status(f"Скажите уточнение ({self.follow_up_timeout} c)...")
        self._log_session("FOLLOW_UP", "Открыто окно контекстного диалога", source="system")
        self._follow_up_timer = threading.Timer(
            self.follow_up_timeout, self._reset_to_idle
        )
        self._follow_up_timer.daemon = True
        self._follow_up_timer.start()

    def _cancel_follow_up_timer(self):
        """Отменяет таймеры возврата в IDLE (follow-up, idle-return, session)."""
        self._cancel_session_retry()
        self._cancel_idle_return_timer()
        timer = getattr(self, "_follow_up_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
            self._follow_up_timer = None

    def _cancel_idle_return_timer(self):
        """Отменяет таймер авто-возврата в IDLE (после «Зевс»)."""
        timer = getattr(self, "_idle_return_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
            self._idle_return_timer = None

    def _cancel_session_retry(self):
        """Отменяет отложенный повторный возврат в IDLE."""
        timer = getattr(self, "_session_retry_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._session_retry_timer = None

    def _arm_session_retry(self, now: float):
        """Планирует повторный _reset_to_idle после окончании минимальной сессии."""
        delay = max(0.2, self._session_active_until - now)
        self._cancel_session_retry()
        self._session_retry_timer = threading.Timer(delay, self._reset_to_idle)
        self._session_retry_timer.daemon = True
        self._session_retry_timer.start()

    def execute_action_async(self, action_func, *args, **kwargs):
        """Запускает выполнение команды/действия в фоновом пуле потоков.

        Универсальная обёртка (по ТЗ): любые внешние программы, поиск и
        системные вызовы выполняются в рабочем пуле, поэтому главный поток
        отрисовки Flet никогда не блокируется и плашка «Working...» не
        появляется. Ошибки изолируются внутри задачи.
        """
        def worker():
            try:
                action_func(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                print(f"[Actions Error] Ошибка выполнения: {e}")
                import traceback
                traceback.print_exc()

        try:
            self._executor.submit(worker)
        except Exception:
            threading.Thread(target=worker, daemon=True, name="zeus-action-async").start()

    def execute_command(self, text: str):
        """Ставит команду в фоновую очередь и сразу возвращается.

        Фактическое исполнение (actions/Ollama) происходит в потоке
        zeus-commands (_command_loop), поэтому ни UI, ни аудио-поток
        не блокируются на время запуска игр/запросов к LLM.
        """
        self.process_text(text, source="voice")

    def _reset_to_idle(self):
        """Принудительный возврат в IDLE: микрофон слушает только «Зевс».

        Внутри окна активного удержания (после «Зевс») возврат блокируется —
        пользователь должен успеть продиктовать команду.
        """
        import time as _t
        now = _t.time()
        if now < self._active_hold_until or now < self._session_active_until:
            self._set_status("Слушаю команду...")
            # Сессия ещё «молодая» — повторяем возврат через короткий таймер,
            # чтобы не «зависнуть» слушать команду навсегда после удержания.
            self._arm_session_retry(now)
            return
        self._cancel_session_retry()
        self._cancel_follow_up_timer()
        self._transition_state("IDLE")
        self._set_status("Ожидание «Зевс»...")
        self._switch_audio_mode("wake")

    def _switch_audio_mode(self, mode: str):
        """Переключает микрофон асинхронно через рабочий пул потоков.

        Защиты от гонок и преждевременного возврата в Wake Word:
          1. Переключение выполняется с небольшой задержкой, чтобы текущий
             поток (прослушивание/Wake Word) успел освободить микрофон.
          2. self._audio_lock сериализует переключения между потоками.
          3. Генераторный счётчик self._switch_seq: если за время задержки
             было запрошено более новое переключение, устаревшее отбрасывается.
             Это не даёт «повисшему» wake-переключателю сбросить свежий
             command-режим сразу после «Да, сэр?».
        """
        self._switch_seq += 1
        my_seq = self._switch_seq

        def _worker():
            with self._audio_lock:
                # Сначала базовая задержка на освобождение микрофона текущим потоком
                time.sleep(0.05)
                # Устаревшее переключение или закрытие приложения — пропускаем
                if my_seq != self._switch_seq or self._is_cleaned_up:
                    return
                if mode == "wake":
                    self.audio.start_wake_mode()
                elif mode == "command":
                    self.audio.start_command_mode()

        try:
            self._executor.submit(_worker)
        except Exception:
            threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Системные команды
    # ------------------------------------------------------------------
    def _handle_system_command(self, result: dict[str, Any]):
        """Обрабатывает результат системной команды."""
        success = result.get("success", False)
        message = result.get("message", "Неизвестная команда, сэр.")
        action = result.get("action", "unknown")

        # Уведомляем UI
        if self.on_system_command:
            self.on_system_command(result)

        # Озвучиваем ответ
        if message:
            self._speak(message)

        # SFX: звук успеха/ошибки в стиле «Железного человека»
        if success:
            sfx.play_success()
        else:
            sfx.play_error()

        self._set_status(message)
        self._log_session("SYSTEM", message, source="system", success=success, action=action)

        # Специфичная обработка для некоторых действий
        if action == "drive_scan_started":
            # Запускаем мониторинг сканирования флешки
            drive = result.get("drive", "")
            if drive:
                self._start_drive_scan_monitor(drive)

    def _system_response(self, result: dict[str, Any], speak: bool = True):
        """Отправляет системный ответ в UI.

        Args:
            result: словарь с результатом (message, action, ...)
            speak: если True — произносит message через TTS
        """
        if self.on_system_command:
            self.on_system_command(result)

        message = result.get("message", "")
        if message and speak:
            self._speak(message)
        self._set_status(message)

    # ------------------------------------------------------------------
    # Отправка в LLM (Chat Engine)
    # ------------------------------------------------------------------
    def _send_to_llm(self, text: str):
        """Отправляет текст в Ollama для генерации ответа."""
        if self.chat.is_busy():
            self._set_status("Зевс уже думает...")
            # Завершаем голосовую команду сразу: не держим FSM в PROCESSING,
            # пока отвечает предыдущий (не наш) запрос.
            self._finish_voice_command()
            return

        self._set_status("Зевс думает...")

        # Уведомляем UI
        if self.on_chat_start:
            self.on_chat_start(text)

        # Отправляем в LLM
        self.chat.ask(text)

    def _on_chat_token(self, token: str):
        """Обработчик нового токена от LLM."""
        if self.on_chat_token:
            self.on_chat_token(token)

    def _on_chat_complete(self, full_text: str):
        """Обработчик завершения генерации LLM."""
        self._set_status("Готов к работе")

        if self.on_chat_complete:
            self.on_chat_complete(full_text)

        # Озвучиваем ответ и проигрываем звук успеха
        self._speak(full_text)
        sfx.play_success()
        self._log_session("LLM_OK", full_text[:200], source="llm", action="chat")
        # Ответ LLM получен и озвучен — только теперь окно уточнений / IDLE.
        self._finish_voice_command()

    def _on_chat_error(self, error: str):
        """Обработчик ошибки LLM."""
        log(f"[Controller] ошибка LLM: {error}")
        if self.state == "PROCESSING":
            self._transition_state("ERROR")
        self._set_status(f"Ошибка: {error}")
        sfx.play_error()

        if self.on_chat_error:
            self.on_chat_error(error)
        self._log_session("LLM_ERR", error, source="llm", success=False)
        # Ошибка генерации тоже завершает голосовую команду.
        self._finish_voice_command()

    # ------------------------------------------------------------------
    # Мониторинг сканирования флешек
    # ------------------------------------------------------------------
    def _start_drive_scan_monitor(self, drive: str):
        """Мониторит завершение сканирования флешки."""
        def _monitor():
            for _ in range(120):
                if self._is_cleaned_up:
                    return
                res = getattr(actions.confirm_drive_scan, "_last_result", None)
                if res is not None and res.get("drive") == drive:
                    added = res.get("added", 0)
                    if added > 0:
                        msg = (
                            f"Готово, сэр. На {drive} найдено и добавлено "
                            f"{added} программ(ы) в индекс как внешние."
                        )
                    else:
                        msg = f"На {drive} не найдено исполняемых файлов, сэр."
                    self._set_status(msg)
                    self._speak(msg)
                    if self.on_system_command:
                        self.on_system_command({
                            "success": True,
                            "message": msg,
                            "action": "drive_scan_done",
                            "drive": drive,
                            "added": added,
                        })
                    actions.confirm_drive_scan._last_result = None
                    return
                threading.Event().wait(1)

            timeout_msg = f"Не удалось завершить сканирование {drive} вовремя, сэр."
            self._set_status(timeout_msg)

        try:
            self._executor.submit(_monitor)
        except Exception:
            threading.Thread(target=_monitor, daemon=True).start()

    # ------------------------------------------------------------------
    # Управление голосом
    # ------------------------------------------------------------------
    def start_model_loader(self, on_progress=None):
        """Запускает загрузку Vosk/Piper и включает Wake Word после неё."""
        def _complete(success: bool):
            if self.is_active:
                self.start_voice()
            self._set_status("Модели готовы" if success else "Модели загружены частично")

        self.audio.load_models_background(on_progress=on_progress, on_complete=_complete)

    def start_voice(self):
        """Запускает фоновое прослушивание (IDLE — только Wake Word «Зевс»)."""
        if not self.is_active:
            return
        self._reset_to_idle()

    def stop_voice(self):
        """Останавливает фоновое прослушивание.

        stop_listening() содержит join до 3 с — вызываем его в пуле потоков,
        чтобы переключатель в UI не подвешивал Flet.
        """
        self._cancel_follow_up_timer()
        self._pending_follow_up = False
        self._cancel_followup_watchdog()
        try:
            self._executor.submit(self.audio.stop_listening)
        except Exception:
            threading.Thread(
                target=self.audio.stop_listening,
                daemon=True,
                name="zeus-audio-stop",
            ).start()
        self._transition_state("IDLE")

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

    def listen_once(self) -> str | None:
        """Однократное распознавание речи (для кнопки)."""
        return self.audio.listen_once()

    def speak(self, text: str):
        """Произносит текст."""
        self._speak(text)

    def force_activate(self):
        """Принудительный вызов ассистента (для глобальной горячей клавиши).

        Дублирует голосовую активацию по ключевому слову «Зевс»: будит
        ассистента из спящего режима и переводит микрофон в режим
        прослушивания команды («Да, сэр?»).
        """
        # Если в спящем режиме — сначала активируем
        if not self.is_active:
            self.set_active(True, confirm=False)

        # Если в «ожидании» — запускаем прослушивание команды, как по Wake Word
        if self.state == "IDLE":
            self._on_wake_word()
        else:
            self._set_status("Да, сэр?")
            self._speak("Да, сэр?")

    def deactivate_from_hotkey(self):
        """Принудительное отключение ассистента по горячей клавише."""
        if self.is_active:
            self.set_active(False, confirm=False)
        else:
            self._set_status("Зевс уже отключён, сэр.")

    # ------------------------------------------------------------------
    # Управление чатом
    # ------------------------------------------------------------------
    def new_chat(self):
        """Очищает историю диалога."""
        self.chat.new_chat()

    def set_model(self, model: str):
        """Устанавливает модель Ollama."""
        self.chat.set_model(model)
        self.update_settings("ai_settings", "model", model)

    def set_system_prompt(self, prompt: str):
        """Устанавливает системный промпт."""
        self.chat.set_system_prompt(prompt)
        self.update_settings("ai_settings", "system_prompt", prompt)

    def set_temperature(self, temp: float):
        """Устанавливает температуру."""
        self.chat.set_temperature(temp)
        self.update_settings("ai_settings", "temperature", temp)

    # ------------------------------------------------------------------
    # Статус
    # ------------------------------------------------------------------
    def _set_status(self, text: str):
        """Обновляет статус в UI, если колбэк зарегистрирован."""
        if hasattr(self, 'on_status') and self.on_status:
            try:
                self.on_status(text)
            except Exception:
                pass

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
        except Exception:
            # Логирование не должно нарушать работу ассистента
            pass

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