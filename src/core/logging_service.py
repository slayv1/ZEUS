"""Локальное журналирование Zeus с ротацией файлов."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from core.config import data_path

_LOGGER_NAME = "zeus"
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_BACKUP_COUNT = 3


def get_logger() -> logging.Logger:
    """Возвращает настроенный логгер, безопасный для вызовов из потоков."""
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        path = data_path("zeus.log")
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


def log(message: str, level: int = logging.INFO) -> None:
    """Пишет сообщение в zeus.log, не выбрасывая исключений наружу."""
    try:
        get_logger().log(level, str(message))
    except Exception:
        pass


def infer_level(message: str) -> int:
    """Определяет уровень по русским/английским маркерам UI-сообщения."""
    text = str(message).lower()
    if any(marker in text for marker in ("ошибка", "error", "exception", "не удалось")):
        return logging.ERROR
    if any(marker in text for marker in ("предупреждение", "warning", "тайм-аут", "недоступен")):
        return logging.WARNING
    return logging.INFO
