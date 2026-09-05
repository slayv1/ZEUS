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
from core.command_router import router


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


# Регистрация на уровне модуля: достаточно импортировать controller,
# чтобы команды были доступны в диспетчере (идемпотентно).
_register_default_commands()


def execute_command_safely(command_func, *args, **kwargs):
    """Универсальная обёртка: запускает любую задачу/системный вызов в фоне.

    Используется для обработчиков событий Flet (нажатия, горячие клавиши,
    коллбэки), чтобы главный поток интерфейса мгновенно завершал обработку
    события и не показывал плашку «Working…». Daemon-поток не блокирует
    завершение приложения.
    """
    def worker():
        try:
            command_func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"[Error] Ошибка при выполнении команды: {exc}")
            traceback.print_exc()

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

        # Инициализация движков
        voice = self.settings.get("voice_settings", {})
        self.audio = AudioEngine(
            language=voice.get("language", "ru-RU"),
            mic_index=voice.get("mic_index"),
        )
        self.chat = ChatEngine()

        # Регистрируем голосовое уведомление о системных ошибках (Crash Handler).
        # При любом непредвиденном исключении Зевс мягко сообщит: «Произошла
        # системная ошибка, сэр», а не аварийно закроет окно.
        try:
            from core import crash_handler
            crash_handler.set_crash_notifier(self.audio.speak)
        except Exception:  # noqa: BLE001
            pass

        # Применяем настройки к движкам
        self._apply_settings()

        # Watchdog: динамическая перезагрузка commands.json при его изменении.
        # Наблюдатель запускается прямо в инициализаторе контроллера (ядро);
        # при сохранении файла вызывается actions.reload_commands(). UI позже
        # подключает свой колбэк через start_watching(callback=...) — он
        # применяется даже при уже запущенном наблюдателе.
        try:
            from core.file_watcher import get_watcher
            watcher = get_watcher()
            watcher.register_reload("commands.json", actions.reload_commands)
            watcher.start()
        except Exception:  # noqa: BLE001
            pass

        # Фоновый авто-сканер новых игр/приложений (рабочий стол + Steam).
        # Периодически дописывает найденное в commands.json; watchdog тут же
        # перечитывает файл, и команды обновляются без перезапуска.
        try:
            if self.settings.get("app_settings", {}).get("auto_scan", True):
                from core import auto_scanner
                auto_scanner.auto_scan_in_background(interval=300)
        except Exception:  # noqa: BLE001
            pass

        # Спящий режим
        self.is_active = True

        # Конечный автомат голосового потока
        self.state = "IDLE"  # IDLE, LISTENING, PROCESSING

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

    def get_settings(self) -> dict:
        """Возвращает текущие настройки."""
        return self.settings

    def update_settings(self, section: str, key: str, value: Any) -> bool:
        """Обновляет конкретную настройку и сохраняет в JSON.

        Args:
            section: раздел ('ai_settings', 'voice_settings', 'app_settings')
            key: ключ внутри раздела
            value: новое значение

        Returns:
            True при успешном сохранении
        """
        if section in self.settings and key in self.settings[section]:
            self.settings[section][key] = value
            saved = self.settings_manager.set(section, key, value)

            # Мгновенное применение критических параметров
            if section == "ai_settings":
                if key == "model":
                    self.chat.set_model(value)
                elif key == "temperature":
                    self.chat.set_temperature(value)
                elif key == "system_prompt":
                    self.chat.set_system_prompt(value)
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
            elif section == "app_settings" and key == "auto_listening":
                if value and not self.is_active:
                    self.set_active(True)
                elif not value and self.is_active:
                    self.set_active(False)

            return saved
        return False

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
            # (_listen_thread.join(timeout=3)) — выполняем его в фоновом
            # потоке, чтобы не блокировать поток Flet (кнопка «Отключиться»)
            # и коллбэки аудио-движка.
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
            self.audio.speak("Да, сэр.")
        elif not active and was_active and confirm:
            # Короткое подтверждение отключения
            self.audio.speak("Есть, сэр.")

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
                    command_result = actions.execute_command(text)
                except Exception as e:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()
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
                    traceback.print_exc()
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
        except Exception:  # noqa: BLE001
            pass
        try:
            from modules.tts_piper import set_stop
            set_stop()
        except Exception:  # noqa: BLE001
            pass
        # Сбрасываем состояние сразу (без ожидания таймеров)
        self._pending_follow_up = False
        self._cancel_followup_watchdog()
        self._cancel_follow_up_timer()
        self._cancel_idle_return_timer()
        self._cancel_session_retry()
        self._transition_state("IDLE")
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
    def _transition_state(self, state: str):
        """Переводит конечный автомат в новое состояние и уведомляет UI."""
        self.state = state
        if getattr(self, "on_state", None):
            try:
                self.on_state(state)
            except Exception:
                pass

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
        self.audio.speak("Да, сэр?")
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
        self.audio.speak("Выполняю.")
        # Окно уточнений откроется ПОСЛЕ фактического исполнения команды —
        # см. _finish_voice_command(): из _command_loop (системная команда)
        # или из _on_chat_complete/_on_chat_error (ответ LLM).
        self._pending_follow_up = True
        self.execute_action_async(self.execute_command, text)

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
        """Запускает выполнение команды/действия в фоновом потоке.

        Универсальная обёртка (по ТЗ): любые внешние программы, поиск и
        системные вызовы выполняются в daemon-потоке, поэтому главный поток
        отрисовки Flet никогда не блокируется и плашка «Working...» не
        появляется. Ошибки изолируются внутри потока.
        """
        def worker():
            try:
                action_func(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                print(f"[Actions Error] Ошибка выполнения: {e}")
                import traceback
                traceback.print_exc()

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
        """Переключает микрофон асинхронно (в отдельном потоке).

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
                # Устаревшее переключение (было запрошено более новое) — пропускаем
                if my_seq != self._switch_seq:
                    return
                if mode == "wake":
                    self.audio.start_wake_mode()
                elif mode == "command":
                    self.audio.start_command_mode()

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
            self.audio.speak(message)

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
            self.audio.speak(message)
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
        self.audio.speak(full_text)
        sfx.play_success()
        self._log_session("LLM_OK", full_text[:200], source="llm", action="chat")
        # Ответ LLM получен и озвучен — только теперь окно уточнений / IDLE.
        self._finish_voice_command()

    def _on_chat_error(self, error: str):
        """Обработчик ошибки LLM."""
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
                    self.audio.speak(msg)
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

        stop_listening() содержит join до 3 с — вызываем его в фоновом
        потоке (daemon), чтобы переключатель в UI не подвешивал Flet.
        """
        self._cancel_follow_up_timer()
        self._pending_follow_up = False
        self._cancel_followup_watchdog()
        threading.Thread(
            target=self.audio.stop_listening,
            daemon=True,
            name="zeus-audio-stop",
        ).start()
        self._transition_state("IDLE")

    def listen_once(self) -> str | None:
        """Однократное распознавание речи (для кнопки)."""
        return self.audio.listen_once()

    def speak(self, text: str):
        """Произносит текст."""
        self.audio.speak(text)

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
            self.audio.speak("Да, сэр?")

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
            import json
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
        """Освобождает ресурсы."""
        self._cancel_follow_up_timer()
        self.audio.cleanup()
