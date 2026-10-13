# -*- coding: utf-8 -*-
"""Прослушивание: wake word, командный режим, распознавание."""
from __future__ import annotations

import os
import re
import threading
import time

import numpy as np
import speech_recognition as sr

from modules.wake_word import WakeWordDetector, resolve_vosk_model_path

# Обрывки Vosk («а», «ну», «вот») не считаются командой.
_NOISE_TAIL_WORDS = {
    "а", "уж", "ну", "и", "но", "вот", "ага", "угу", "хм", "э", "хе", "о",
    "у", "аа", "ээ", "не", "же", "бы", "ли", "да", "так", "ведь", "ах", "эх",
}

_log_audio_error = lambda text: None
apply_microphone_gain = lambda chunk, gain=1.5: chunk
ModelLoaderThread = None
AudioListenerThread = None


def bind_listen() -> None:
    """Подключает помощники из audio_engine после его загрузки."""
    global _log_audio_error, apply_microphone_gain, ModelLoaderThread, AudioListenerThread
    from modules import audio_engine as ae
    _log_audio_error = ae._log_audio_error
    apply_microphone_gain = ae.apply_microphone_gain
    ModelLoaderThread = ae.ModelLoaderThread
    AudioListenerThread = ae.AudioListenerThread


class AudioListenMixin:
    def start_listening(self):
        """Запускает прослушивание в зависимости от режима.

        - Active: полное распознавание речи (Google Speech)
        - Sleep:  лёгкий Wake Word детектор (Vosk), если доступен;
                 иначе микрофон полностью отключается
        """
        if not self._mic_available:
            print("[AudioEngine] Микрофон недоступен, прослушивание не запущено")
            return

        if self.is_active:
            self._start_full_listen()
        else:
            self._start_wake_listen()

    def start_wake_mode(self):
        """Переключает микрофон ТОЛЬКО на Wake Word детектор (IDLE-режим).

        Используется конечным автоматом контроллера: в «ожидании» слушается
        лишь ключевое слово «Зевс» (Vosk), а тяжёлое распознавание команды
        не запускается.
        """
        if not self._mic_available:
            print("[AudioEngine] Микрофон недоступен — Wake Word не запущен")
            return
        # Останавливаем полное распознавание команды, если оно шло.
        self._listen_running = False
        th = self._listen_thread
        # Не пытаемся join'ить самого себя (внутри цикла прослушивания).
        if th is not None and th is not threading.current_thread():
            th.join(timeout=3)
        self._start_wake_listen()
        print("[AudioEngine] start_wake_mode: _start_wake_listen завершен, проверяем состояние wake детектора")
        if self.wake.available:
            print(f"[AudioEngine] WakeWordDetector доступен: {self.wake.available}")
            print(f"[AudioEngine] Wakе thread запущен: {self.wake._running if hasattr(self.wake, '_running') else 'N/A'}")
        else:
            print("[AudioEngine] WakeWordDetector НЕ доступен")

    def start_command_mode(self):
        """Переключает микрофон на полное распознавание команды (LISTENING).

        Останавливает Wake Word детектор (микрофон занят одним источником)
        и запускает тяжёлое распознавание команды.

        ВАЖНО: метод содержит потенциально долгие операции (wake.stop()
        с join, запуск потока распознавания, ленивую загрузку Vosk-модели)
        и должен вызываться из фонового потока. Контроллер делает это через
        _switch_audio_mode (zeus-поток) либо напрямую activate_command_
        mode_async() — главный поток Flet никогда не блокируется.
        """
        import time as _t
        # Дольше, чем гонка переключения, короче таймера возврата в ожидание (5 с).
        self._command_hold_until = _t.monotonic() + 4.0
        self.wake.stop()
        if self._mic_available:
            self._start_full_listen()

    def stop_wake_listener(self):
        """Публичный алиас: останавливает Wake Word детектор (см. wake.stop)."""
        try:
            self.wake.stop()
        except Exception as exc:  # noqa: BLE001
            _log_audio_error(f"не остановил wake-детектор: {exc}")

    def start_full_listening(self):
        """Публичный алиас: запускает полное распознавание команды."""
        if self._mic_available:
            self._start_full_listen()

    def activate_command_mode_async(self):
        """Асинхронный переход в командный режим (фоновый daemon-поток).

        Тяжёлая последовательность «остановка wake-детектора -> запуск
        полного прослушивания -> (ленивая) инициализация Vosk/TTS»
        выполняется вне вызывающего потока: переход мгновенный для UI.
        """
        threading.Thread(
            target=self.start_command_mode,
            daemon=True,
            name="zeus-cmd-mode",
        ).start()

    def prewarm_tts(self):
        """Фоново прогревает TTS (загрузка модели Piper при старте).

        Чтобы первая фраза «Да, сэр?» звучала мгновенно и инициализация
        синтеза не совпадала с моментом активации командного режима.
        """
        def _prewarm():
            try:
                from modules.tts_piper import prewarm
                prewarm()
            except Exception as e:  # noqa: BLE001
                print(f"[AudioEngine] Piper недоступен (фолбэк pyttsx3): {e}")

        threading.Thread(target=_prewarm, daemon=True, name="zeus-tts-prewarm").start()

    def load_models_background(self, on_progress=None, on_complete=None):
        """Загружает Vosk и Piper последовательно в отдельном daemon-потоке."""
        self._model_loader_thread = ModelLoaderThread(
            self, on_progress=on_progress, on_complete=on_complete
        )
        self._model_loader_thread.start()

    def _start_full_listen(self):
        """Запускает полное распознавание речи (тяжёлый Active-режим)."""
        if self._listen_running:
            return
        self._listen_running = True
        self._listen_thread = AudioListenerThread(self)
        self._listen_thread.start()
        print("[AudioEngine] Полное прослушивание запущено (Active)")

    def _start_wake_listen(self):
        """Запускает лёгкий Wake Word детектор (Sleep-режим)."""
        print("[AudioEngine] _start_wake_listen: запуск легкого Wake Word детектора")
        if self.wake.available:
            self.wake.set_mic_device(self._mic_device_index)
            # ТЗ (петля самосрабатывания): армируем cooldown перед стартом
            # детектора — первые 0.8 c после возврата в Sleep «Зевс» не
            # сработает повторно на переходный шум / эхо команды.
            try:
                self.wake.arm_cooldown(0.8)
            except Exception:  # noqa: BLE001
                pass
            self.wake.start(self._on_wake)
            print("[AudioEngine] Лёгкий Wake Word запущен (Sleep)")
        else:
            print("[AudioEngine] Wake Word недоступен — микрофон отключён (Sleep)")

    def stop_listening(self):
        """Останавливает ВСЁ прослушивание (полное и Wake Word)."""
        self._listen_running = False
        if self._listen_thread:
            self._listen_thread.join(timeout=3)
        self.wake.stop()
        print("[AudioEngine] Прослушивание остановлено")

    def _on_wake(self):
        """Callback от Wake Word детектора — пробуждаем «Слушателя».

        Передаём в контроллер служебное событие __WAKE__ (обнаружено
        ключевое слово «Зевс»), чтобы конечный автомат перевёл помощника
        из IDLE в LISTENING. Запускаем обработку в отдельном потоке,
        чтобы не вызывать wake.stop() изнутри потока самого детектора
        (избегаем гонки за микрофон и потенциального дедлока).
        """

        print("[AudioEngine] _on_wake: вызван callback от WakeWord детектора")

        def _activate():
            # ТЗ (петля самосрабатывания): короткая пауза при переключении
            # Sleep -> Active, чтобы переходный шум микрофона / хвост фразы
            # не был принят за новую команду сразу после активации.
            import time
            time.sleep(0.3)
            print("[AudioEngine] _on_wake: активируем командный режим")
            if self.on_command:
                self.on_command("__WAKE__")
        threading.Thread(target=_activate, daemon=True).start()

    def _make_gate(self):
        """Создаёт шумовой гейт для цикла прослушивания команды.

        Настройки читаются заново при каждом старте прослушивания, чтобы
        изменение порога в окне настроек применялось без перезапуска.
        Если модуль гейта по какой-то причине недоступен — возвращаем
        «всегда открытый» объект: аудио-тракт не должен падать из-за фильтра.
        """
        threshold, enabled = 30, True
        try:
            from core.config import load_settings
            vs = load_settings().get("voice_settings", {})
            threshold = int(vs.get(
                "command_energy_threshold",
                vs.get("wake_energy_threshold", 30),
            ))
            enabled = bool(vs.get("noise_gate_enabled", True))
        except Exception:  # noqa: BLE001 — defaults безопасны
            pass
        try:
            from core.audio_gate import NoiseGate
            return NoiseGate(threshold, enabled=enabled)
        except Exception:  # noqa: BLE001 — без гейта слушаем как раньше
            class _AlwaysOpen:
                passed = 0
                rejected = 0

                @staticmethod
                def allow(data, now=None):
                    return True

                @staticmethod
                def describe():
                    return "шумовой гейт недоступен"
            return _AlwaysOpen()

    @staticmethod
    def _has_partial(recognizer) -> bool:
        """True, если у Vosk уже есть незаконченная фраза (частичный результат).

        Нужен, чтобы шумовой гейт не обрывал начатую фразу: если распознаватель
        «услышал» начало слова, блоки подаём даже ниже порога. Любая ошибка
        трактуется как «фраза есть» (fail-open) — риск обрезанного слова хуже,
        чем лишние блоки шума в распознавателе.
        """
        try:
            import json as _json
            partial = _json.loads(recognizer.PartialResult()).get("partial", "")
            return bool(partial and partial.strip())
        except Exception:  # noqa: BLE001
            return True

    def _listen_loop(self):
        """Основной цикл прослушивания команды (работает в фоновом потоке).

        Захват — напрямую через sounddevice (НЕ требует PyAudio), распознавание —
        офлайн через Vosk (та же модель, что у Wake Word). Если Vosk недоступен —
        фолбэк на Google Speech (сеть). Полностью устраняет зависимость от PyAudio.

        Процесс: микрофон слушает поток, блоки подаются в KaldiRecognizer,
        при завершении фразы (AcceptWaveform=True) текст передаётся дальше.
        """
        sample_rate = 16000
        if not self._mic_available:
            print("[AudioEngine] Микрофон недоступен — прослушивание не запущено")
            self._listen_running = False
            return

        model, model_sr = self._get_vosk_model()
        if model is None:
            self._listen_google_fallback(sample_rate)
            return

        import json as _json
        import sounddevice as sd
        import vosk

        recognizer = vosk.KaldiRecognizer(model, model_sr)
        recognizer.SetWords(False)
        chunk = 4000  # ~250 мс при 16 кГц

        try:
            with sd.RawInputStream(
                samplerate=model_sr,
                blocksize=chunk,
                device=self._mic_device_index,
                dtype="int16",
                channels=1,
            ) as stream:
                print("[AudioEngine] Полное прослушивание (sounddevice+Vosk) запущено")
                # ТЗ (петля самосрабатывания): сброс входного буфера при
                # переходе Sleep -> Active — читаем и отбрасываем первые
                # ~0.5 c, чтобы хвост «Зевс» / переходный шум не попал в
                # KaldiRecognizer сразу после старта.
                import time as _wtime
                _wtime.sleep(0.3)
                recognizer.Reset()
                for _ in range(2):
                    try:
                        stream.read(chunk)
                    except Exception as exc:  # noqa: BLE001
                        _log_audio_error(f"Ошибка сброса буфера микрофона: {exc}")
                        break
                # Шумовой гейт (ТЗ v1.4 §2.2): гул кулеров, клики клавиатуры и
                # шорох стола раньше попадали прямо в KaldiRecognizer и рождали
                # «команды-призраки». Гейт адаптивный: порог = max(абсолютный
                # минимум, фон × 2), после речи остаётся открытым ещё hangover,
                # поэтому окончание фразы не обрезается.
                gate = self._make_gate()
                last_gate_log = 0.0
                print(f"[AUDIO] [GATE] шумовой гейт: "
                      f"{'вкл' if getattr(gate, 'enabled', True) else 'выкл'} "
                      f"порог={getattr(gate, 'threshold', 0)} "
                      f"(шум не пойдёт в распознаватель)")
                while self._listen_running:
                    try:
                        data, _ = stream.read(chunk)
                    except Exception as exc:
                        now_err = _wtime.monotonic()
                        if now_err - getattr(self, "_last_mic_err", 0.0) > 5.0:
                            self._last_mic_err = now_err
                            _log_audio_error(f"Ошибка чтения микрофона: {exc}")
                        continue
                    if data is None or len(data) == 0:
                        continue
                    data = apply_microphone_gain(data, self.mic_gain)
                    now = _wtime.monotonic()
                    if not gate.allow(data, now=now):
                        # Тишина/шум: в recognizer не идёт, но и фразу на середине
                        # не бросаем — если Vosk уже начал фразу, блок подаём.
                        if self._has_partial(recognizer):
                            if recognizer.AcceptWaveform(data):
                                res = _json.loads(recognizer.Result())
                                self._handle_listen_text((res.get("text") or "").strip())
                            continue
                        if now - last_gate_log > 5.0:
                            last_gate_log = now
                            print(f"[AUDIO] [GATE] шум отсечён: {gate.describe()} "
                                  f"(пропущено={gate.passed} отклонено={gate.rejected})")
                        continue
                    if recognizer.AcceptWaveform(data):
                        res = _json.loads(recognizer.Result())
                        self._handle_listen_text((res.get("text") or "").strip())
        except Exception as e:
            _log_audio_error(f"Ошибка цикла прослушивания (микрофон недоступен?): {e}")
            self._listen_running = False

    # Триггеры тишины: немедленное прерывание любых задач/озвучки.
    _STOP_TRIGGERS = ("молчи", "замолчи", "тихо", "хватит", "стоп",
                      "хватит говорить", "прекрати", "заткнись")

    def _is_stop_command(self, text: str) -> bool:
        """True, если управляющая фраза просит Зевса замолчать/прекратить."""
        if not text:
            return False
        t = text.lower().strip()
        return any(re.search(rf"(?<!\w){re.escape(tr)}(?!\w)", t)
                   for tr in self._STOP_TRIGGERS)

    def _handle_listen_text(self, text: str):
        """Обрабатывает распознанную фразу непрерывного прослушивания команды.

        Отбрасывает «эхо» собственного ответа TTS: если Зевс ещё говорит
        (is_speaking) или сразу после (echo_grace) — фраза не обрабатывается.
        """
        import time as _t
        if not text:
            return
        # === 2.1 Global Stop Filter: САМАЯ ПЕРВАЯ проверка (ТЗ v1.2) ===
        # Стоп-слово перехватывается до любых гейтов, команд, поиска и LLM:
        # мгновенно глушим Piper и уходим в сон.
        if self._is_stop_command(text):
            print(f"[StopTrigger] '{text}' — прерывание аудио и возврат в сон")
            self.stop_speaking()
            try:
                self._start_wake_listen()
            except Exception:  # noqa: BLE001
                pass
            return
        # Эхо-гейт: собственный голос из динамиков. ВАЖНО: Vosk склеивает
        # эхо «Да, сэр?» с командой пользователя («да сэр запусти теккен 8»).
        # Поэтому эхо-ПРЕФИКС снимается и остаток обрабатывается как команда;
        # отбрасывается только фраза, состоящая из чистого эха.
        if self.is_speaking or _t.time() < self._echo_grace_until:
            stripped = self._strip_echo_prefix(text)
            if not stripped or self._is_noise_tail(stripped) or stripped == text:
                # В окне эха: чистый эхо-фрагмент либо «хвост» обрывка —
                # не команда (критерий 3 ТЗ v1.2).
                if self._debug_mode:
                    print(f"[AudioEngine] Эхо отброшено: '{text}'")
                return
            if self._debug_mode:
                print(f"[AudioEngine] Эхо-префикс снят: '{text}' -> '{stripped}'")
            text = stripped
        # Мусорный «хвост» распознавания не считается командой и не сбрасывает
        # сессию — продолжаем слушать пользователя ещё пару секунд.
        if self._is_noise_tail(text):
            if self._debug_mode:
                print(f"[AudioEngine] Мусорный хвост проигнорирован: '{text}'")
            time.sleep(0.25)
            return
        # Cooldown: после принятой команды 2 с игнорируем ЛЮБЫЕ новые фразы —
        # эхо колонок и обрывки распознавания («это понятно», «парня») не
        # должны превращаться в фейковые команды (ТЗ «пауза-заглушка»).
        now = _t.time()
        if now < getattr(self, "_last_cmd_ts", 0.0) + 2.0:
            if self._debug_mode:
                print(f"[AudioEngine] Cooldown: '{text}' отброшено")
            return
        self._last_cmd_text = text
        self._last_cmd_ts = now
        print(f"[AudioEngine] Распознано: {text}")
        # Проверяем команды спящего режима
        if self._check_sleep_commands(text):
            return
        if self.on_command:
            self.on_command(text)

    @staticmethod
    def _strip_echo_prefix(text: str) -> str:
        """Снимает эхо-префиксы собственного TTS с начала фразы.

        Зевс говорит «Да, сэр?» / «Выполняю.» — микрофон слышит их вместе
        с командой пользователя: «да сэр запусти теккен 8». Убираем
        повторы префикса циклически; если осталась только команда —
        возвращаем её, иначе пустую строку (чистое эхо).
        """
        import re as _re
        t = (text or "").lower().strip(" ,.!?")
        pat = _re.compile(
            r"^(?:да|дай|ну|давай)?\s*"
            r"(?:сэр|сер|сир|сергей|зевс|зеве|выполняй|выполняют|выполняю)"
            r"\b[,.!?]?\s*",
        )
        prev = None
        while prev != t and t:
            prev = t
            t = pat.sub("", t).strip(" ,.!?")
        return t  # "" = вся фраза была чистым эхом

    def _is_noise_tail(self, text: str) -> bool:
        """True, если фраза — короткий/мусорный «хвост» распознавания.

        Например «а уж», «ну», «вот», «а» — обрывки Vosk не являются командой:
        они не должны сбрасывать сессию в спящий режим или запускать ложное
        исполнение. Возвращает True, чтобы фраза была проигнорирована.
        """
        t = (text or "").strip().lower()
        if not t:
            return True
        clean = re.sub(r"[^а-яёa-z0-9]+", "", t)
        if len(clean) < 3:
            return True
        words = [re.sub(r"[^а-яёa-z0-9]+", "", w) for w in t.split()]
        words = [w for w in words if w]
        if words and all(w in _NOISE_TAIL_WORDS for w in words):
            return True
        return False


    def _listen_google_fallback(self, sample_rate: int = 16000, window: int = 5):
        """Фолбэк непрерывного прослушивания через Google Speech (сеть).

        Используется только если Vosk недоступен. Захват — через sounddevice,
        окно записи распознаётся через Google Speech.
        """
        import io
        import wave

        import numpy as np
        import sounddevice as sd

        print("[AudioEngine] Полное прослушивание через Google (sounddevice) — Vosk недоступен")
        while self._listen_running:
            try:
                audio_data = sd.rec(
                    int(sample_rate * window),
                    samplerate=sample_rate,
                    channels=1,
                    dtype='int16',
                    device=self._mic_device_index,
                )
                sd.wait()
                audio_data = apply_microphone_gain(audio_data.tobytes(), self.mic_gain)
                samples = np.frombuffer(audio_data, dtype=np.int16)

                byte_io = io.BytesIO()
                with wave.open(byte_io, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wf.writeframes(samples.tobytes())
                byte_io.seek(0)
                audio = sr.AudioData(byte_io.read(), sample_rate, 2)

                try:
                    text = self.recognizer.recognize_google(audio, language=self.language)
                    self._handle_listen_text((text or "").strip())
                except sr.UnknownValueError:
                    pass
                except sr.RequestError as e:
                    _log_audio_error(f"Ошибка сервиса распознавания: {e}")
                except Exception as e:
                    _log_audio_error(f"Ошибка распознавания: {e}")
            except Exception as e:
                _log_audio_error(f"Ошибка прослушивания (фолбэк Google): {e}")
                if self._listen_running:
                    import time
                    time.sleep(0.5)

    # ------------------------------------------------------------------
    # Спящий режим
    # ------------------------------------------------------------------
    def _check_sleep_commands(self, text: str) -> bool:
        """Проверяет текст на команды 'включись/отключись'.

        Матчинг пословный (regex-границы слов), без ложных срабатываний
        на случайных подстроках. Раньше внешний any() содержал «сырой»
        элемент «зеве», который совпадал с любой фразой, где упоминается
        Зевс, и лишь маскировал реальную проверку внутри.

        Returns:
            True если это команда спящего режима (текст уже обработан)
        """
        text_lower = (text or "").lower().strip()
        if not text_lower:
            return False
        import re as _re

        def _has(word: str) -> bool:
            return bool(_re.search(rf"(?<!\w){_re.escape(word)}(?!\w)", text_lower))

        # Команды отключения
        if _has("отключись") or _has("усни"):
            self.is_active = False
            msg = "Зевс отключён, сэр. Чтобы включить, скажите «Зевс, включись»."
            print(f"[AudioEngine] {msg}")
            if self.on_command:
                # Отправляем как системное сообщение
                self.on_command("__SLEEP_OFF__")
            return True

        # Команды включения (только если сейчас спим — иначе это обычная фраза)
        if (_has("включись") or _has("проснись") or _has("вернись")) and not self.is_active:
            self.is_active = True
            msg = "Зевс активирован, сэр. Чем могу помочь?"
            print(f"[AudioEngine] {msg}")
            if self.on_command:
                self.on_command("__SLEEP_ON__")
            return True

        return False

    def set_active(self, active: bool):
        """Включает/выключает спящий режим.

        При выключении (Sleep) тяжёлое распознавание останавливается,
        а микрофон переключается на лёгкий Wake Word детектор (Vosk) —
        ресурсы процессора освобождаются, «каждый шорох» не слушается.
        Если Wake Word недоступен — микрофон полностью отключается.
        """
        if active == self.is_active:
            # Повторная установка того же режима — перезапускаем циклы
            # для гарантии консистентного состояния.
            self._restart_loops()
            return

        self.is_active = active

        if active:
            # Выход из Sleep: останавливаем Wake Word, запускаем полное
            self.wake.stop()
            if self._mic_available:
                self._start_full_listen()
        else:
            # Вход в Sleep: останавливаем полное, запускаем Wake Word
            self._listen_running = False
            if self._listen_thread:
                self._listen_thread.join(timeout=3)
            self._start_wake_listen()

        status = "активирован" if active else "отключён (лёгкий Sleep)"
        print(f"[AudioEngine] Зевс {status}")

    def _restart_loops(self):
        """Перезапускает активный цикл прослушивания."""
        self.wake.stop()
        self._listen_running = False
        if self._listen_thread:
            self._listen_thread.join(timeout=3)
        if self._mic_available:
            if self.is_active:
                self._start_full_listen()
            else:
                self._start_wake_listen()

    # ------------------------------------------------------------------
    # TTS (синтез речи)
    # ------------------------------------------------------------------

