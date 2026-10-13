"""Энергетический шумовой гейт (Noise Gate) для PCM-потока 16 бит (ТЗ v1.4 §2.2).

Проблема, которую он решает: в режиме прослушивания команды блоки микрофона
подавались в ``KaldiRecognizer`` напрямую, поэтому гул кулеров, клики клавиатуры
и шорох стола попадали в распознавание и порождали «команды-призраки»
(«зьус», «зус», «сэм» вместо тишины). Wake-детектор от этого уже защищался
(``wake_word._maybe_trigger``), а основной цикл прослушивания — нет.

Гейт устроен как нормальный аппаратный noise gate:

* ``peak`` — средний модуль отсчёта блока (грубая, но дешёвая оценка уровня);
* ``noise_floor`` — адаптивная оценка фонового шума: медленный трекер минимума.
  Он не «вскидывается» речевыми всплесками (растёт не более чем на ``FLOOR_RISE``
  за блок), поэтому громкий разговор не поднимает порог и не делает гейт глухим;
* порог ``gate = max(threshold, noise_floor * 2)`` — абсолютный минимум плюс
  запас над фоном: работает и на тихом встроенном микрофоне, и на гарнитуре;
* ``hangover`` — после речевого всплеска гейт ещё короткое время остаётся
  открытым, чтобы не обрезать окончание фразы.

Гейт НЕ изменяет аудио (не давит и не фильтрует отсчёты) — он лишь отвечает на
вопрос «подавать ли этот блок в распознаватель».
"""
from __future__ import annotations

import array
import time

# Средний модуль отсчёта int16. 30 ≈ тишина в тихой комнате; шумная клавиатура
# даёт 40-120, речь у микрофона — 1500-12000.
DEFAULT_THRESHOLD = 30

# Насколько долго гейт держится открытым после последнего речевого всплеска.
DEFAULT_HANGOVER_MS = 350.0

# Затухание удержанного пика (0.85 ≈ около секунды до нуля при блоке 250 мс).
PEAK_DECAY = 0.85

# Максимум, на который фон может вырасти за один блок: защищает от «вскидывания»
# порога от одного хлопка.
FLOOR_RISE = 1

# Во сколько раз порог должен превышать фоновый шум.
FLOOR_MULTIPLIER = 2

# Выше этого числа отсчётов в блоке уровень считается с прореживанием.
_MAX_EXACT_SAMPLES = 32000


def peak_level(data) -> int:
    """Средний модуль отсчёта блока PCM int16 (0 при пустом/битом блоке)."""
    try:
        raw = data.tobytes() if hasattr(data, "tobytes") else bytes(data)
        if len(raw) < 2:
            return 0
        samples = array.array("h")
        samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
        n = len(samples)
        if n == 0:
            return 0
        step = 1 if n <= _MAX_EXACT_SAMPLES else n // _MAX_EXACT_SAMPLES
        total = 0
        count = 0
        for i in range(0, n, step):
            total += abs(samples[i])
            count += 1
        return total // max(1, count)
    except Exception:  # noqa: BLE001 — оценка уровня не должна ронять аудио-поток
        return 0


class NoiseGate:
    """Адаптивный шумовой гейт: «пропустить блок в распознаватель или нет»."""

    def __init__(self, threshold: int = DEFAULT_THRESHOLD, *,
                 hangover_ms: float = DEFAULT_HANGOVER_MS,
                 enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self.threshold = max(0, int(threshold))
        self.hangover_s = max(0.0, float(hangover_ms) / 1000.0)
        self.reset()

    def reset(self) -> None:
        """Сбрасывает состояние (новое устройство, старт прослушивания)."""
        self._recent_peak = 0
        self._noise_floor = 0
        self._open_until = 0.0
        self.passed = 0
        self.rejected = 0
        self.last_peak = 0
        self.last_gate = self.threshold

    @property
    def noise_floor(self) -> int:
        """Текущая оценка фонового шума."""
        return self._noise_floor

    @property
    def gate_level(self) -> int:
        """Эффективный порог: абсолютный минимум или фон × коэффициент."""
        return max(self.threshold, self._noise_floor * FLOOR_MULTIPLIER)

    # ------------------------------------------------------------------
    def allow(self, data, now: float | None = None) -> bool:
        """Обновляет состояние по блоку ``data`` и решает: пропустить ли его.

        Вызывается ровно один раз на блок. Счётчики ``passed``/``rejected``
        идут для диагностики (пишутся в терминал как ``[AUDIO] [GATE]``).
        ``now`` передают тесты; в работе берётся ``time.monotonic()``.
        """
        return self.allow_peak(peak_level(data), now=now)

    def allow_peak(self, peak: int, now: float | None = None) -> bool:
        """То же, что :meth:`allow`, но для уже посчитанного уровня сигнала."""
        now = time.monotonic() if now is None else now
        peak = max(0, int(peak))

        # 1. Удержанный пик с затуханием: «было громко» ещё немного считается.
        decayed = int(self._recent_peak * PEAK_DECAY)
        self._recent_peak = peak if peak > decayed else decayed

        # 2. Фоновый шум — трекер минимума с ограниченным ростом: один хлопок
        #    не должен «вскинуть» порог вверх.
        self._noise_floor = min(peak, self._noise_floor + FLOOR_RISE)

        gate = self.gate_level
        self.last_peak = peak
        self.last_gate = gate

        if not self.enabled:
            self.passed += 1
            return True

        if peak >= gate:
            # Речь/щелчок выше порога — открываем и держим открытым hangover.
            self._open_until = now + self.hangover_s
            self.passed += 1
            return True

        if now < self._open_until:
            # Окончание фразы: хвост не режем.
            self.passed += 1
            return True

        self.rejected += 1
        return False

    def describe(self) -> str:
        """Короткая строка для лога: пик / порог / фон."""
        return (f"peak={self.last_peak} gate={self.last_gate} "
                f"floor={self._noise_floor} threshold={self.threshold}")
