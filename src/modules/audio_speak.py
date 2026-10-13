# -*- coding: utf-8 -*-
"""Озвучка: очередь TTS, Piper и остановка речи."""
from __future__ import annotations

import threading

_log_audio_error = lambda text: None
get_tts_engine = lambda: None


def bind_speak() -> None:
    """Подключает помощники из audio_engine после его загрузки."""
    global _log_audio_error, get_tts_engine
    from modules import audio_engine as ae
    _log_audio_error = ae._log_audio_error
    get_tts_engine = ae.get_tts_engine


class AudioSpeakMixin:
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
                    _log_audio_error(f"Ошибка TTS: {e}")
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
        except Exception as exc:
            _log_audio_error(f"не остановил звук: {exc}")
        try:
            from modules.tts_piper import set_stop
            set_stop()
        except Exception as exc:
            _log_audio_error(f"не остановил Piper: {exc}")
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
                # ТЗ v1.4 §2.5: Piper играет в выбранное устройство вывода.
                if speak_piper(text, device=getattr(self, "_output_device_index", None)):
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
            _log_audio_error(f"Ошибка TTS: {e}")

    # ------------------------------------------------------------------
    # Однократное распознавание (для кнопки «Слушать»)
    # ------------------------------------------------------------------

