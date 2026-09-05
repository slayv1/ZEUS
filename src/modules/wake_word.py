"""Детектор ключевого слова (Wake Word) для Зевса.

Реализован на основе Vosk (офлайн-распознавание, как в референсном
проекте). В режиме Sleep слушает ТОЛЬКО ключевое слово «Зевс»,
потребляя минимум ресурсов процессора (маленькая модель ~50 МБ,
работает локально, без обращения к сети).

Модель загружается ЛЕНИВО — при первом вызове start(). Это гарантирует,
что создание AudioEngine (и всей программы) не блокируется сетевой
загрузкой при старте.

Если Vosk или модель недоступны — детектор помечается как
недоступный (available=False). Архитектура корректно деградирует:
в режиме Sleep микрофон полностью отключается, а активация возможна
через UI-кнопку или текстовую команду «Зевс, включись» в чате.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import urllib.request
import zipfile

# Путь к папке моделей (frozen-aware через config.resource_path)
def _app_root() -> str:
    if getattr(__import__("sys"), "frozen", False):
        from core import config
        return config.app_base_dir()
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_MODELS_DIR = os.path.join(_app_root(), "models")
_MODEL_NAME = "vosk-model-small-ru-0.22"
_MODEL_PATH = os.path.join(_MODELS_DIR, _MODEL_NAME)
_MODEL_URL = f"https://alphacephei.com/vosk/models/{_MODEL_NAME}.zip"


def resolve_vosk_model_path(model_path: str) -> str:
    """Возвращает путь к модели, пригодный для загрузки в `vosk.Model`.

    Известный баг Vosk (Windows-сборка 0.3.45): `Model()` не может открыть
    модель, если в пути есть не-ASCII символы (например кириллица «Проекты\ИИ»),
    и выдаёт «does not contain model files». Если путь чисто ASCII — возвращаем
    его без изменений. Иначе создаём ASCII-джанкшен в %TEMP% (быстрая ссылка,
    без копирования ~150 МБ) и возвращаем его путь.
    """
    try:
        model_path.encode("ascii")
        return model_path
    except UnicodeEncodeError:
        pass

    import tempfile

    # Имя джанкшена фиксированное (ASCII); пересоздаём при необходимости.
    # Важно использовать lexists(), а не exists(): после обновления/удаления
    # старой сборки джанкшен может ссылаться на уже несуществующую папку.
    # Для exists() такой «висячий» junction невидим, а CreateJunction затем
    # падает с WinError 183 («файл уже существует»).
    dst = os.path.join(tempfile.gettempdir(), "zeus_vosk_model")
    try:
        # Удаляем старый джанкшен/пустую ссылку, если она висит (не трогает цель)
        if os.path.lexists(dst):
            os.rmdir(dst)
    except Exception:  # noqa: BLE001 — возможно занят, попробуем переиспользовать
        if os.path.isdir(dst):
            return dst
        return model_path

    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(model_path, dst)
        else:
            os.symlink(model_path, dst, target_is_directory=True)
    except Exception as e:  # noqa: BLE001
        print(f"[WakeWord] Не удалось создать ASCII-ссылку на модель: {e}")
        return model_path

    if os.path.isdir(dst):
        print(f"[WakeWord] Модель загружаем через ASCII-путь: {dst}")
        return dst
    return model_path


# Ключевые варианты пробуждения (в нижнем регистре) — для проверки текста результата
_WAKE_WORDS = ["зевс", "зевсе", "зевс,"]

# Чистые слова для грамматики Vosk (без пунктуации) — модель ищет ТОЛЬКО их,
# что резко снижает ложные срабатывания.
_WAKE_KEYWORDS = ["зевс", "зевес", "зевсе"]

# Минимальный интервал между срабатываниями (сек), защита от повторных
# срабатываний на «эхо» собственного голоса / отзвуке в комнате.
_WAKE_COOLDOWN = 2.0


class WakeWordDetector:
    """Лёгкий детектор ключевого слова «Зевс» на базе Vosk.

    Attributes:
        available: True, если библиотека Vosk установлена и модель
                   успешно загружена (проверяется лениво в start())
        on_wake: callback(), вызывается при обнаружении ключевого слова
    """

    def __init__(self, model_path: str = _MODEL_PATH, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.model_path = model_path
        self.available = False

        self._vosk = None
        self._model = None
        self._recognizer = None
        self._loaded = False

        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_ev = threading.Event()
        self._mic_device_index = None
        self.on_wake: callable | None = None
        # Отладочный режим: выводит уровень звука и распознанные фразы, а также
        # подробные сообщения об ошибках захвата. Включается из настроек.
        self.debug = False

        # Энергетический фильтр (VAD): срабатывание подтверждается, только если
        # уровень звука выше шумового фона. Абсолютный минимум — низкий (30),
        # чтобы тихие микрофоны работали; чистую тишину (пик ~несколько единиц)
        # отсекает адаптивный шумовой фон. Меньше минимума — чувствительнее.
        self.energy_threshold = 30
        self._recent_peak = 0       # затухающий пик громкости последних блоков
        self._noise_floor = 0       # адаптивный шумовой фон (медленный трекер минимума)
        self._muted = False         # true — детектор заглушен (собственная речь TTS)
        self._last_wake_ts = 0.0    # штамп последнего подтверждённого срабатывания
        # Усиление микрофона для wake (задаётся из AudioEngine.mic_gain). Важно,
        # чтобы тихий микрофон достигает и энергопорога, и порога распознавания.
        self.mic_gain = 1.0

        # Проверяем ТОЛЬКО наличие библиотеки vosk (быстро, без сети).
        # Модель догрузим лениво при первом start().
        try:
            import vosk
            self._vosk = vosk
            self.available = True
            print("[WakeWord] Библиотека Vosk найдена")
        except Exception as e:
            print(f"[WakeWord] Vosk не установлен: {e}")
            self.available = False

    # ------------------------------------------------------------------
    # Ленивая загрузка модели
    # ------------------------------------------------------------------
    def _ensure_loaded(self):
        """Лениво загружает модель Vosk (при первом использовании)."""
        if self._loaded or not self.available:
            return
        try:
            if not os.path.isdir(self.model_path):
                self._download_model()
            if not os.path.isdir(self.model_path):
                self.available = False
                return
            self._model = self._vosk.Model(resolve_vosk_model_path(self.model_path))
            # Грамматика: модель ищет ТОЛЬКО ключевые слова («зевс» и его формы)
            # плюс токен неизвестного. Резко снижает ложные срабатывания по
            # сравнению со свободным распознаванием любой речи.
            import json as _json
            _grammar = _json.dumps(_WAKE_KEYWORDS + ["[unk]"], ensure_ascii=False)
            self._recognizer = self._vosk.KaldiRecognizer(
                self._model, self.sample_rate, _grammar
            )
            self._recognizer.SetWords(False)
            self._loaded = True
            print("[WakeWord] Детектор готов (Vosk, офлайн)")
        except Exception as e:
            print(f"[WakeWord] Ошибка загрузки модели: {e}")
            self.available = False

    def _download_model(self):
        """Автоматически загружает маленькую русскую модель Vosk.

        Скачивание ограничено таймаутом, чтобы не блокировать
        программу при отсутствии сети.
        """
        try:
            os.makedirs(_MODELS_DIR, exist_ok=True)
            zip_path = os.path.join(_MODELS_DIR, _MODEL_NAME + ".zip")
            print(f"[WakeWord] Загрузка модели: {_MODEL_URL}")
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(20)
            try:
                urllib.request.urlretrieve(_MODEL_URL, zip_path)
            finally:
                socket.setdefaulttimeout(old_timeout)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(_MODELS_DIR)
            try:
                os.remove(zip_path)
            except OSError:
                pass
            print("[WakeWord] Модель успешно загружена")
        except Exception as e:
            print(f"[WakeWord] Не удалось загрузить модель: {e}")

    # ------------------------------------------------------------------
    # Управление
    # ------------------------------------------------------------------
    def set_mic_device(self, index: int | None):
        """Устанавливает индекс устройства микрофона."""
        self._mic_device_index = index

    def start(self, on_wake: callable):
        """Запускает фоновый цикл прослушивания Wake Word.

        Лениво загружает модель при первом вызове. Если модель
        недоступна (нет сети/файла) — детектор не запускается.

        Args:
            on_wake: callback, вызываемый при обнаружении «Зевс»
        """
        if not self.available:
            return
        self._ensure_loaded()
        if not self.available or not self._loaded:
            print("[WakeWord] Модель недоступна — детектор не запущен")
            return
        self.on_wake = on_wake
        if self._running:
            return
        self._running = True
        self._stop_ev.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("[WakeWord] Цикл прослушивания запущен (лёгкий режим)")

    def stop(self):
        """Останавливает цикл прослушивания Wake Word."""
        self._running = False
        self._stop_ev.set()
        print("[WakeWord] Цикл прослушивания остановлен")

    def set_debug(self, enabled: bool):
        """Включает/выключает отладочный вывод цикла Wake Word."""
        self.debug = bool(enabled)

    def arm_cooldown(self, seconds: float = None):
        """Ставит cooldown на `seconds` секунд с момента вызова.

        Защита от мгновенного самосрабатывания при переключении режимов
        (Active -> Sleep): переходный шум микрофона / хвост фразы не должны
        быть приняты за новое «Зевс» сразу после перезапуска детектора.
        """
        import time
        delay = _WAKE_COOLDOWN if seconds is None else max(0.0, float(seconds))
        self._last_wake_ts = time.time() + delay - _WAKE_COOLDOWN

    # ------------------------------------------------------------------
    # Внутренний цикл
    # ------------------------------------------------------------------
    def _loop(self):
        import time

        import sounddevice as sd

        # Повторные попытки открыть микрофон: если устройство занято другой
        # программой (WASAPI/эксклюзив) или временно недоступно — не умираем,
        # а пробуем снова, пока пользователь не освободит вход.
        retry_delay = 2.0
        max_open_retries = 90  # ~3 минуты попыток при занятом устройстве
        open_attempts = 0

        while self._running and not self._stop_ev.is_set():
            stream_ok = False
            try:
                with sd.RawInputStream(
                    samplerate=self.sample_rate,
                    blocksize=8000,
                    device=self._mic_device_index,
                    dtype="int16",
                    channels=1,
                ) as stream:
                    stream_ok = True
                    open_attempts = 0
                    chunks = 0
                    heartbeat = time.time()
                    if self.debug:
                        print(f"[WAKEWORD] Захват запущен, устройство={self._mic_device_index}")
                    while self._running and not self._stop_ev.is_set():
                        # Собственная речь (TTS) через динамики: не слушаем и
                        # сбрасываем распознаватель — иначе Зевс «услышит» себя.
                        if self._muted:
                            if self._recognizer is not None:
                                try:
                                    self._recognizer.Reset()
                                except Exception:
                                    pass
                            time.sleep(0.2)
                            continue

                        try:
                            data, overflowed = stream.read(4000)
                        except Exception as e:
                            if self.debug:
                                print(f"[WAKEWORD] Ошибка чтения: {e}")
                            break

                        # Статус звукового потока (overflow/underflow от PortAudio)
                        if overflowed and self.debug:
                            print("\n[WAKEWORD] Предупреждение потока: переполнение/потеря данных (overflow)")

                        if data is None or len(data) == 0:
                            continue

                        # Усиление микрофона (если задано) — чтобы тихий микрофон
                        # реально достигал и энергопорога, и распознавания Vosk.
                        gain = getattr(self, "mic_gain", 1.0)
                        if gain != 1.0:
                            try:
                                import numpy as np
                                _arr = np.frombuffer(data, dtype=np.int16)
                                _g = np.clip(_arr.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
                                data = _g.tobytes()
                            except Exception:
                                pass

                        # Энергетический фильтр (VAD): ведём затухающий пик громкости
                        peak = self._peak_level(data)
                        self._update_recent_peak(peak)

                        # --- Отладка: проверяем, поступает ли звук с микрофона ---
                        if self.debug and peak > 0:
                            print(f"[WAKEWORD-DEBUG] звук поступает, уровень={peak}", end="\r")

                        self._process_chunk(data)
                        chunks += 1

                        # Периодический «heartbeat»: подтверждает, что поток
                        # жив и реально принимает данные с устройства.
                        now = time.time()
                        if self.debug and now - heartbeat >= 5.0:
                            heartbeat = now
                            print(f"\n[WAKEWORD] Поток активен: блоков={chunks}, пик={self._recent_peak}")
            except Exception as e:
                # Не удалось открыть/удерживать поток — сообщаем и пробуем снова
                if self.debug:
                    print(f"\n[WAKEWORD] Не удалось открыть звуковой поток: {e}")
                # Счётчик растёт только при неудачном ОТКРЫТИИ (не при разрыве уже
                # работавшего потока), чтобы транзиентные сбои не глушили цикл.
                if not stream_ok:
                    open_attempts += 1
                if open_attempts >= max_open_retries:
                    if self.debug:
                        print("[WAKEWORD] Слишком много попыток открыть микрофон — останавливаемся")
                    break
            finally:
                if self.debug and stream_ok:
                    print("[WAKEWORD] Поток закрыт")

            # Пауза между попытками, пока не попросили остановиться
            if self._running and not self._stop_ev.is_set():
                time.sleep(retry_delay)

        self._running = False

    @staticmethod
    def _peak_level(data) -> int:
        """Грубая оценка уровня входного сигнала (средний модуль отсчёта int16)."""
        try:
            import array
            samples = array.array("h", data)
            if not samples:
                return 0
            n = len(samples)
            step = max(1, n // 200)
            total = 0
            count = 0
            for i in range(0, n, step):
                total += abs(samples[i])
                count += 1
            return total // max(1, count)
        except Exception:
            return 0

    def _process_chunk(self, data):
        """Обрабатывает блок PCM: полный/частичный результат Vosk и поиск «Зевс»."""
        import json

        if self._recognizer is None:
            return

        try:
            raw = data.tobytes() if hasattr(data, "tobytes") else bytes(data)

            # Сначала полный результат (фраза завершена)
            if self._recognizer.AcceptWaveform(raw):
                result = json.loads(self._recognizer.Result())
                text = result.get("text", "").lower()
                if self.debug:
                    print(f"\n[VOSK WAKE ENGINE]: Распознано -> '{text}'")
                self._maybe_trigger(text)
            else:
                # Неполная фраза — ищем слово в частичном результате
                try:
                    partial = json.loads(self._recognizer.PartialResult())
                    text = (partial.get("partial", "") or "").lower()
                    self._maybe_trigger(text)
                except Exception:
                    pass
        except Exception as e:
            if self.debug:
                print(f"[WAKEWORD] Ошибка обработки блока: {e}")

    def _update_recent_peak(self, peak: int):
        """Обновляет затухающий пик и адаптивный шумовой фон.

        Держит «гейт открытым» короткое время после произнесённого слова.
        Шумовой фон тянется к минимуму (растёт не более чем на 1 за блок),
        поэтому речевые всплески его не «вскидывают».
        """
        decayed = int(self._recent_peak * 0.85)
        self._recent_peak = peak if peak > decayed else decayed
        # Оценка фонового шума: медленный трекер минимума
        self._noise_floor = min(peak, self._noise_floor + 1)

    def _maybe_trigger(self, text: str) -> bool:
        """Проверяет фразу на «Зевс» и (при подтверждении) вызывает on_wake.

        Возвращает True, если текст содержал ключевое слово (вне зависимости,
        подтверждено ли срабатывание фильтрами) — чтобы не проверять повторно.

        Фильтры против ложных срабатываний:
          1. Ограничение грамматикой Vosk (ищем только «зевс»/формы).
          2. Энергетический фильтр: «Зевс» должно быть произнесено при
             достаточном уровне звука (не просто шуршание/шёпот).
          3. Кулдаун: защита от повторных срабатываний на «эхо».
        """
        if not text or not any(w in text for w in _WAKE_WORDS):
            return False

        # 2. Энергетический фильтр (VAD): порог не ниже абсолютного минимума и
        #    выше шумового фона. Адаптивно — работает и на тихом микрофоне.
        gate = max(self.energy_threshold, self._noise_floor * 2)
        if self._recent_peak < gate:
            if self.debug:
                print(f"[WAKEWORD] Отклонено энергогате: peak={self._recent_peak} < порог={gate}")
            return True

        # 3. Кулдаун — не позволяем эхо/эху колонок срабатывать повторно
        import time
        now = time.time()
        if now - self._last_wake_ts < _WAKE_COOLDOWN:
            if self.debug:
                print("[WAKEWORD] Срабатывание подавлено (кулдаун)")
            return True
        self._last_wake_ts = now

        # Эхо-гейт: не будимся на «Зевс» из собственного ответа TTS (grace-окно)
        if hasattr(self, "_echo_grace_until") and now < self._echo_grace_until:
            if self.debug:
                print("[WAKEWORD] Срабатывание подавлено (эхо собственной речи)")
            return True

        # Слово подтверждено — сбрасываем распознаватель и будим ассистента
        if self._recognizer is not None:
            try:
                self._recognizer.Reset()
            except Exception:
                pass
        print("[WAKEWORD] Обнаружено ключевое слово «Зевс»")
        if self.on_wake:
            try:
                self.on_wake()
            except Exception as e:
                print(f"[WakeWord] Ошибка callback: {e}")
        return True

    def set_energy_threshold(self, threshold: int):
        """Задаёт порог громкости (VAD) для подтверждения срабатывания."""
        self.energy_threshold = max(0, int(threshold))

    def mute(self, muted: bool):
        """Заглушает детектор (на время собственной речи TTS), чтобы Зевс
        не «услышал» свой голос из динамиков и не сработал повторно."""
        self._muted = bool(muted)
        self._recent_peak = 0  # начинаем с чистого листа после собственной речи
        if self._recognizer is not None:
            try:
                self._recognizer.Reset()
            except Exception:
                pass
