# -*- coding: utf-8 -*-
"""Голосовой цикл: очередь команд, автомат, ответы чата."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any

from core import actions, config, file_scanner
from core.command_router import router
from core.logging_service import log
from modules import sfx

_WAKE_FORMS = {
    "зевс", "зевса", "зевсу", "зевсом", "зевсе",
    "зеве", "зевес", "зевеса", "зэвса", "зеуса",
}
_WAKE_FILL = {"а", "ну", "да", "сэр", "сер", "сир"}


def _is_wake_phrase(text: str) -> bool:
    """True, если фраза — только обращение «Зевс» (в том числе «зевса»)."""
    import re
    words = re.sub(r"[^а-яёa-z0-9]+", " ", (text or "").lower()).split()
    words = [w for w in words if w not in _WAKE_FILL]
    return bool(words) and all(w in _WAKE_FORMS for w in words)


class ZeusVoiceMixin:
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

    def _route_pending(self, text: str) -> bool:
        """«Да» и «нет» отвечают агенту и не идут в системные команды.

        Любая другая фраза снимает ожидание: иначе следующее случайное «да»
        выполнило бы старый клик или запуск.
        """
        from core.agent_harness import PendingAction, classify_confirmation

        pending = getattr(self.chat, "_pending", None)
        if not isinstance(pending, PendingAction):
            return False
        if classify_confirmation(text) is None:
            self.chat.cancel_pending()
            log("[Controller] подтверждение снято: пришла другая команда")
            return False
        if self._send_to_llm(text):
            self._arm_llm_watchdog()
        return True

    def _arm_llm_watchdog(self) -> None:
        """Страховка: если ответ модели завис, голос всё равно вернётся в ожидание."""
        self._cancel_followup_watchdog()
        self._followup_watchdog = threading.Timer(
            self.follow_up_timeout + 90.0, self._finish_voice_command
        )
        self._followup_watchdog.daemon = True
        self._followup_watchdog.start()

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
                    if self._route_pending(text):
                        continue
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
                        if self._send_to_llm(text):
                            self._arm_llm_watchdog()
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
        if getattr(self, "chat", None) is not None:
            self.chat.cancel_pending()
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

        # Одно только «Зевс» / «зевса» будит. Обычная фраза («молодец»)
        # идёт в разговор с моделью.
        if _is_wake_phrase(text):
            if self.state == "IDLE":
                self._on_wake_word()
            return

        # В режиме «слушателя команд» обрабатываем команду через FSM.
        # SPEAKING тоже: «Да, сэр?» ещё звучит, а фраза пользователя уже пришла.
        if self.state in ("LISTENING", "PROCESSING", "SPEAKING"):
            self._on_command_heard(text)
            return

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
                    remaining = getattr(self.audio, "_command_hold_until", 0.0) - time.monotonic()
                    if remaining > 0:
                        # Командное окно только открылось. Повторить возврат
                        # к «Зевс», когда оно истечёт, а не обрывать его.
                        retry = threading.Timer(
                            remaining + 0.15,
                            lambda: self._switch_audio_mode("wake"),
                        )
                        retry.daemon = True
                        retry.start()
                        return
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
    def _send_to_llm(self, text: str) -> bool:
        """Отправляет текст в Ollama. False — модель ещё занята прошлым запросом."""
        if self.chat.is_busy():
            self._queued_utterance = text
            self._set_status("Зевс ещё отвечает, фраза подождёт...")
            self._finish_voice_command()
            return False

        self._set_status("Зевс думает...")

        # Уведомляем UI
        if self.on_chat_start:
            self.on_chat_start(text)

        # Отправляем в LLM
        self.chat.ask(text)
        return True

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
        queued = getattr(self, "_queued_utterance", None)
        self._queued_utterance = None
        if queued:
            self.process_text(queued, source="voice")

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
        queued = getattr(self, "_queued_utterance", None)
        self._queued_utterance = None
        if queued:
            self.process_text(queued, source="voice")

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


