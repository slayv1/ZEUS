"""Проверки локального логгера и модульных синонимов команд."""
import json
import logging

from src.core import actions
from src.core.logging_service import infer_level


def test_log_level_inference():
    assert infer_level("Инициализация завершена") == logging.INFO
    assert infer_level("Предупреждение: микрофон недоступен") == logging.WARNING
    assert infer_level("Ошибка загрузки модели") == logging.ERROR


def test_commands_map_contains_aliases():
    aliases = actions._load_command_aliases()
    assert aliases["запусти"] == "открой"
    assert aliases["найти"] == "найди"
    assert actions._normalize_command_aliases("запусти chrome") == "открой chrome"


def test_commands_map_is_valid_json():
    with open(actions._commands_map_path(), "r", encoding="utf-8") as file:
        data = json.load(file)
    assert "открой" in data
    assert data["открой"]["aliases"]