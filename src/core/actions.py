# -*- coding: utf-8 -*-
"""Диспетчер системных команд Зевса.

Зоны вынесены в соседние модули:
  * actions_launch — база команд, запуск и закрытие;
  * actions_media — громкость, яркость, время, калькулятор, заметки;
  * actions_search — интернет, погода, магазины, индекс и диски.

Этот модуль решает, какая зона обрабатывает фразу, и реэкспортирует
прежние имена, чтобы ``from core import actions`` не менялся.
"""
from __future__ import annotations

import re
from typing import Any

from core.actions_media import (
    _ABSOLUTE_VALUE_PATTERN,
    _CALC_BIN_OPS,
    _CALC_MAX_BASE,
    _CALC_MAX_DEPTH,
    _CALC_MAX_EXPONENT,
    _CALC_MAX_RESULT,
    _CALC_UN_OPS,
    _CALC_WORDS,
    _NUM_WORDS,
    _VK_VOLUME_DOWN,
    _VK_VOLUME_MUTE,
    _VK_VOLUME_UP,
    _VOLUME_MAX_STEPS,
    _as_float,
    _calc_node,
    _handle_brightness_command,
    _handle_calc_command,
    _handle_lock_pc_command,
    _handle_minimize_all_command,
    _handle_notes_command,
    _handle_time_date_command,
    _handle_volume_command,
    _notes_path,
    _press_media_key,
    _safe_calc,
    set_brightness,
    set_volume_absolute,
    set_volume_relative,
)

from core.actions_launch import (
    _CLOSE_ALIASES,
    _CMDS_CACHE,
    _CMDS_CACHE_MTIME,
    _COMMAND_ALIASES,
    _FILLER_PREFIX_RE,
    _SYSTEM_PROCESSES,
    _TRANSLIT_MAP,
    _command_save_failed,
    _commands_map_path,
    _commands_path,
    _exact_name_match,
    _find_app,
    _find_game,
    _fuzzy_key,
    _is_system_process,
    _launch_app,
    _launch_game,
    _launch_random_game,
    _load_command_aliases,
    _norm_ratio,
    _norm_token,
    _normalize_command_aliases,
    _resolve_close_target,
    _save_commands,
    _strip_leading_fillers,
    _translit,
    capture_active_app,
    close_application,
    delete_command,
    ensure_commands_file,
    find_best_game_or_app,
    launch_installed_game,
    load_commands,
    reload_commands,
    save_app_command,
)

from core.actions_search import (
    DriveMonitor,
    _DRIVE_CHECK_INTERVAL,
    _STORE_LAUNCH_VERBS,
    _WEATHER_CITY_RE,
    _WEATHER_QUESTION_RE,
    _WEATHER_SERVICE_PHRASES,
    _WEATHER_TRIGGERS,
    _WEATHER_VOCATIVE_RE,
    _WEBSEARCH_MARKERS,
    _drive_monitor_instance,
    _extract_store_query,
    _extract_weather_city,
    _extract_websearch_query,
    _handle_drive_scan_response,
    _handle_find_command,
    _handle_scan_flash_command,
    _handle_steam_store_command,
    _handle_taobao_command,
    _handle_update_index,
    _handle_weather_command,
    _handle_websearch_command,
    _pending_drive_scan,
    _sounds_like_taobao,
    cancel_drive_scan,
    confirm_drive_scan,
    get_pending_drive_scan,
    set_pending_drive_scan,
    start_drive_monitor,
    stop_drive_monitor,
)

# === Парсинг команд ===

# Ключевые слова, которые указывают на системную команду.
# Примечание (websearch/weather): «погугли»/«погода» сюда НЕ добавлены —
# их хендлеры (_handle_websearch_command/_handle_weather_command) вызываются
# в execute_command() ДО этой проверки-гейта, гейт нужен только для
# ветки запуска приложений. Добавление «погода» ломало обычную речь
# («анекдот про погоду» -> ложная «системная команда» вместо LLM).
_COMMAND_KEYWORDS = re.compile(
    r"\b(открой|открыть|запусти|запустить|закрой|закрыть|выключи|запомни|найди|найти|обнови|удали|close|open|launch|start|stop|find|update|delete)\b",
    re.IGNORECASE,
)

_APP_NAME_PATTERN = re.compile(
    r"(?:открой|открыть|запусти|запустить|закрой|закрыть|выключи|close|open|launch|start|stop)\s+(.+)",
    re.IGNORECASE,
)

