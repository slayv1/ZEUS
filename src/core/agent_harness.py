# -*- coding: utf-8 -*-
"""Агентный каркас (Harness): цикл оркестрации инструментов LLM (ТЗ v1.4).

Обычный чат-запрос — это «один вопрос — один ответ». Агентный каркас даёт
модели цикл: модель решает, что для ответа нужен инструмент, Ollama возвращает
``tool_calls``, каркас выполняет их и возвращает результаты сообщением
``role=tool`` — и так, пока модель не соберёт финальный ответ и не исчерпается
лимит шагов.

Роль каркаса в системе::

    ChatEngine._run_sync ──> AgentHarness.run ──> client.chat(tools=...)
                                   │                    │
                                   │  tool_calls        │
                                   │<───────────────────┤
                                   ├──> ToolRegistry.execute(...)
                                   │  {"role":"tool",   │
                                   │   "tool_name":...} │
                                   │───────────────────>│
                                   └──> финальный текст (HarnessResult)

Ключевые решения:

* вызовы идут НЕ в потоке (``stream=False``): промежуточные ответы с
  tool_calls не имеет смысла стримить, а финальный текст ChatEngine
  отдаёт в UI/TTS одним куском — как уже делает кэшированный ответ;
* каждое сообщение ``role=tool`` несёт JSON-результат инструмента, включая
  ошибки — модель должна видеть сбои, чтобы скорректировать план;
* при исчерпании шагов возвращается честный текст «не успел», а не последний
  обрывок рассуждений;
* каркас НЕ пишет в историю ChatEngine сам — это решение вызывающей стороны
  (см. ``ChatEngine._run_sync``: после инструментов ответ не кэшируется).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from core.agent_tools import ToolRegistry, get_registry

# Сколько инструментов модель может вызвать за один запрос (по умолчанию).
DEFAULT_MAX_STEPS = 5

# Текст, если лимит шагов исчерпан, а модель так и не ответила по-человечески.
_LIMIT_TEXT = (
    "Не успел завершить задачу за отведённые шаги, сэр. "
    "Попробуйте переформулировать запрос."
)


@dataclass
class HarnessResult:
    """Итог агентного цикла."""

    text: str
    used_tools: list[str] = field(default_factory=list)
    steps: int = 0
    limit_reached: bool = False


class AgentHarness:
    """Цикл «tool_calls -> исполнение -> role=tool -> повтор» вокруг Ollama."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        *,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.registry = registry if registry is not None else get_registry()
        # Диагностические события цикла ([AI] [HARNESS] ...); None = молчим.
        self.on_event = on_event

    # -- публичное API ------------------------------------------------------
    def run(
        self,
        client: Any,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        max_steps: int = DEFAULT_MAX_STEPS,
        allow_write: bool = False,
        allow_actions: bool = False,
    ) -> HarnessResult:
        """Один агентный цикл. Возвращает финальный текст и след инструментов.

        ``client`` — ollama.Client (или подмена в тестах) с методом chat().
        Исключения клиента пробрасываются наружу: вызывающая сторона
        (ChatEngine) сама решает, падать ли на обычный стриминг.
        """
        working = list(messages)
        schemas = self.registry.schemas()
        steps = max(1, int(max_steps))
        used: list[str] = []

        for step in range(1, steps + 1):
            response = client.chat(
                model=model,
                messages=working,
                tools=schemas,
                options={"temperature": temperature},
                stream=False,
            )
            message = _field(response, "message", None) or {}
            content = _field(message, "content", "") or ""
            tool_calls = _field(message, "tool_calls", None) or []

            if not tool_calls:
                return HarnessResult(text=content, used_tools=used, steps=step)

            # Фиксируем в истории ход модели: без него Ollama не примет
            # результаты инструментов на следующем шаге.
            working.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [_plain_tool_call(tc) for tc in tool_calls],
            })

            for call in _plain_tool_call_list(tool_calls):
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                payload = self.registry.execute(
                    name,
                    arguments,
                    allow_write=allow_write,
                    allow_actions=allow_actions,
                )
                used.append(name)
                working.append({
                    "role": "tool",
                    "tool_name": name,
                    "content": payload,
                })
                self._emit(f"шаг {step}/{steps}: {name} -> {payload[:160]}")

        self._emit(f"лимит шагов ({steps}) исчерпан")
        return HarnessResult(
            text=_LIMIT_TEXT, used_tools=used, steps=steps, limit_reached=True
        )

    # -- внутренности -------------------------------------------------------
    def _emit(self, text: str) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(f"[AI] [HARNESS] {text}")
        except Exception:  # noqa: BLE001 — диагностика не должна ломать цикл
            pass


# ---------------------------------------------------------------------------
# Работа с форматами ответов Ollama (dict- и attribute-доступ)
# ---------------------------------------------------------------------------
def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Достаёт поле из dict или объекта (ChatResponse/Message — оба мира)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    value = getattr(obj, name, default)
    return default if value is None else value


def _plain_tool_call(raw: Any) -> dict[str, Any]:
    """Нормализует один tool_call к {"function": {"name", "arguments"}}."""
    fn = _field(raw, "function", raw)
    name = _field(fn, "name", "") or ""
    arguments = _field(fn, "arguments", {}) or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            arguments = {"_raw": arguments}
    if not isinstance(arguments, dict):
        arguments = {"_raw": str(arguments)}
    return {"function": {"name": str(name), "arguments": dict(arguments)}}


def _plain_tool_call_list(tool_calls: Any) -> list[dict[str, Any]]:
    return [_plain_tool_call(tc) for tc in tool_calls]
