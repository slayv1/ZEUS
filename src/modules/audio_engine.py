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
from modules.audio_listen import AudioListenMixin, bind_listen
from modules.audio_speak import AudioSpeakMixin, bind_speak

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


def _log_audio_error(text: str) -> None:
    """Пишет ошибку аудио в консоль И в zeus.log (best-effort).

    Зевс — многопоточное приложение, а в noconsole-сборке (.exe) консоли
    нет вовсе: след ошибки распознавания/микрофона/TTS должен попадать в
    файл журнала (data/zeus.log), чтобы его можно было проследить.
    Уровень определяется автоматически по маркерам текста («Ошибка …» ->
    ERROR, «недоступен» -> WARNING).
    """
    try:
        print(f"[AudioEngine] {text}")
        from core.logging_service import log as _log_msg, infer_level as _inf
        _log_msg(f"[AudioEngine] {text}", _inf(f"[AudioEngine] {text}"))
    except Exception:  # noqa: BLE001
        pass

# ------------------------------------------------------------------
# TTS в стиле «Джарвис»
# ------------------------------------------------------------------
# Глобальный замок для потокобезопасной инициализации движка.
_tts_lock = threading.Lock()
# Кэш движка: один инстанс pyttsx3 на всё приложение.
# Инициализация движка — медленная операция, поэтому создаём его
# лениво (один раз) и переиспользуем. Это ключ к быстрому отклику.
_tts_engine = None


class AudioListenerThread(threading.Thread):
    """Daemon-поток непрерывного распознавания команд."""

    def __init__(self, engine: "AudioEngine"):
        super().__init__(target=engine._listen_loop, daemon=True, name="zeus-audio-listener")
        self.engine = engine

    def stop(self) -> None:
        self.engine._listen_running = False


