"""Модуль чата с LLM (Chat Engine) для Зевса.

Реализует:
  - Асинхронное взаимодействие с Ollama
  - Стриминг ответов
  - Управление историей диалога
  - Системные промпты

Архитектура:
  - ChatEngine — главный класс, клиент Ollama
  - Работает асинхронно, не блокируя UI
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from typing import Callable

import ollama

# Кэш ответов LLM для частых команд используется вне import gauge
try:
    from core import config
except Exception:  # noqa: BLE001 — допустимо при импорте вне проекта
    config = None


OLLAMA_HOST = "http://localhost:11434"

# Кэш ответов для частых/заученных команд: нормализованный запрос -> ответ.
# Позволяет мгновенно отвечать без обращения к локальной модели Ollama.
_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "cache.json",
)
# Максимальное число накопленных записей кэша (предохранитель от роста файла).
_CACHE_MAX_SIZE = 200
# Порог длины ответа для «запоминания» — не кэшируем гигантские генерации.
_CACHE_MAX_ANSWER = 1200
# Порог длины запроса — кэшируем только короткие (частые) команды.
_CACHE_MAX_QUERY = 80

# Встроенные «заученные» ответы на типовые вопросы (без обращения к модели).
_CANNED_ANSWERS = {
    "привет": "Здравствуйте, сэр. Чем могу помочь?",
    "здравствуй": "Здравствуйте, сэр. Ожидаю ваших указаний.",
    "здравствуйте": "Здравствуйте, сэр. Ожидаю ваших указаний.",
    "добрый день": "Добрый день, сэр. Чем могу быть полезен?",
    "добрый вечер": "Добрый вечер, сэр. Чем могу быть полезен?",
    "доброе утро": "Доброе утро, сэр. Чем могу быть полезен?",
    "как дела": "Всё в порядке, сэр. Все системы работают штатно.",
    "как ты": "Всё в порядке, сэр. Все системы работают штатно.",
    "ты здесь": "Всегда на связи, сэр.",
    "здесь ты": "Всегда на связи, сэр.",
    "слышишь меня": "Да, сэр. Слышу вас чётко.",
    "спасибо": "Всегда к вашим услугам, сэр.",
    "благодарю": "Всегда к вашим услугам, сэр.",
    "кто ты": "Я — Зевс, ваш персональный голосовой ассистент.",
    "как тебя зовут": "Меня зовут Зевс, сэр.",
    "ты живой": "Я искусственный интеллект, сэр, но стараюсь быть максимально полезным.",
    "что ты умеешь": "Я управляю программами, запускаю приложения, отвечаю на вопросы "
                    "и выполняю голосовые команды, сэр.",
    "почему ты тут": "Я здесь, чтобы помогать вам, сэр.",
}

# Таймаут ожидания ответа от LLM (секунды). Защита от вечной блокировки
# флага `_is_busy`, если Ollama зависла или модель генерирует слишком долго.
REQUEST_TIMEOUT = 60.0

# Максимум сообщений истории, отправляемых модели: история растёт бесконечно
# не должна — иначе переполняется контекст LLM и растёт время генерации.
_MAX_HISTORY = 16


class ChatEngine:
    """Чат-движок Зевса для общения с LLM через Ollama.

    Attributes:
        model: название модели Ollama (по умолчанию llama3.1)
        system_prompt: системный промпт
        temperature: температура генерации (0.0 - 1.0)
        on_token: callback(token) при стриминге нового токена
        on_complete: callback(full_text) когда генерация завершена
        on_error: callback(error_message) при ошибке
    """

    def __init__(self, host: str = OLLAMA_HOST):
        self.host = host
        # Клиент Ollama (ТЗ: host передаётся в конструктор, а не в chat()).
        # В новых версиях библиотеки ollama метод chat()/Client.chat() не
        # принимает аргумент `host` — иначе TypeError: unexpected keyword
        # argument 'host'.
        self.client = ollama.Client(host=host)
        self.model = "llama3.1"
        self.system_prompt = ""
        self.temperature = 0.7

        # Callbacks
        self.on_token: Callable[[str], None] | None = None
        self.on_complete: Callable[[str], None] | None = None
        self.on_error: Callable[[str], None] | None = None

        # История диалога
        self.conversation: list[dict[str, str]] = []

        # Флаг занятости
        self._is_busy = False

        # --- Кэш ответов для частых команд ---
        # Накопленные «заученные» пары + встроенные (canned). Нормализованный
        # запрос -> ответ. Поиск выполняется ДО обращения к Ollama.
        self.cache_enabled = True
        self._cache: dict[str, str] = {}
        self._load_cache()

    # ------------------------------------------------------------------
    # Кэш ответов (частые команды)
    # ------------------------------------------------------------------
    def _cache_key(self, text: str) -> str:
        """Нормализует запрос для поиска в кэше.

        Приводит к нижнему регистру, убирает обращение к Зевсу в начале
        («Зевс, ...», «Зевс ...»), пунктуацию и лишние пробелы.
        """
        import re
        s = text.lower().strip()
        s = re.sub(r"\b(зеевс|зевс|зевес)\b\s*,?\s+", "", s)
        s = re.sub(r"[,.;:!?«»()]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _load_cache(self) -> None:
        """Загружает накопленный кэш ответов из data/cache.json (best-effort)."""
        self._cache = {}
        try:
            if os.path.exists(_CACHE_PATH):
                with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._cache = {str(k): str(v) for k, v in data.items()}
        except Exception:  # noqa: BLE001 — кэш не должен ломать чат
            self._cache = {}

    def _save_cache(self) -> None:
        """Сохраняет накопленный кэш на диск (best-effort)."""
        if not self._cache:
            return
        try:
            os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
            with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    def _remember(self, query: str, answer: str) -> None:
        """«Запоминает» короткий частый запрос для мгновенного ответа впредь.

        Встроенные canned-ответы имеют приоритет и не перезаписываются.
        Накопленный кэш ограничен по размеру (LRU-срез — новые записи сверху).
        """
        if not self.cache_enabled:
            return
        if not query or not answer:
            return
        if len(answer) > _CACHE_MAX_ANSWER:
            return
        key = self._cache_key(query)
        if not key or len(key) > _CACHE_MAX_QUERY:
            return
        if key in _CANNED_ANSWERS:
            return  # встроенные ответы не трогаем
        self._cache[key] = answer.strip()
        # LRU-срез до максимального размера
        if len(self._cache) > _CACHE_MAX_SIZE:
            items = list(self._cache.items())
            self._cache = dict(items[-_CACHE_MAX_SIZE:])
        self._save_cache()

    def get_cached_answer(self, text: str) -> str | None:
        """Возвращает закэшированный ответ для запроса или None."""
        if not self.cache_enabled:
            return None
        key = self._cache_key(text)
        if not key:
            return None
        if key in _CANNED_ANSWERS:
            return _CANNED_ANSWERS[key]
        return self._cache.get(key)

    # ------------------------------------------------------------------
    # Проверка Ollama
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        """Проверяет доступность Ollama."""
        try:
            with urllib.request.urlopen(self.host, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def is_busy(self) -> bool:
        """Проверяет, выполняется ли сейчас генерация."""
        return self._is_busy

    # ------------------------------------------------------------------
    # Отправка сообщения
    # ------------------------------------------------------------------
    def ask(self, text: str):
        """Отправляет текст в LLM и запускает стриминг ответа.

        Сначала проверяет кэш частых команд: если запрос «заучен» (встроенный
        canned-ответ или накопленный из прошлых генераций), отвечает мгновенно
        без обращения к локальной модели Ollama.

        Работает в отдельном потоке через синхронный ollama.chat — без asyncio,
        чтобы избежать ошибки «Event loop is closed».
        """
        if self._is_busy:
            return

        # --- Быстрый путь: ответ из кэша без обращения к Ollama ---
        cached = self.get_cached_answer(text)
        if cached is not None:
            self._is_busy = True
            self.conversation.append({"role": "user", "content": text})
            self.conversation.append({"role": "assistant", "content": cached})
            threading.Thread(
                target=self._emit_cached, args=(cached,), daemon=True
            ).start()
            return

        self._is_busy = True

        # Сохраняем сообщение пользователя в историю
        self.conversation.append({"role": "user", "content": text})

        # Запускаем синхронный запрос к Ollama в отдельном потоке (без asyncio)
        threading.Thread(
            target=self._run_sync,
            args=(text,),
            daemon=True,
        ).start()

    def _emit_cached(self, answer: str):
        """Эмитирует закэшированный ответ через on_token/on_complete.

        Запускается в отдельном потоке и не блокирует UI. Флаг _is_busy
        снимается после завершения — как в обычном потоке генерации.
        """
        try:
            if self.on_token:
                self.on_token(answer)
            if self.on_complete:
                self.on_complete(answer)
        finally:
            self._is_busy = False

    def _run_sync(self, text: str):
        """Синхронный запрос к Ollama в отдельном потоке (без asyncio).

        Исключает ошибку «Event loop is closed»: используется синхронный
        ollama.chat вместо AsyncClient + event loop.
        """
        full_text = ""
        try:
            messages = []
            if self.system_prompt:
                messages.append({"role": "system", "content": self.system_prompt})
            messages.extend(self.conversation[-_MAX_HISTORY:])

            # Синхронный стриминг от Ollama через клиент (хост задан в конструкторе)
            stream = self.client.chat(
                model=self.model,
                messages=messages,
                options={"temperature": self.temperature},
                stream=True,
            )

            for chunk in stream:
                piece = chunk["message"]["content"]
                full_text += piece
                if self.on_token:
                    self.on_token(piece)

            # Сохраняем ответ в историю
            self.conversation.append({"role": "assistant", "content": full_text})

            # «Запоминаем» частый запрос для мгновенного ответа в следующий раз.
            self._remember(text, full_text)

            if self.on_complete:
                self.on_complete(full_text)

        except Exception as e:
            error_msg = f"Ошибка подключения к Ollama: {e}"
            self._trigger_error(error_msg)
        finally:
            self._is_busy = False

    def _trigger_error(self, message: str):
        """Уведомляет UI об ошибке через on_error (если зарегистрирован)."""
        if self.on_error:
            self.on_error(message)

    # ------------------------------------------------------------------
    # Управление историей
    # ------------------------------------------------------------------
    def new_chat(self):
        """Очищает историю диалога."""
        self.conversation.clear()

    def set_model(self, model: str):
        """Устанавливает модель Ollama."""
        self.model = model

    def set_system_prompt(self, prompt: str):
        """Устанавливает системный промпт."""
        self.system_prompt = prompt

    def set_temperature(self, temp: float):
        """Устанавливает температуру генерации (0.0–2.0)."""
        self.temperature = max(0.0, min(2.0, temp))