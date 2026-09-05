"""Звуковые эффекты интерфейса (SFX) Зевса.

Короткие футуристичные звуки «в стиле Железного человека»:
  - play_activation() — при пробуждении по ключевому слову «Зевс»
  - play_success()    — при успешном завершении задачи
  - play_error()      — при ошибке

Звуки генерируются программно (numpy, чистые синусоиды с плавным
огибанием) и воспроизводятся через winsound (Windows). Использование
winsound с флагом SND_MEMORY не занимает микрофон и не конфликтует
с фоновым захватом аудио.
"""
from __future__ import annotations

import io
import threading
import wave

# Глобальный флаг включения/выключения SFX (задаётся контроллером).
_sfx_enabled = True
_lock = threading.Lock()

_SAMPLE_RATE = 48000


def set_enabled(enabled: bool):
    """Включает/выключает звуковые эффекты в рантайме."""
    global _sfx_enabled
    _sfx_enabled = bool(enabled)


def is_enabled() -> bool:
    """Возвращает True, если SFX включены."""
    return _sfx_enabled


# ------------------------------------------------------------------
# Генерация сигналов
# ------------------------------------------------------------------
def _sweep(f_start: float, f_end: float, dur: float, vol: float = 0.5):
    """Частотное скольжение (sweep) с плавным огибанием."""
    try:
        import numpy as np
    except Exception:
        return None
    n = max(1, int(_SAMPLE_RATE * dur))
    t = np.linspace(0, dur, n, endpoint=False)
    # Плавное изменение частоты от f_start до f_end
    freq = np.linspace(f_start, f_end, n)
    phase = 2.0 * np.pi * np.cumsum(freq) / _SAMPLE_RATE
    # Огибающая: плавное нарастание и затухание (0..1)
    envelope = np.sin(np.pi * t / dur) ** 1.5
    return (vol * envelope * np.sin(phase)).astype(np.float32)


def _tone(freq: float, dur: float, vol: float = 0.5):
    """Простой тон с огибающей."""
    return _sweep(freq, freq, dur, vol)


def _mix(segments):
    """Склеивает список сигналов (+паузы) в один WAV-bytes."""
    try:
        import numpy as np
        parts = [s for s in segments if s is not None]
        if not parts:
            return None
        data = np.concatenate([np.asarray(p, dtype=np.float32) for p in parts])
    except Exception:
        return None

    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def _pause(sec: float = 0.03):
    """Тишина (пауза) заданной длины."""
    try:
        import numpy as np
    except Exception:
        return None
    n = int(_SAMPLE_RATE * sec)
    return np.zeros(n, dtype=np.float32)


def _play_wav(wav_bytes):
    """Воспроизводит WAV-bytes во временном файле (не блокирует поток).

    Вместо флага SND_MEMORY («Cannot play asynchronously from memory» на части
    систем) сохраняем PCM-данные в temp-файл (ASCII-путь) и проигрываем через
    SND_FILENAME. Воспроизведение синхронное — вызывается уже внутри
    фонового потока-воркера SFX (см. _run), поэтому UI/логика не блокируются.
    """
    if wav_bytes is None:
        return
    tmp_path = None
    try:
        import os
        import tempfile
        import winsound

        fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="zeus_sfx_")
        with os.fdopen(fd, "wb") as f:
            f.write(wav_bytes)

        winsound.PlaySound(tmp_path, winsound.SND_FILENAME)
    except Exception as e:
        print(f"[SFX] Ошибка воспроизведения: {e}")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _run(kind: str):
    """Запускает эффект в отдельном фоновом потоке."""
    if not _sfx_enabled:
        return
    def _worker():
        with _lock:
            if kind == "activation":
                _play_wav(_mix([
                    _sweep(320, 900, 0.12, 0.45),
                    _pause(0.02),
                    _sweep(900, 1600, 0.12, 0.5),
                    _pause(0.02),
                    _sweep(1600, 2400, 0.18, 0.5),
                ]))
            elif kind == "success":
                _play_wav(_mix([
                    _tone(880, 0.08, 0.4),
                    _pause(0.02),
                    _tone(1320, 0.08, 0.4),
                    _pause(0.02),
                    _tone(1760, 0.16, 0.45),
                ]))
            elif kind == "error":
                _play_wav(_mix([
                    _tone(220, 0.2, 0.4),
                    _pause(0.03),
                    _tone(160, 0.25, 0.45),
                ]))
            elif kind == "thinking":
                _play_wav(_mix([
                    _sweep(500, 250, 0.15, 0.35),
                    _pause(0.02),
                    _sweep(450, 200, 0.15, 0.4),
                ]))
    threading.Thread(target=_worker, daemon=True).start()


def play_activation():
    """При пробуждении по ключевому слову «Зевс»."""
    _run("activation")


def play_success():
    """При успешном завершении задачи."""
    _run("success")


def play_error():
    """При ошибке."""
    _run("error")


def play_thinking():
    """Короткий «задумчивый» эффект перед обработкой."""
    _run("thinking")