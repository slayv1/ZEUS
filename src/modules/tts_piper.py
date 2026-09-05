"""Модуль локального синтеза речи через Piper TTS.

Полностью офлайн: читает локальную модель (.onnx) и её конфиг (.json)
из папки models/tts/. Звук проигрывается потоково через sounddevice
(без temp-файлов, subprocess и pygame).

Особенности:
  - Ленивая загрузка голоса (кэш module-level) — как в audio_engine.py.
  - Потоковая озвучка чанками PCM в OutputStream sounddevice.
  - speak_piper() никогда не бросает исключение наружу: возвращает True,
    если текст озвучен, иначе False (для фолбэка на pyttsx3).
"""
from __future__ import annotations

import os
import re
import sys
import threading

# В frozen-сборке (PyInstaller) piper.phonemize_espeak._DIR указывает не туда,
# где лежат espeak-ng-data, из-за чего при синтезе падает:
#   "Error processing file 'D:/a/piper1-gpl/.../espeak-ng-data\phontab'".
# Переопределяем глобальный дефолт ESPEAK_DATA_DIR на фактическое расположение
# данных (зашиты в _internal/piper/espeak-ng-data) ПЕРЕД первой инициализацией
# фонемайзера piper.
def _ensure_ascii_espeak_data(espeak_dir: str) -> str:
    """Возвращает путь к espeak-ng-data, безопасный для espeak-ng (ASCII).

    espeak-ng (espeakbridge.pyd) читает служебные файлы (phontab и т.д.)
    байтовыми путями: кириллица в пути («Проекты/ИИ/...») вызывает
    'Error processing file ... phontab: Illegal byte sequence'.
    Если исходный путь содержит не-ASCII символы — копируем данные один раз
    в латинский каталог %LOCALAPPDATA%/ZeusPiper/espeak-ng-data и
    возвращаем его. Копия кешируется по маркер-файлу с датой источника.
    """
    try:
        espeak_dir.encode("ascii")
        return espeak_dir  # путь уже ASCII — используем как есть
    except UnicodeEncodeError:
        pass

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or r"C:\ProgramData"
    dest = os.path.join(base, "ZeusPiper", "espeak-ng-data")
    marker = os.path.join(dest, ".zeus_copy_ok")

    try:
        src_phontab = os.path.join(espeak_dir, "phontab")
        src_mtime = os.path.getmtime(src_phontab) if os.path.isfile(src_phontab) else 0
        if not (os.path.isfile(marker) and os.path.getmtime(marker) >= src_mtime):
            import shutil
            if os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(espeak_dir, dest)
            with open(marker, "w", encoding="ascii") as f:
                f.write("ok")
            print(f"[Piper] espeak-ng-data скопированы в ASCII-путь: {dest}")
        else:
            print(f"[Piper] espeak-ng-data: используется кеш {dest}")
        return dest
    except Exception as e:  # noqa: BLE001
        print(f"[Piper] Не удалось скопировать espeak-ng-data в ASCII-путь: {e}")
        return espeak_dir


# В frozen-сборке (PyInstaller) piper.phonemize_espeak._DIR указывает не туда,
# где лежат espeak-ng-data -> "Error processing file 'D:/a/piper1-gpl/...'".
# В dev-режиме путь проекта может содержать кириллицу («Проекты\ИИ») — тогда
# espeak-ng падает с 'Illegal byte sequence'. В обоих случаях переопределяем
# ESPEAK_DATA_DIR на корректное ASCII-расположение ПЕРЕД инициализацией piper.
def _configure_espeak_data_dir():
    try:
        from pathlib import Path as _Path
        from piper import phonemize_espeak as _pe

        candidates = []
        if getattr(sys, "frozen", False):
            try:
                from core.config import resource_path as _rp
                candidates.append(_rp(os.path.join("piper", "espeak-ng-data")))
            except Exception:  # noqa: BLE001
                pass
        # Dev-режим: данные идут в пакете piper (site-packages).
        try:
            import piper as _piper_mod
            candidates.append(
                os.path.join(os.path.dirname(_piper_mod.__file__), "espeak-ng-data")
            )
        except Exception:  # noqa: BLE001
            pass

        for cand in candidates:
            if cand and os.path.isdir(cand):
                safe = _ensure_ascii_espeak_data(cand)
                _pe.ESPEAK_DATA_DIR = _Path(safe)
                # espeakbridge.pyd игнорирует аргумент initialize() и использует
                # зашитый compile-time путь. Передаём расположение через
                # переменные окружения espeak-ng — они переопределяют дефолт.
                os.environ["ESPEAK_DATA_PATH"] = safe
                os.environ["ESPEAK_PATH"] = safe
                print(f"[Piper] espeak-ng-data: {safe}")
                return
        print("[Piper] espeak-ng-data не найдена — фолбэк на дефолт piper")
    except Exception as _e:  # noqa: BLE001
        print(f"[Piper] Не удалось переопределить ESPEAK_DATA_DIR: {_e}")


