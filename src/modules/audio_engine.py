"""Модуль голосового взаимодействия (Audio Engine) для Зевса.

Реализует:
  - Голосовой ввод через Vosk (офлайн-распознавание)
  - Голосовой вывод через pyttsx3 (TTS)
  - Флаг is_active для «Спящего режима»

Архитектура:
  - AudioEngine — главный класс, управляющий микрофоном и динамиком
  - Работает в фоновых потоках, не блокируя UI
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from typing import Callable

import numpy as np
import pyttsx3
import speech_recognition as sr

from modules.wake_word import WakeWordDetector, resolve_vosk_model_path

def apply_microphone_gain(audio_chunk: bytes, gain_factor: float = 1.5) -> bytes:
    """
    Усиливает входящий звуковой поток на заданный коэффициент.
    gain_factor: 1.0 — без изменений, 1.5–2.0 — заметное усиление чувствительности.
    
    Предотвращает клиппинг (хрипы) при резких звуках за счет ограничения максимального значения.
    """
    # Преобразуем байты в массив numpy (int16)
    audio_data = np.frombuffer(audio_chunk, dtype=np.int16)
    
    # Умножаем на коэффициент усиления (переводим в float32 для избежания переполнения)
    amplified = audio_data.astype(np.float32) * gain_factor
    
    # Ограничиваем значения, чтобы избежать перегрузки (клиппинга)
    amplified = np.clip(amplified, -32768, 32767)
    
    return amplified.astype(np.int16).tobytes()

# ------------------------------------------------------------------
# TTS в стиле «Джарвис»
# ------------------------------------------------------------------
# Глобальный замок для потокобезопасной инициализации движка.
_tts_lock = threading.Lock()
# Кэш движка: один инстанс pyttsx3 на всё приложение.
# Инициализация движка — медленная операция, поэтому создаём его
# лениво (один раз) и переиспользуем. Это ключ к быстрому отклику.
_tts_engine = None


# Фразы-паразиты — «хвост» распознавания (а уж, изу, ну…). Такие обрывки Vosk
# НЕ считаются командой и не должны сбрасывать сессию активного прослушивания
# в спящий режим — слушаем пользователя дальше.
_NOISE_TAIL_WORDS = {
    "а", "уж", "ну", "и", "но", "вот", "ага", "угу", "хм", "э", "хе", "о",
    "у", "аа", "ээ", "не", "же", "бы", "ли", "да", "так", "ведь", "ах", "эх",
}


def pick_jarvis_voice(voices, preferred_voice: str = "auto") -> object | None:
    """Выбирает «строгий мужской» голос из переданного списка.

    Стратегия (порядок приоритета):
      1. Точный голос из настроек (preferred_voice != "auto") — если найден по id.
      2. Русский мужской голос (Microsoft Pavel / Mikhail) — помощник
         отвечает на русском, поэтому это приоритетная цель.
      3. Любой русский голос (может оказаться женским, как запасной вариант).
      4. Английский мужской голос (David / George / James) — как в ТЗ, запасной
         строгий «бортовой» тембр.
      5. Если ничего не найдено — None (движок использует системный голос по умолчанию).
    """
    if preferred_voice != "auto":
        for v in voices:
            if v.id == preferred_voice:
                return v

    def _is_ru(v) -> bool:
        return ("russian" in v.name.lower()
                or "ru-ru" in v.id.lower()
                or "tms_ru_ru" in v.id.lower())

    def _is_ru_male(v) -> bool:
        return _is_ru(v) and any(
            token in v.name.lower()
            for token in ("pavel", "mikhail", "michael", "david")
        )

    def _is_en_male(v) -> bool:
        name_low = v.name.lower()
        return any(token in name_low
                   for token in ("david", "george", "james"))

    # 1) Русский мужской голос
    for v in voices:
        if _is_ru_male(v):
            return v
    # 2) Любой русский голос
    for v in voices:
        if _is_ru(v):
            return v
    # 3) Английский мужской голос (бортовой «Джарвис»)
    for v in voices:
        if _is_en_male(v):
            return v
    return None

def init_jarvis_voice(rate: int = 190, volume: float = 1.0,
                      voice: str = "auto") -> "pyttsx3.Engine":
    """Инициализирует локальный TTS-движок с параметрами под «Джарвиса».

    Жёсткая конфигурация (автономно, без сети/облака):
      - rate:    ускоренный темп 185–200 слов/мин (Джарвис говорит быстро).
      - volume:  максимум 1.0 — уверенное, чёткое звучание.
      - voice:   перебор системных голосов Windows с выбором строго мужского тембра.
    """
    engine = pyttsx3.init()
    try:
        # Ускоренный темп речи, характерный для ИИ
        engine.setProperty("rate", int(rate))
        # Максимальная громкость
        engine.setProperty("volume", float(volume))
        # Выбор подходящего мужского голоса из системы
        best = pick_jarvis_voice(engine.getProperty("voices"), voice)
        if best is not None:
            engine.setProperty("voice", best.id)
            print(f"[TTS] Голос выбран: {best.name} (rate={int(rate)}, volume={float(volume):.1f})")
    except Exception as e:
        print(f"[TTS] Не удалось настроить голос, использую системный: {e}")
    return engine

def get_tts_engine() -> "pyttsx3.Engine":
    """Возвращает общий (кэшированный) движок TTS.

    Читает настройки из data/settings.json при первом создании движка,
    чтобы значения rate/volume/voice можно было менять без перезапуска.
    """
    global _tts_engine
    with _tts_lock:
        if _tts_engine is None:
            rate, volume, voice = 190, 1.0, "auto"
            try:
                from core.config import load_settings
                vs = load_settings().get("voice_settings", {})
                rate = int(vs.get("rate", 190))
                volume = float(vs.get("volume", 1.0))
                voice = str(vs.get("voice", "auto"))
            except Exception:
                pass  # значения по умолчанию
            _tts_engine = init_jarvis_voice(rate=rate, volume=volume, voice=voice)
            # Держим движок «тёплым»: короткий bootstrap, чтобы первый вызов
            # не зависал на инициализации драйвера SAPI5.
            try:
                _tts_engine.runAndWait()
            except Exception:
                pass
    return _tts_engine
class AudioEngine:
    """Голосовой движок Зевса.

    Управляет:
    - Распознаванием речи (через Google Speech Recognition API)
    - Синтезом речи (через pyttsx3)
    - Спящим режимом (is_active)

    Attributes:
        is_active: глобальный переключатель прослушивания
        on_command: callback(command_text) — вызывается при распознавании команды
    """

    def __init__(self, language: str = "ru-RU", mic_index: int | None = None):
        self.language = language
        self.is_active = True  # Спящий режим: по умолчанию включён
        self.is_speaking = False  # True, когда Зевс сам говорит (детектор заглушён)
        # Callback для UI — переключение slow/fast GIF голограммы при смене состояния
        self.on_speaking_changed: callable = None
        # Окно «гашения эха»: распознанные фразы в этот период (после собственной
        # речи) отбрасываются, чтобы микрофон не ловил собственный ответ TTS.
        self._echo_grace_until = 0.0

        # --- Очередь TTS: один поток-озвучка вместо потока на каждый вызов ---
        # pyttsx3 (SAPI5) НЕ потокобезопасен: два параллельных runAndWait()
        # на общем движке приводят к дедлоку («run loop already started»).
        # Диалог («Да, сэр?» -> «Выполняю.» -> «Запускаю…») вызывает speak()
        # из разных потоков почти одновременно — поэтому все фразы
        # последовательно проговаривает единственный поток-потребитель.
        self._tts_queue: queue.Queue = queue.Queue()
        self._tts_thread = threading.Thread(
            target=self._tts_loop, daemon=True, name="zeus-tts"
        )
        self._tts_thread.start()

        # Распознавание речи
        self.recognizer = sr.Recognizer()
        # Auto-Detect Mic: если mic_index задан в настройках — используем его,
        # иначе автоматически выбираем устройство ввода по умолчанию.
        # _mic_device_index / _mic_device_name присваиваются внутри _check_microphone.
        self._mic_available = self._check_microphone(preferred=mic_index)

        # Усиление микрофона (для повышения чувствительности)
        self.mic_gain = 1.5  # значение по умолчанию
        try:
            from core.config import load_settings
            vs = load_settings().get("voice_settings", {})
            self.mic_gain = float(vs.get("mic_gain", 1.5))
        except Exception:
            pass  # оставляем значение по умолчанию

        # TTS (синтез речи)
        self._stop_speaking = False

        # Callback для обработки распознанного текста
        self.on_command: Callable[[str], None] | None = None

        # Callback амплитуды микрофона (для радиального визуализатора)
        self.on_amplitude: Callable[[float], None] | None = None
        # Амплитуда для визуализатора (0.0–1.0), потокобезопасно
        self._current_amplitude = 0.0
        self._amp_thread_running = False

        # Фоновый поток прослушивания
        self._listen_thread: threading.Thread | None = None
        self._listen_running = False

        # Лёгкий детектор Wake Word (Vosk) — используется в Sleep-режиме
        self.wake = WakeWordDetector()
        if self._mic_available:
            self.wake.set_mic_device(self._mic_device_index)

        # Отладка Wake Word: флаг берём из настроек (app_settings.debug_wake)
        self._debug_mode = False
        # Порог чувствительности Wake Word (VAD): меньше — чувствительнее,
        # но больше ложных срабатываний. И флаг отладки.
        try:
            from core.config import load_settings
            _vs = load_settings().get("voice_settings", {})
            threshold = int(_vs.get("wake_energy_threshold", 30))
            self.wake.set_energy_threshold(threshold)
            # Передаём усиление микрофона в wake-детектор (тихий микрофон должен
            # достигать энергопорога и порога распознавания).
            self.wake.mic_gain = getattr(self, "mic_gain", 1.0)
            self._debug_mode = bool(
                load_settings().get("app_settings", {}).get("debug_wake", False)
            )
            self.wake.set_debug(self._debug_mode)
        except Exception:
            pass

        print(f"[AudioEngine] Микрофон: {'доступен' if self._mic_available else 'недоступен'}")
        if self._debug_mode:
            self._print_mic_devices()

    # ------------------------------------------------------------------
    # Микрофон
    # ------------------------------------------------------------------
    def _check_microphone(self, preferred: int | None = None) -> bool:
        """Проверяет доступность микрофона и выбирает устройство.

        Auto-Detect Mic:
          - Если preferred (индекс из настроек) задан и валиден — используем его.
          - Иначе автоматически выбираем системное устройство ввода по
            умолчанию (sd.default.device[1]); если его нет — первое
            устройство с входными каналами.

        Это избавляет от ошибок, когда дефолтный микрофон в системе меняется:
        при мик_index=None всегда берётся актуальный дефолт.
        """
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            input_indices = [
                i for i, d in enumerate(devices) if d['max_input_channels'] > 0
            ]
            if not input_indices:
                self._mic_device_index = None
                self._mic_device_name = None
                return False

            idx: int | None = None

            # 1. Явно заданный индекс из настроек (если валиден)
            if preferred is not None and preferred in input_indices:
                idx = int(preferred)
            else:
                # 2. Авто-выбор: дефолтное устройство ввода системы
                try:
                    default_in = sd.default.device[1]
                    if default_in in input_indices and default_in is not None:
                        idx = int(default_in)
                except Exception:  # noqa: BLE001
                    idx = None
                # 3. Иначе — первое устройство с входом
                if idx is None:
                    idx = input_indices[0]

            self._mic_device_index = idx
            self._mic_device_name = devices[idx]['name']
            print(
                f"[AudioEngine] Микрофон # {idx}: {devices[idx]['name']}"
                f" {'(по умолчанию)' if preferred is None else '(из настроек)'}"
            )
            return True
        except Exception:
            self._mic_device_index = None
            self._mic_device_name = None
            return False

    def is_mic_available(self) -> bool:
        """Возвращает True, если микрофон доступен."""
        return self._mic_available

    def _print_mic_devices(self):
        """Выводит в консоль список всех устройств ввода (для отладки/выбора)."""
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            print("[AudioEngine] Доступные устройства ввода:")
            for i, d in enumerate(devices):
                if d.get("max_input_channels", 0) > 0:
                    marker = " <-- выбран" if i == self._mic_device_index else ""
                    print(f"  [{i}] {d.get('name')} (in={d.get('max_input_channels')}){marker}")
        except Exception as e:
            print(f"[AudioEngine] Не удалось вывести список устройств: {e}")

    # ------------------------------------------------------------------
    # Фоновое прослушивание
    # ------------------------------------------------------------------
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
        self.wake.stop()
        if self._mic_available:
            self._start_full_listen()

    def stop_wake_listener(self):
        """Публичный алиас: останавливает Wake Word детектор (см. wake.stop)."""
        try:
            self.wake.stop()
        except Exception:  # noqa: BLE001
            pass

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

    def _start_full_listen(self):
        """Запускает полное распознавание речи (тяжёлый Active-режим)."""
        if self._listen_running:
            return
        self._listen_running = True
        self._listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
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
                    except Exception:  # noqa: BLE001
                        break
                while self._listen_running:
                    try:
                        data, _ = stream.read(chunk)
                    except Exception:
                        continue
                    if data is None or len(data) == 0:
                        continue
                    data = apply_microphone_gain(data, self.mic_gain)
                    if recognizer.AcceptWaveform(data):
                        res = _json.loads(recognizer.Result())
                        self._handle_listen_text((res.get("text") or "").strip())
        except Exception as e:
            print(f"[AudioEngine] Ошибка цикла прослушивания (микрофон недоступен?): {e}")
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
                    print(f"[AudioEngine] Ошибка сервиса распознавания: {e}")
                except Exception as e:
                    print(f"[AudioEngine] Ошибка распознавания: {e}")
            except Exception as e:
                print(f"[AudioEngine] Ошибка прослушивания (фолбэк Google): {e}")
                if self._listen_running:
                    import time
                    time.sleep(0.5)

    # ------------------------------------------------------------------
    # Спящий режим
    # ------------------------------------------------------------------
    def _check_sleep_commands(self, text: str) -> bool:
        """Проверяет текст на команды 'включись/отключись'.

        Returns:
            True если это команда спящего режима (текст уже обработан)
        """
        text_lower = text.lower().strip()

        # Команды отключения
        if any(w in text_lower for w in ["зеве отключись", "зеве", "отключись", "зеве усни", "усни"]):
            if "отключись" in text_lower or "усни" in text_lower:
                self.is_active = False
                msg = "Зевс отключён, сэр. Чтобы включить, скажите «Зевс, включись»."
                print(f"[AudioEngine] {msg}")
                if self.on_command:
                    # Отправляем как системное сообщение
                    self.on_command("__SLEEP_OFF__")
                return True

        # Команды включения
        if any(w in text_lower for w in ["зеве включись", "зеве проснись", "проснись", "вернись"]):
            if not self.is_active:
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
    def _set_speaking(self, value: bool):
        """Устанавливает флаг is_speaking и уведомляет UI через callback."""
        if self.is_speaking != value:
            self.is_speaking = value
            if self.on_speaking_changed is not None:
                try:
                    self.on_speaking_changed(value)
                except Exception:
                    pass

    def speak(self, text: str):
        """Ставит фразу в очередь озвучки и сразу возвращается.

        Все фразы проговариваются последовательно единственным потоком
        zeus-tts (_tts_loop): исключается дедлок pyttsx3 при перекрытии
        «Да, сэр?» / «Выполняю.» / «Запускаю…», а вызывающие потоки
        (аудио, zeus-commands, UI) никогда не ждут синтез речи.
        """
        self._stop_speaking = False
        # Анти-эхо: не слушаем себя, пока говорим (глушим сразу,
        # не дожидаясь начала фактического проговаривания из очереди).
        self._set_speaking(True)
        if hasattr(self, "wake"):
            try:
                self.wake.mute(True)
            except Exception:
                pass
        try:
            self._tts_queue.put(text)
        except Exception:  # noqa: BLE001
            self._set_speaking(False)

    def _tts_loop(self):
        """Поток-потребитель озвучки: последовательно произносит фразы.

        На каждую фразу: mute wake -> произнесение -> grace 1.2 c против
        эха -> unmute. Любое исключение не завершает поток.
        """
        import time as _t

        while True:
            text = self._tts_queue.get()
            if text is None:
                self._tts_queue.task_done()
                break
            try:
                self._set_speaking(True)
                try:
                    self._dispatch_speak(text)
                except Exception as e:  # noqa: BLE001
                    print(f"[AudioEngine] Ошибка TTS: {e}")
            finally:
                self._set_speaking(False)
                # После собственной речи ещё 2.0 c глушим эхо (ТЗ v1.2,
                # cooldown_period = 2.0): распознавания из микрофона
                # (собственный ответ из динамиков) отбрасываются.
                self._echo_grace_until = _t.time() + 2.0
                if hasattr(self, "wake"):
                    try:
                        self.wake.mute(False)
                        # Пробрасываем grace в wake-детектор (эхо-гейт)
                        self.wake._echo_grace_until = self._echo_grace_until
                    except Exception:
                        pass
                try:
                    self._tts_queue.task_done()
                except Exception:  # noqa: BLE001
                    pass

    def stop_speaking(self):
        """Прерывает текущую озвучку (любой движок).

        ВАЖНО: экстренно глушим звук через sounddevice (sd.stop()), чтобы
        Piper замолкал мгновенно, а не доигрывал текущий буфер (ТЗ «молчи»).
        """
        self._stop_speaking = True
        # Удаляем ответы, которые ещё не начали звучать. Иначе после
        # остановки текущей фразы очередь продолжит говорить сама.
        while True:
            try:
                self._tts_queue.get_nowait()
                self._tts_queue.task_done()
            except queue.Empty:
                break
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        try:
            from modules.tts_piper import set_stop
            set_stop()
        except Exception:
            pass
        try:
            get_tts_engine().stop()
        except Exception:
            pass

    def _dispatch_speak(self, text: str):
        """Раздаёт текст выбранному TTS-движку с фолбэком."""
        engine = "piper"
        try:
            from core.config import load_settings
            engine = str(
                load_settings().get("voice_settings", {}).get("tts_engine", "piper")
            ).lower()
        except Exception:
            engine = "piper"

        if engine == "piper":
            try:
                from modules.tts_piper import speak_piper
                if speak_piper(text):
                    return  # озвучено Piper
            except Exception:
                pass  # фолбэк ниже

        # pyttsx3 (фолбэк или движок по умолчанию)
        self._speak_thread(text)

    def _speak_thread(self, text: str):
        """Внутренний поток TTS.

        Использует общий кэшированный движок (быстрый отклик) с параметрами
        «Джарвиса»: rate 190, volume 1.0, строгий мужской голос.
        """
        try:
            # Общий движок уже инициализирован init_jarvis_voice().
            engine = get_tts_engine()
            sentences = re.split(r"(?<=[.!?])\s+", text)
            for sent in sentences:
                if self._stop_speaking:
                    engine.stop()
                    break
                engine.say(sent)
            engine.runAndWait()
        except Exception as e:
            print(f"[AudioEngine] Ошибка TTS: {e}")

    # ------------------------------------------------------------------
    # Однократное распознавание (для кнопки «Слушать»)
    # ------------------------------------------------------------------
    def _get_vosk_model(self):
        """Лениво загружает и кэширует Vosk модель для офлайн-распознавания.

        Использует ту же модель, что и WakeWordDetector (self.wake),
        чтобы не загружать модель дважды.

        Returns:
            Кортеж (model, sample_rate) или (None, 16000) при ошибке.
        """
        # Если WakeWordDetector уже загрузил модель — используем её
        if self.wake is not None and self.wake._loaded and self.wake._model is not None:
            return self.wake._model, self.wake.sample_rate
        # Иначе пытаемся загрузить самостоятельно
        if self.wake is not None and self.wake.available:
            self.wake._ensure_loaded()
            if self.wake._loaded and self.wake._model is not None:
                return self.wake._model, self.wake.sample_rate
        # Fallback: импортируем vosk и загружаем напрямую
        try:
            import vosk
            import os as _os
            model_path = self.wake.model_path if self.wake else None
            if model_path and _os.path.isdir(model_path):
                model = vosk.Model(resolve_vosk_model_path(model_path))
                return model, 16000
        except Exception as e:
            print(f"[AudioEngine] Не удалось загрузить Vosk модель: {e}")
        return None, 16000

    def listen_once(self, timeout: int = 5) -> str | None:
        """Однократно записывает и распознаёт речь (полностью офлайн через Vosk).

        Используется при нажатии кнопки «Слушать» в UI.
        Если Vosk недоступен — фолбэк на Google Speech (сеть).

        Args:
            timeout: максимальное время записи (сек)

        Returns:
            распознанный текст или None при ошибке
        """
        if not self._mic_available:
            return None

        try:
            import sounddevice as sd
            import json as _json
            import numpy as np

            sample_rate = 16000
            channels = 1
            dtype = 'int16'

            # Захват аудио через sounddevice (16 кГц, int16)
            audio_data = sd.rec(
                int(sample_rate * timeout),
                samplerate=sample_rate,
                channels=channels,
                dtype=dtype,
                device=self._mic_device_index
            )
            sd.wait()

            # Применяем усиление микрофона
            audio_data = apply_microphone_gain(audio_data.tobytes(), self.mic_gain)
            audio_data = np.frombuffer(audio_data, dtype=np.int16)

            # Распознавание через Vosk (офлайн)
            model, model_sr = self._get_vosk_model()
            if model is not None:
                import vosk
                recognizer = vosk.KaldiRecognizer(model, model_sr)
                full_result = ""
                chunk_size = 4000  # ~250 мс при 16 кГц
                for i in range(0, len(audio_data), chunk_size):
                    chunk = audio_data[i:i + chunk_size].tobytes()
                    if recognizer.AcceptWaveform(chunk):
                        result = _json.loads(recognizer.Result())
                        full_result = result.get("text", "")
                final = _json.loads(recognizer.FinalResult())
                text = final.get("text", "")
                if not text and full_result:
                    text = full_result
                return text.strip() if text.strip() else None

            # Фолбэк: Vosk недоступен — Google Speech (сеть)
            import io
            import wave
            byte_io = io.BytesIO()
            with wave.open(byte_io, 'wb') as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(audio_data.tobytes())

            byte_io.seek(0)
            audio = sr.AudioData(byte_io.read(), sample_rate, 2)

            text = self.recognizer.recognize_google(audio, language=self.language)
            return text.strip()

        except sr.UnknownValueError:
            return None
        except Exception as e:
            print(f"[AudioEngine] Ошибка listen_once: {e}")
            return None

    # ------------------------------------------------------------------
    # Амплитуда микрофона (для радиального визуализатора)
    # ------------------------------------------------------------------
    def start_amplitude_monitor(self):
        """Запускает фоновый мониторинг амплитуды микрофона.

        В отдельном потоке непрерывно читает уровень звука (0.0–1.0)
        и вызывает on_amplitude callback. Частота обновления — ~30 FPS.
        """
        if self._amp_thread_running or not self._mic_available:
            return
        self._amp_thread_running = True
        threading.Thread(target=self._amplitude_loop, daemon=True).start()

    def stop_amplitude_monitor(self):
        """Останавливает мониторинг амплитуды."""
        self._amp_thread_running = False

    def _amplitude_loop(self):
        """Фоновый цикл чтения амплитуды микрофона."""
        try:
            import sounddevice as sd
            import numpy as np

            # Читаем маленькие блоки аудио для получения амплитуды
            sample_rate = 16000
            block_size = 512  # ~32ms при 16kHz

            while self._amp_thread_running:
                try:
                    block = sd.rec(
                        block_size,
                        samplerate=sample_rate,
                        channels=1,
                        dtype='float32',
                        device=self._mic_device_index,
                    )
                    sd.wait()
                    # RMS амплитуда
                    rms = float(np.sqrt(np.mean(block ** 2)))
                    # Нормализуем 0.0–1.0 (эмпирический порог)
                    amplitude = min(1.0, rms * 3.0)
                    self._current_amplitude = amplitude

                    if self.on_amplitude:
                        self.on_amplitude(amplitude)

                    # ~30 FPS
                    time.sleep(0.033)
                except Exception:
                    time.sleep(0.1)
        except Exception:
            self._amp_thread_running = False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def cleanup(self):
        """Останавливает все потоки и освобождает ресурсы."""
        self.stop_amplitude_monitor()
        self.stop_listening()
        self._stop_speaking = True