class ModelLoaderThread(threading.Thread):
    """Последовательно прогревает Vosk и Piper вне GUI-потока."""

    def __init__(self, engine: "AudioEngine", on_progress=None, on_complete=None):
        super().__init__(daemon=True, name="zeus-model-loader")
        self.engine = engine
        self.on_progress = on_progress
        self.on_complete = on_complete

    def run(self) -> None:
        success = True
        try:
            if self.on_progress:
                self.on_progress("Загрузка модели Vosk…")
            if self.engine.wake.available:
                self.engine.wake._ensure_loaded()
                success = bool(self.engine.wake._loaded) and success
                if self.on_progress:
                    self.on_progress("Модель Vosk загружена")
            else:
                success = False
                if self.on_progress:
                    self.on_progress("Vosk недоступен")
        except Exception as exc:  # noqa: BLE001
            success = False
            if self.on_progress:
                self.on_progress(f"Ошибка Vosk: {exc}")
        try:
            if self.on_progress:
                self.on_progress("Загрузка Piper TTS…")
            from modules.tts_piper import prewarm
            success = bool(prewarm()) and success
            if self.on_progress:
                self.on_progress("Piper TTS загружен")
        except Exception as exc:  # noqa: BLE001
            success = False
            if self.on_progress:
                self.on_progress(f"Piper недоступен: {exc}")
        if self.on_complete:
            self.on_complete(success)


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
class AudioEngine(AudioListenMixin, AudioSpeakMixin):
    """Голосовой движок Зевса.

    Управляет:
    - Распознаванием речи (через Google Speech Recognition API)
    - Синтезом речи (через pyttsx3)
    - Спящим режимом (is_active)

    Attributes:
        is_active: глобальный переключатель прослушивания
        on_command: callback(command_text) — вызывается при распознавании команды
    """

    def __init__(
        self,
        language: str = "ru-RU",
        mic_index: int | None = None,
        input_device_id: str | None = None,
        output_device_id: str | None = None,
    ):
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
        # Стабильные ID устройств (ТЗ v1.4 §2.7): endpoint-ID хранится в
        # конфиге, числовой индекс sounddevice — только рантайм-кэш.
        self._input_device_id = input_device_id
        self._output_device_id = output_device_id
        self._output_device_index = None
        if output_device_id:
            try:
                from core.audio_devices import resolve_device_id_to_index
                self._output_device_index = resolve_device_id_to_index(
                    output_device_id, kind="output"
                )
            except Exception:  # noqa: BLE001 — вывод в системный дефолт
                self._output_device_index = None
        # Стабильный ID ввода имеет приоритет над устаревшим mic_index.
        if input_device_id and mic_index is None:
            try:
                from core.audio_devices import resolve_device_id_to_index
                mic_index = resolve_device_id_to_index(input_device_id, kind="input")
            except Exception:  # noqa: BLE001
                mic_index = None
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
            # Шумовой гейт (ТЗ v1.4 §2.2): общий выключатель энергетического
            # фильтра для wake-режима. Порог команды настраивается отдельно
            # (command_energy_threshold) и применяется в _make_gate.
            self.wake.set_gate_config(
                enabled=bool(_vs.get("noise_gate_enabled", True))
            )
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

    # ------------------------------------------------------------------
    # Переключение устройств на лету (v1.4, ТЗ §2.4/2.5)
    # ------------------------------------------------------------------
    def switch_input_device(self, device_id: str | None) -> bool:
        """Переключает микрофон на устройство по стабильному ID.

        Резолвит endpoint-ID в текущий индекс (индексы «переезжают» при
        hot-plug), обновляет wake-детектор и перезапускает циклы
        прослушивания без пересоздания движка. Возвращает успех.

        device_id=None — авто-режим: возврат к системному устройству
        ввода по умолчанию (например, пункт «Авто» в трее/настройках).
        """
        if device_id is None:
            # Авто-режим: сбрасываем явный выбор и берём системный дефолт
            self._input_device_id = None
            ok = self._check_microphone(preferred=None)
        else:
            try:
                from core.audio_devices import resolve_device_id_to_index
                index = resolve_device_id_to_index(device_id, kind="input")
            except Exception as exc:  # noqa: BLE001
                _log_audio_error(f"Ошибка резолва устройства ввода: {exc}")
                return False
            if index is None:
                return False
            self._input_device_id = device_id
            ok = self._check_microphone(preferred=index)
        try:
            if ok and self.wake is not None:
                self.wake.set_mic_device(self._mic_device_index)
                self._restart_loops()
        except Exception as exc:  # noqa: BLE001
            _log_audio_error(f"Ошибка перезапуска после смены микрофона: {exc}")
        return ok

    def switch_output_device(self, device_id: str | None) -> bool:
        """Переключает устройство вывода (Piper) по стабильному ID.

        pyttsx3-фолбэк продолжает играть в системный дефолт: смена системного
        дефолтного устройства заметна пользователю и задевает другие
        приложения — применяется только как крайний случай (ТЗ §6).
        """
        try:
            from core.audio_devices import resolve_device_id_to_index
            index = resolve_device_id_to_index(device_id, kind="output")
        except Exception as exc:  # noqa: BLE001
            _log_audio_error(f"Ошибка резолва устройства вывода: {exc}")
            return False
        self._output_device_id = device_id
        self._output_device_index = index
        return device_id is None or index is not None

    def get_current_input_device_id(self) -> str | None:
        return self._input_device_id

    def get_current_output_device_id(self) -> str | None:
        return self._output_device_id

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

        # Микрофон уже занят детектором «Зевс» или командным циклом.
        # Второй sd.rec на Windows не стартует, пока первый поток не закрыт.
        wake_was_running = bool(getattr(getattr(self, "wake", None), "_running", False))
        command_was_running = bool(getattr(self, "_listen_running", False))
        try:
            self.stop_listening()
            time.sleep(0.2)
            return self._capture_once(timeout)
        finally:
            if command_was_running:
                self._start_full_listen()
            elif wake_was_running:
                self._start_wake_listen()

    def _capture_once(self, timeout: int = 5) -> str | None:
        """Пишет короткий фрагмент и распознаёт его. Микрофон должен быть свободен."""
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
            _log_audio_error(f"Ошибка listen_once: {e}")
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

bind_listen()
bind_speak()