_configure_espeak_data_dir()


# Предок-настройки по умолчанию (если вдруг нечитаемы из config).
# /!\ Реальные значения читаются из data/settings.json в speak_piper().
_PIPER_MODEL_DEFAULT = "models/tts/voice.onnx"
_PIPER_LENGTH_SCALE_DEFAULT = 0.9
_PIPER_NOISE_SCALE_DEFAULT = 0.6

# Кэш: один загруженный голос на всё приложение.
_voice = None
_voice_model_path = None
_voice_lock = threading.Lock()
_stop_generation = 0


def model_available(model_path: str | None = None) -> bool:
    """True, если локальный .onnx и .json модели существуют."""
    path = model_path or _PIPER_MODEL_DEFAULT
    return bool(path and os.path.exists(path))


def set_stop() -> None:
    """Просит текущую озвучку прерваться (для кнопки «Стоп»)."""
    global _stop_generation
    _stop_generation += 1


def _normalize_path(raw: str) -> str:
    """Приводит путь (возможен относительный) к корню приложения.

    path из settings.json может быть относительным («models/tts/…») —
    резолвим через config.resource_path(): рядом с .exe (frozen) или
    в корне проекта (разработка), с fallback в _MEIPASS.
    """
    if not raw:
        raw = _PIPER_MODEL_DEFAULT
    if os.path.isabs(raw):
        return raw
    from core.config import resource_path
    return resource_path(raw)


def _load_voice(model_path: str):
    """Загружает и кэширует голос Piper (лениво). Потокобезопасно.

    Returns:
        PiperVoice или None, если piper-tts недоступен или модель не найдена.
    """
    global _voice, _voice_model_path

    normalized = _normalize_path(model_path)
    json_path = normalized + ".json"

    if not os.path.exists(normalized):
        print(f"[Piper] Модель не найдена: {normalized}")
        return None
    if not os.path.exists(json_path):
        print(f"[Piper] Конфиг модели не найден: {json_path}")
        return None

    with _voice_lock:
        if _voice is not None and _voice_model_path == normalized:
            return _voice
        try:
            from piper import PiperVoice
            if getattr(sys, "frozen", False):
                # В PyInstaller piper.__file__ указывает не туда, где лежат
                # espeak-ng-data — передаём путь явно (файлы зашиты в
                # _internal/piper/espeak-ng-data).
                from core.config import resource_path
                espeak_dir = resource_path(os.path.join("piper", "espeak-ng-data"))
                _voice = PiperVoice.load(
                    normalized, espeak_data_dir=espeak_dir, use_cuda=False
                )
            else:
                _voice = PiperVoice.load(normalized, use_cuda=False)
            _voice_model_path = normalized
            print(f"[Piper] Голос загружен: {os.path.basename(normalized)}")
            return _voice
        except Exception as e:  # noqa: BLE001 (fallback на pyttsx3)
            print(f"[Piper] Не удалось загрузить модель: {e}")
            _voice = None
            _voice_model_path = None
            return None


def _sample_rate(voice) -> int:
    """Частота дискретизации модели (часто 22050)."""
    try:
        return int(voice.config.sample_rate)
    except Exception:  # noqa: BLE001
        return 22050


