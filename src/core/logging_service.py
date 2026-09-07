"""Локальное журналирование Zeus с ротацией файлов.

Записи пишутся В ФАЙЛ (по умолчанию ``data/zeus.log``, рядом с .exe
в frozen-сборке) с временной меткой и уровнем ([INFO]/[WARNING]/[ERROR]/
[DEBUG]) — при сбоях скомпилированной версии отчёт можно открыть и
проследить, что происходило. Консоль при этом тоже остаётся.

Потокобезопасность: приложение многопоточное (UI, Vosk, TTS, очередь
команд) и ``get_logger()`` вызывается из любого потока. Создание хендлеров
сериализуется модульным локом, логгеры кешируются по пути, а сам
``RotatingFileHandler`` уже потокобезопасен — параллельные записи не
перемешиваются и не «задваиваются».
"""
from __future__ import annotations

import logging
import os
import threading
from logging.handlers import RotatingFileHandler

from core.config import data_path

_LOGGER_NAME = "zeus"
_LOG_FILENAME = "zeus.log"
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_BACKUP_COUNT = 3

# Стартовый уровень логгера. set_level() меняет его в рантайме.
_ACTIVE_LEVEL = logging.INFO

# Локальный пул логгеров: ключ — нормализованный путь к файлу.
# Без пула и лока два потока, впервые вызывающие get_logger(), могут
# одновременно увидеть пустой logger.handlers и добавить ДВА хендлера —
# каждая запись тогда попадала бы в файл дважды.
_pool_lock = threading.Lock()
_loggers: dict[str, logging.Logger] = {}


def _default_path() -> str:
    return data_path(_LOG_FILENAME)


def get_logger(path: str | None = None) -> logging.Logger:
    """Возвращает настроенный логгер (по умолчанию пишет в data/zeus.log).

    Потокобезопасно: создание хендлеров сериализуется, логгеры кешируются
    по нормализованному пути, повторные вызовы возвращают тот же экземпляр.
    Параметр ``path`` используют тесты для записи во временный файл.
    """
    if path is None:
        path = _default_path()
    key = os.path.normcase(os.path.abspath(path))

    with _pool_lock:
        logger = _loggers.get(key)
        if logger is None:
            if key == os.path.normcase(os.path.abspath(_default_path())):
                logger = logging.getLogger(_LOGGER_NAME)
            else:
                logger = logging.getLogger(f"{_LOGGER_NAME}.{len(_loggers) + 1}")
            _loggers[key] = logger

        if not logger.handlers:
            logger.setLevel(_ACTIVE_LEVEL)
            logger.propagate = False
            try:
                handler = RotatingFileHandler(
                    path,
                    maxBytes=_LOG_MAX_BYTES,
                    backupCount=_LOG_BACKUP_COUNT,
                    encoding="utf-8",
                )
                handler.setFormatter(logging.Formatter(
                    "[%(asctime)s] [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                ))
                logger.addHandler(handler)
            except OSError:
                # Логирование не должно ломать запуск приложения.
                logger.addHandler(logging.NullHandler())
        return logger


def set_level(level: int) -> None:
    """Меняет уровень логирования для всех созданных логгеров и новых."""
    global _ACTIVE_LEVEL
    _ACTIVE_LEVEL = int(level)
    with _pool_lock:
        for logger in _loggers.values():
            try:
                logger.setLevel(_ACTIVE_LEVEL)
            except Exception:  # noqa: BLE001
                pass


def log(message: str, level: int = logging.INFO) -> None:
    """Пишет сообщение в zeus.log, не выбрасывая исключений наружу."""
    try:
        get_logger().log(level, str(message))
    except Exception:
        pass


def debug(message: str) -> None:
    """Пишет отладочное сообщение (видно при уровне DEBUG)."""
    log(message, logging.DEBUG)


def error(message: str) -> None:
    log(message, logging.ERROR)


def infer_level(message: str) -> int:
    """Определяет уровень по русским/английским маркерам UI-сообщения."""
    text = str(message).lower()
    if any(marker in text for marker in ("ошибка", "error", "exception", "не удалось")):
        return logging.ERROR
    if any(marker in text for marker in ("предупреждение", "warning", "тайм-аут", "недоступен")):
        return logging.WARNING
    if any(marker in text for marker in ("debug", "отладк", "трассировк")):
        return logging.DEBUG
    return logging.INFO
