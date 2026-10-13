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

# Атомарная запись JSON (кэш ответов): tmp + fsync + os.replace
try:
    from core.atomic_json import write_json_atomic
except Exception:  # noqa: BLE001 — допустимо при импорте вне проекта
    def write_json_atomic(path, data, *, indent=2, sort_keys=False):  # type: ignore[misc]
        """Заглушка на случай импорта модуля вне проекта: пишем как раньше."""
        import json as _json
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=indent)
            return True
        except Exception:  # noqa: BLE001
            return False


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

# Блокировка доступа к кэшу (RLock: _remember() держит лок и внутри вызывает
# _save_cache(), который тоже его захватывает). Генерация выполняется в
# отдельных потоках — без лока параллельные _remember() одновременно
# перезаписывали бы data/cache.json, теряя/перемешивая записи.
_cache_lock = threading.RLock()

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

        # --- Агентный каркас (Harness, ТЗ v1.4) ---
        # agent_enabled: False -> обычный стриминг «вопрос-ответ»;
        # True -> цикл с инструментами (запуск приложений, файлы, клики,
        # веб-поиск) через core.agent_harness.AgentHarness.
        self.agent_enabled = False
        self.agent_max_steps = 5
        self.agent_allow_write = False
        self.agent_allow_actions = False
        # Действие агента, которое ждёт «да» или «нет» в следующем сообщении.
        self._pending = None

        # Callbacks
        self.on_token: Callable[[str], None] | None = None
        self.on_complete: Callable[[str], None] | None = None
        self.on_error: Callable[[str], None] | None = None

        # История диалога
        self.conversation: list[dict[str, str]] = []

        # Флаг занятости
        self._is_busy = False
        # Лок для атомарной проверки-и-установки флага: параллельные ask()
        # из разных потоков иначе оба проходят проверку и запускают
        # генерацию одновременно.
        self._busy_lock = threading.Lock()

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
        with _cache_lock:
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
        """Сохраняет накопленный кэш на диск (best-effort).

        Вызывается только при удержанном _cache_lock (см. _remember).
        Запись атомарная: усечённый при краш cache.json означал бы потерю
        всей накопленной памяти ответов.
        """
        with _cache_lock:
            if not self._cache:
                return
            try:
                os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
                write_json_atomic(_CACHE_PATH, self._cache, indent=2)
            except Exception as exc:  # noqa: BLE001
                try:
                    from core.logging_service import error
                    error(f"[AI] не записал кэш ответов: {exc}")
                except Exception:
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
        with _cache_lock:
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
        with _cache_lock:
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
        with self._busy_lock:
            return self._is_busy

    # ------------------------------------------------------------------
    # Отправка сообщения
    # ------------------------------------------------------------------
    def ask(self, text: str):
        """Отправляет текст в LLM и запускает стриминг ответа.

        Сначала проверяет кэш частых команд: если запрос «заучен» (встроенный
        canned-ответ или накопленный из прошлых генераций), отвечает мгновенно
        без обращения к локальной модели Ollama.

        Если агент ждёт подтверждения, «да» и «нет» не идут в кэш и не
        запускают новый запрос к модели.

        Работает в отдельном потоке через синхронный ollama.chat — без asyncio,
        чтобы избежать ошибки «Event loop is closed».
        """
        from core.agent_harness import classify_confirmation

        decision = None
        with self._busy_lock:
            if self._is_busy:
                return
            if self._pending is not None:
                decision = classify_confirmation(text)
                if decision is None:
                    self._pending = None
            # --- Быстрый путь: ответ из кэша без обращения к Ollama ---
            cached = None if decision else self.get_cached_answer(text)
            # Занимаем флаг ДО запуска фонового потока: иначе параллельный
            # ask() пройдёт проверку занятости одновременно с нами.
            self._is_busy = True

        if decision is not None:
            threading.Thread(
                target=self._resume_pending,
                args=(text, decision == "yes"),
                daemon=True,
            ).start()
            return

        if cached is not None:
            self.conversation.append({"role": "user", "content": text})
            self.conversation.append({"role": "assistant", "content": cached})
            threading.Thread(
                target=self._emit_cached, args=(cached,), daemon=True
            ).start()
            return

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
            with self._busy_lock:
                self._is_busy = False

    def _run_sync(self, text: str):
        """Синхронный запрос к Ollama в отдельном потоке (без asyncio).

        Исключает ошибку «Event loop is closed»: используется синхронный
        ollama.chat вместо AsyncClient + event loop.

        Если включён агентный каркас (agent_enabled), сначала пробуем цикл
        с инструментами (core.agent_harness): модель может запускать
        приложения, читать файлы, кликать и искать в сети. При любой ошибке
        каркаса (модель без tool-calling, сбой клиента) — тихий откат на
        обычный стриминг ниже: чат никогда не должен «умирать» из-за Harness.
        """
        full_text = ""
        try:
            messages = []
            if self.system_prompt:
                messages.append({"role": "system", "content": self.system_prompt})
            messages.extend(self.conversation[-_MAX_HISTORY:])

            # --- Агентный каркас: цикл инструментов вместо чистого текста ---
            if self.agent_enabled:
                agent_text = self._run_agent(messages, text)
                if agent_text is not None:
                    full_text = agent_text
                    if self.on_token:
                        self.on_token(full_text)
                    if self.on_complete:
                        self.on_complete(full_text)
                    return

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
            with self._busy_lock:
                self._is_busy = False

    def _run_agent(self, messages: list[dict], text: str) -> str | None:
        """Цикл агентного каркаса. None -> откатиться на обычный стриминг.

        Возвращает финальный текст модели. История пополняется только финальным
        ответом (без служебных tool-сообщений — они одноразовые), а «запоминать»
        в кэш можно лишь ответы, полученные БЕЗ инструментов: закэшированный
        «Запускаю X» в следующий раз ничего не запустил бы.
        """
        try:
            from core.agent_harness import AgentHarness
        except Exception as exc:  # noqa: BLE001 — модуль обязан быть, но...
            print(f"[AI] [HARNESS] каркас недоступен: {exc}")
            return None

        try:
            harness = AgentHarness(on_event=print)
            result = harness.run(
                self.client,
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_steps=self.agent_max_steps,
                allow_write=self.agent_allow_write,
                allow_actions=self.agent_allow_actions,
            )
        except Exception as exc:  # noqa: BLE001 — модель без tools и т.п.
            print(f"[AI] [HARNESS] цикл не удался, откат на стриминг: {exc}")
            return None

        full_text = result.text or ""
        if result.pending is not None:
            self._pending = result.pending
        self.conversation.append({"role": "assistant", "content": full_text})
        if result.pending is None and not result.used_tools:
            # Ответ без инструментов — обычный чат, кэшируем как раньше.
            self._remember(text, full_text)
        return full_text

    def _resume_pending(self, text: str, approved: bool) -> None:
        """Выполняет или отклоняет действие, которое ждало «да»."""
        pending = self._pending
        self._pending = None
        full_text = ""
        try:
            if pending is None:
                return
            self.conversation.append({"role": "user", "content": text})
            from core.agent_harness import AgentHarness
            harness = AgentHarness(on_event=print)
            result = harness.resume(
                self.client,
                pending,
                approved=approved,
                model=self.model,
                temperature=self.temperature,
            )
            full_text = result.text or ""
            if result.pending is not None:
                self._pending = result.pending
            self.conversation.append({"role": "assistant", "content": full_text})
            if self.on_token:
                self.on_token(full_text)
            if self.on_complete:
                self.on_complete(full_text)
        except Exception as exc:  # noqa: BLE001
            self._trigger_error(f"Не подтвердил действие: {exc}")
        finally:
            with self._busy_lock:
                self._is_busy = False

    def _trigger_error(self, message: str):
        """Уведомляет UI об ошибке через on_error (если зарегистрирован)."""
        if self.on_error:
            self.on_error(message)

    # ------------------------------------------------------------------
    # Управление историей
    # ------------------------------------------------------------------
    def has_pending_action(self) -> bool:
        """True, пока агент ждёт «да» или «нет»."""
        return self._pending is not None

    def cancel_pending(self) -> None:
        """Снимает ожидание подтверждения, не трогая историю чата."""
        self._pending = None

    def new_chat(self):
        """Очищает историю диалога и невыполненное подтверждение."""
        self._pending = None
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

    def set_agent_config(
        self,
        enabled: bool,
        max_steps: int = 5,
        allow_write: bool = False,
        allow_actions: bool = False,
    ) -> None:
        """Настраивает агентный каркас (Harness) без перезапуска.

        Вызывается из controller._apply_settings / update_settings, чтобы
        тумблеры настроек применялись к следующему же запросу к модели.
        """
        self.agent_enabled = bool(enabled)
        try:
            self.agent_max_steps = max(1, min(20, int(max_steps)))
        except (TypeError, ValueError):
            self.agent_max_steps = 5
        self.agent_allow_write = bool(allow_write)
        self.agent_allow_actions = bool(allow_actions)
        state = "вкл" if self.agent_enabled else "выкл"
        print(
            f"[AI] [HARNESS] агентный каркас: {state} "
            f"(шагов={self.agent_max_steps}, "
            f"запись файлов={'да' if self.agent_allow_write else 'нет'}, "
            f"действия={'да' if self.agent_allow_actions else 'нет'})"
        )