def prewarm(model_path: str | None = None) -> bool:
    """Фоново прогревает голос Piper (загружает модель в кэш).

    Вызывается при старте ассистента из фонового потока, чтобы первая
    фраза «Да, сэр?» не ждала загрузки .onnx-модели (сотни мс — секунды).

    Returns:
        True — модель готова; False — piper/модель недоступны.
    """
    if model_path is None:
        try:
            from core.config import load_settings
            model_path = load_settings().get("voice_settings", {}).get(
                "piper_model", _PIPER_MODEL_DEFAULT
            )
        except Exception:  # noqa: BLE001
            model_path = _PIPER_MODEL_DEFAULT
    return _load_voice(model_path) is not None


def speak_piper(
    text: str,
    model_path: str | None = None,
    length_scale: float | None = None,
    noise_scale: float | None = None,
) -> bool:
    """Озвучивает текст потоково через Piper.

    Args:
        text: текст для синтеза.
        model_path: путь к .onnx (обычно из настроек; default models/tts/voice.onnx).
        length_scale: темп (меньше 1.0 — быстрее). Default берётся из настроек.
        noise_scale: «ровность» голоса. Default из настроек.

    Returns:
        True — озвучено; False — piper недоступен / модель не найдена / прервано.
    """
    global _stop_generation
    start_generation = _stop_generation

    # Значения из настроек, если аргументы не заданы явно.
    if length_scale is None or noise_scale is None or model_path is None:
        try:
            from core.config import load_settings
            vs = load_settings().get("voice_settings", {})
            if model_path is None:
                model_path = vs.get("piper_model", _PIPER_MODEL_DEFAULT)
            if length_scale is None:
                length_scale = float(vs.get("piper_length_scale", _PIPER_LENGTH_SCALE_DEFAULT))
            if noise_scale is None:
                noise_scale = float(vs.get("piper_noise_scale", _PIPER_NOISE_SCALE_DEFAULT))
        except Exception:  # noqa: BLE001
            model_path = model_path or _PIPER_MODEL_DEFAULT
            length_scale = length_scale or _PIPER_LENGTH_SCALE_DEFAULT
            noise_scale = noise_scale or _PIPER_NOISE_SCALE_DEFAULT

    if not text or not text.strip():
        return False

    voice = _load_voice(model_path)
    if voice is None:
        return False

    try:
        import sounddevice as sd
        import numpy as np
    except Exception:  # noqa: BLE001
        print("[Piper] sounddevice/numpy недоступны — фолбэк на pyttsx3")
        return False

    # Анализируем текст на предложения (по знакам конца предложения).
    sentences = re.split(r"(?<=[.!?])\s+", text)

    try:
        from piper.config import SynthesisConfig
        syn = SynthesisConfig(
            length_scale=length_scale,
            noise_scale=noise_scale,
            volume=1.0,
        )
    except Exception:  # noqa: BLE001
        print("[Piper] piper.config недоступен — фолбэк на pyttsx3")
        return False

    stream = None
    try:
        for sent in sentences:
            if not sent.strip() or _stop_generation != start_generation:
                break
            # piper-tts 1.x: synthesizer отдаёт чанки AudioChunk
            for chunk in voice.synthesize(sent, syn_config=syn, include_alignments=False):
                if _stop_generation != start_generation:
                    break
                rate = int(getattr(chunk, "sample_rate", None) or 22050)
                channels = int(getattr(chunk, "sample_channels", None) or 1)
                audio = getattr(chunk, "audio_int16_array", None)
                if audio is None:
                    audio = (getattr(chunk, "audio_float_array") * 32767).astype(np.int16)
                if stream is None:
                    stream = sd.OutputStream(
                        samplerate=rate, channels=channels, dtype="int16", blocksize=4096
                    )
                    stream.start()
                stream.write(np.ascontiguousarray(audio))
        return True
    except Exception as e:  # noqa: BLE001 (фолбэк на pyttsx3)
        print(f"[Piper TTS Error]: {e}")
        return False
    finally:
        if stream is not None:
            stream.stop()
            stream.close()