# Паттерн для команды поиска программы
_FIND_APP_PATTERN = re.compile(
    r"(?:найди|find)\s+(.+)",
    re.IGNORECASE,
)

# Паттерн для команды обновления базы
_UPDATE_INDEX_PATTERN = re.compile(
    r"(?:обнови\s+базу|обнови\s+индекс|update\s+index|update\s+database)",
    re.IGNORECASE,
)

# Паттерн для команды запоминания программы
_REMEMBER_APP_PATTERN = re.compile(
    r"запомни\s+эту\s+программу\s+как\s+(.+)",
    re.IGNORECASE,
)

# Паттерн для команды удаления
_DELETE_COMMAND_PATTERN = re.compile(
    r"удали\s+команду\s+(.+)",
    re.IGNORECASE,
)

def is_system_command(text: str) -> bool:
    """Проверяет, содержит ли текст командные ключевые слова."""
    return bool(_COMMAND_KEYWORDS.search(text))


def _extract_target(text: str) -> str | None:
    """Извлекает имя программы/игры из текста команды."""
    m = _APP_NAME_PATTERN.search(text)
    if m:
        target = m.group(1).strip().lower()
        # Убираем лишние слова
        target = re.sub(r"\s+(пожалуйста|плиз|please|pls)\b", "", target, flags=re.IGNORECASE).strip()
        # Убираем знаки препинания в конце
        target = re.sub(r"[,;.]+$", "", target).strip()
        return target
    return None


def _extract_find_target(text: str) -> str | None:
    """Извлекает имя программы из команды 'найди [Имя]'."""
    m = _FIND_APP_PATTERN.search(text)
    if m:
        target = m.group(1).strip().lower()
        # Убираем лишние слова
        target = re.sub(r"\s+(пожалуйста|плиз|please|pls)\b", "", target, flags=re.IGNORECASE).strip()
        # Убираем знаки препинания в конце
        target = re.sub(r"[,;.]+$", "", target).strip()
        return target
    return None


