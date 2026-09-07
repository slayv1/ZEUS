"""Проверки локального логгера и модульных синонимов команд."""
import json
import logging
import threading
from pathlib import Path

from src.core import actions
from src.core.logging_service import get_logger, infer_level, log, set_level, debug, error


def test_log_level_inference():
    assert infer_level("Инициализация завершена") == logging.INFO
    assert infer_level("Предупреждение: микрофон недоступен") == logging.WARNING
    assert infer_level("Ошибка загрузки модели") == logging.ERROR
    assert infer_level("debug: отладка цикла") == logging.DEBUG
    assert infer_level("debug") == logging.DEBUG


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


# ----------------------------------------------------------------------
# Логгер: файл с таймстемпами, уровни, потокобезопасность
# ----------------------------------------------------------------------
def test_logger_writes_to_file_with_levels(tmp_path: Path):
    """Логи пишутся в файл (а не только в консоль) с меткой уровня."""
    logfile = tmp_path / "zeus_test.log"
    logger = get_logger(str(logfile))
    logger.info("Инициализация завершена")
    logger.warning("Предупреждение: микрофон недоступен")
    logger.error("Ошибка загрузки модели")
    logger.debug("debug-запись (не должна попасть при INFO)")

    text = logfile.read_text(encoding="utf-8")
    # Таймстемп в записи
    assert "[2026-" in text
    lines = text.splitlines()
    assert len(lines) == 3, f"ждали 3 строки, а не {len(lines)}"
    assert "[INFO] Инициализация завершена" in text
    assert "[WARNING] Предупреждение: микрофон недоступен" in text
    assert "[ERROR] Ошибка загрузки модели" in text
    assert not any("debug-запись" in ln for ln in lines)  # INFO не пропускает DEBUG


def test_logger_debug_level_enables_debug(tmp_path: Path):
    logfile = tmp_path / "zeus_debug.log"
    logger = get_logger(str(logfile))
    logger.info("фоновая запись")
    set_level(logging.DEBUG)
    try:
        logger.debug("отладочная запись")
        logger.error("ошибка")
        text = logfile.read_text(encoding="utf-8")
        assert "[DEBUG] отладочная запись" in text
        assert "[ERROR] ошибка" in text
    finally:
        set_level(logging.INFO)


def test_logger_concurrent_writes_are_complete_and_unique(tmp_path: Path):
    """Параллельная запись из 8 потоков не теряет строк и не задваивает их.

    Раньше get_logger() мог создать два хендлера при одновременном первом
    вызове из разных потоков — каждая запись писалась в файл дважды.
    """
    logfile = tmp_path / "zeus_concurrent.log"
    n_threads = 8
    per_thread = 30

    def worker(idx: int):
        logger = get_logger(str(logfile))
        for i in range(per_thread):
            logger.info(f"thread-{idx}-line-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = logfile.read_text(encoding="utf-8")
    expected = n_threads * per_thread
    # Точный подсчёт по целым строкам: каждый маркер ровно в одной строке.
    # (поиск подстрокой невозможен — "thread-0-line-1" входит в "line-10..19")
    lines = text.splitlines()
    assert len(lines) == expected, f"ожидали {expected} строк, а не {len(lines)}"
    for t_idx in range(n_threads):
        for i in range(per_thread):
            marker = f"[INFO] thread-{t_idx}-line-{i}"
            assert sum(1 for ln in lines if ln.endswith(marker)) == 1, (
                f"{marker} задвоена/потеряна"
            )


def test_logger_pool_single_handler_per_path(tmp_path: Path):
    """Повторные вызовы get_logger(path) не плодят хендлеры."""
    logfile = tmp_path / "zeus_single.log"
    first = get_logger(str(logfile))
    second = get_logger(str(logfile))
    assert first is second
    assert len(first.handlers) == 1