def execute_command(text: str) -> dict[str, Any] | None:
    """Пытается выполнить системную команду.

    Возвращает словарь с результатом:
      {
        "success": bool,
        "message": str,   # голосовой ответ
        "action": str,    # "launch_app" | "launch_game" | "close_app" | "random_game" | "remember_app"
      }

    Возвращает None, если текст не содержит командных ключевых слов.
    """
    text = _normalize_command_aliases(text)
    text_lower = text.lower()

    # --- Этап 12: ответ на вопрос о сканировании нового носителя ---
    # Если Зевс ранее спросил «сканировать ли флешку?» и пользователь
    # отвечает «да»/«сканируй» или «нет» — обрабатываем это первым.
    drive_response = _handle_drive_scan_response(text)
    if drive_response is not None:
        return drive_response

    # --- Этап 12: явная команда сканирования флешки/внешнего диска ---
    flash_cmd = _handle_scan_flash_command(text)
    if flash_cmd is not None:
        return flash_cmd

    # --- Время/дата, калькулятор, блокировка ПК, окна, заметки ---
    for handler in (
        _handle_time_date_command,
        _handle_calc_command,
        _handle_lock_pc_command,
        _handle_minimize_all_command,
        _handle_notes_command,
    ):
        quick_result = handler(text)
        if quick_result is not None:
            return quick_result

    # --- Системные макросы: громкость и яркость (голосовое управление) ---
    volume_result = _handle_volume_command(text)
    if volume_result is not None:
        return volume_result
    brightness_result = _handle_brightness_command(text)
    if brightness_result is not None:
        return brightness_result

    # --- Taobao: веб-поиск (голосовое «найди … на таобао») ---
    taobao_result = _handle_taobao_command(text)
    if taobao_result is not None:
        return taobao_result

    # --- Веб-поиск «погугли/поищи в интернете» (ТЗ websearch_weather §1) ---
    # До steam: «погугли сколько стоит X» без «стим» не должен уйти в магазин;
    # до блока «найди»: «найди в интернете X» перехватывается раньше локального поиска.
    websearch_result = _handle_websearch_command(text)
    if websearch_result is not None:
        return websearch_result

    # --- Погода MVP «погода в X» (ТЗ websearch_weather §2.1) ---
    # После taobao («погода» не содержит «тао» — нет пересечения), до steam/«найди».
    weather_result = _handle_weather_command(text)
    if weather_result is not None:
        return weather_result

    # --- Гибридный Steam: веб-магазин (поиск/цены/корзина) ---
    steam_result = _handle_steam_store_command(text)
    if steam_result is not None:
        return steam_result

    # --- Автономный поиск программы ---
    if any(w in text_lower for w in ["найди", "find"]):
        find_target = _extract_find_target(text)
        if find_target:
            return _handle_find_command(find_target)
        else:
            return {
                "success": False,
                "message": "Не понял, что искать. Используйте: 'найди [Имя программы]'",
                "action": "file_search",
            }
    
    # --- Обновление индекса ---
    if _UPDATE_INDEX_PATTERN.search(text):
        return _handle_update_index()
    
    # --- Запоминание активного приложения ---
    if "запомни эту программу" in text_lower:
        # Извлекаем имя из команды "запомни эту программу как [Имя]"
        m = _REMEMBER_APP_PATTERN.search(text)
        if m:
            app_name = m.group(1).strip()
            
            # Получаем активный процесс
            capture_result = capture_active_app()
            
            if not capture_result["success"]:
                return {
                    "success": False,
                    "message": capture_result["message"],
                    "action": "remember_app",
                }
            
            # Сохраняем в commands.json
            save_result = save_app_command(app_name, capture_result["path"])
            return save_result
        else:
            return {
                "success": False,
                "message": "Не понял, как назвать программу. Используйте: 'запомни эту программу как [Имя]'",
                "action": "remember_app",
            }
    
    # --- Удаление команды ---
    if any(w in text_lower for w in ["удали", "delete"]):
        m = _DELETE_COMMAND_PATTERN.search(text)
        if m:
            command_name = m.group(1).strip()
            # Возвращаем запрос на подтверждение
            return {
                "success": True,
                "message": f"Вы уверены, что хотите удалить команду '{command_name}'?",
                "action": "delete_command_confirm",
                "command_name": command_name,
            }
        else:
            return {
                "success": False,
                "message": "Не понял, какую команду удалить. Используйте: 'удали команду [Имя]'",
                "action": "delete_command",
            }
    
    # Проверяем остальные системные команды
    if not is_system_command(text):
        # Одиночное название без глагола («каэс», «гта», «вотч догс»):
        # запускаем только при ТОЧНОМ совпадении с ключом/алиасом,
        # иначе обычная речь не должна превращаться в запуск.
        _k, _e = _exact_name_match(text, load_commands())
        if _e is not None:
            if _k == "app":
                return _launch_app(text, _e)
            return _launch_game(text, _e)
        return None

    commands = load_commands()
    target = _extract_target(text)
    if not target:
        # fallback: возможно глагол распознан неточно — пробуем сопоставить
        # весь текст («контр страйк два», «включи теккен 8» и т.п.).
        kind, entry = find_best_game_or_app(text, commands)
        if entry is not None:
            if kind == "app":
                return _launch_app(text, entry)
            return _launch_game(text, entry)
        return {
            "success": False,
            "message": "Не понял, что запустить. Уточните название.",
            "action": "unknown",
        }

    # --- Закрытие приложения ---
    if any(w in text_lower for w in ["закрой", "close", "stop", "выключи"]):
        return close_application(target)

    # --- Случайная игра ---
    if any(w in text_lower for w in ["случайную", "рандом", "random", "любую игру"]):
        return _launch_random_game(commands)

    # --- Запуск приложения ---
    # Сначала ищем в commands.json
    app_entry = _find_app(target, commands.get("приложения", {}))
    if app_entry:
        return _launch_app(target, app_entry)

    # --- Запуск игры ---
    game_entry = _find_game(target, commands.get("игры", {}))
    if game_entry:
        return _launch_game(target, game_entry)

    # --- Унифицированный поиск: «каэс/каес», грубые произношения, алиасы ---
    best_kind, best_entry = find_best_game_or_app(target or text, commands)
    if best_entry is not None:
        if best_kind == "app":
            return _launch_app(target or text, best_entry)
        return _launch_game(target or text, best_entry)
    
    # Если не нашли в commands.json - ищем в индексе (fallback)
    from core.file_scanner import get_indexed_program
    indexed_path = get_indexed_program(target)
    if indexed_path:
        # Нашли в индексе - предлагаем сохранить
        return {
            "success": True,
            "message": f"Нашёл {indexed_path}. Запустить, сэр?",
            "action": "launch_app",
            "path": indexed_path,
        }

    return {
        "success": False,
        "message": f"Не нашёл '{target}' в списке команд. Добавьте его в commands.json или скажите 'найди {target}'.",
        "action": "not_found",
    }

