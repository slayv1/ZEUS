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
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from core.agent_tools import RISK_ACTION, RISK_WRITE, ToolRegistry, get_registry
from core.command_effects import describe_effect

# Сколько инструментов модель может вызвать за один запрос (по умолчанию).
DEFAULT_MAX_STEPS = 5

# Текст, если лимит шагов исчерпан, а модель так и не ответила по-человечески.
_LIMIT_TEXT = (
    "Не успел завершить задачу за отведённые шаги, сэр. "
    "Попробуйте переформулировать запрос."
)


@dataclass
class PendingAction:
    """Действие, которое ждёт «да» или «нет» в том же диалоге."""

    name: str
    arguments: dict[str, Any]
    prompt: str
    messages: list[dict[str, Any]]
    rest_calls: list[dict[str, Any]]
    allow_write: bool
    allow_actions: bool
    max_steps: int
    step: int


@dataclass
class HarnessResult:
    """Итог агентного цикла."""

    text: str
    used_tools: list[str] = field(default_factory=list)
    steps: int = 0
    limit_reached: bool = False
    pending: PendingAction | None = None


_YES = {
    "да", "давай", "ага", "угу", "ок", "окей", "хорошо",
    "подтверждаю", "подтверди", "yes", "выполняй",
}
_NO = {"нет", "не", "не надо", "отмена", "отмени", "no", "нельзя", "стоп"}


def classify_confirmation(text: str) -> str | None:
    """«yes» / «no» для ожидающего действия, иначе None (новый запрос)."""
    s = (text or "").lower().strip()
    s = re.sub(r"[,.;:!?«»]+", " ", s)
    s = re.sub(r"\b(зевс|сэр|пожалуйста)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s in _YES or s.startswith("да "):
        return "yes"
    if s in _NO or s.startswith("нет"):
        return "no"
    return None


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
        return self._loop(
            client,
            messages=list(messages),
            model=model,
            temperature=temperature,
            max_steps=max(1, int(max_steps)),
            allow_write=allow_write,
            allow_actions=allow_actions,
            start_step=1,
            used=[],
        )

    def resume(
        self,
        client: Any,
        pending: PendingAction,
        *,
        approved: bool,
        model: str,
        temperature: float = 0.7,
    ) -> HarnessResult:
        """Продолжает цикл после «да» или «нет»."""
        working = list(pending.messages)
        used: list[str] = []
        if approved:
            payload = self.registry.execute(
                pending.name,
                pending.arguments,
                allow_write=pending.allow_write,
                allow_actions=pending.allow_actions,
            )
            used.append(pending.name)
        else:
            payload = json.dumps(
                {"success": False, "error": "Пользователь отклонил действие."},
                ensure_ascii=False,
            )
        working.append({
            "role": "tool",
            "tool_name": pending.name,
            "content": payload,
        })
        self._emit(f"подтверждение {pending.name}: {'да' if approved else 'нет'}")

        paused = self._apply_calls(
            working,
            pending.rest_calls,
            used,
            allow_write=pending.allow_write,
            allow_actions=pending.allow_actions,
            max_steps=pending.max_steps,
            step=pending.step,
        )
        if paused is not None:
            paused.used_tools = list(used)
            return paused
        return self._loop(
            client,
            messages=working,
            model=model,
            temperature=temperature,
            max_steps=pending.max_steps,
            allow_write=pending.allow_write,
            allow_actions=pending.allow_actions,
            start_step=pending.step + 1,
            used=used,
        )

    # -- внутренности -------------------------------------------------------
    def _loop(
        self,
        client: Any,
        *,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        max_steps: int,
        allow_write: bool,
        allow_actions: bool,
        start_step: int,
        used: list[str],
    ) -> HarnessResult:
        working = list(messages)
        schemas = self.registry.schemas()
        if start_step > max_steps:
            return self._final_answer(
                client, working, model, temperature, schemas, used, max_steps
            )

        for step in range(start_step, max_steps + 1):
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
                return HarnessResult(text=content, used_tools=list(used), steps=step)

            plain = _plain_tool_call_list(tool_calls)
            working.append({
                "role": "assistant",
                "content": content,
                "tool_calls": plain,
            })
            paused = self._apply_calls(
                working,
                plain,
                used,
                allow_write=allow_write,
                allow_actions=allow_actions,
                max_steps=max_steps,
                step=step,
            )
            if paused is not None:
                paused.used_tools = list(used)
                return paused

        self._emit(f"лимит шагов ({max_steps}) исчерпан")
        return HarnessResult(
            text=_LIMIT_TEXT,
            used_tools=list(used),
            steps=max_steps,
            limit_reached=True,
        )

    def _final_answer(
        self,
        client: Any,
        working: list[dict[str, Any]],
        model: str,
        temperature: float,
        schemas: list[dict[str, Any]],
        used: list[str],
        max_steps: int,
    ) -> HarnessResult:
        """Один ответ модели после подтверждения на последнем шаге."""
        response = client.chat(
            model=model,
            messages=working,
            tools=schemas,
            options={"temperature": temperature},
            stream=False,
        )
        message = _field(response, "message", None) or {}
        content = _field(message, "content", "") or ""
        if _field(message, "tool_calls", None):
            self._emit(f"лимит шагов ({max_steps}) исчерпан")
            return HarnessResult(
                text=_LIMIT_TEXT,
                used_tools=list(used),
                steps=max_steps,
                limit_reached=True,
            )
        return HarnessResult(text=content, used_tools=list(used), steps=max_steps)

    def _apply_calls(
        self,
        working: list[dict[str, Any]],
        calls: list[dict[str, Any]],
        used: list[str],
        *,
        allow_write: bool,
        allow_actions: bool,
        max_steps: int,
        step: int,
    ) -> HarnessResult | None:
        for index, call in enumerate(calls):
            name = call["function"]["name"]
            arguments = call["function"]["arguments"]
            tool = self.registry.get(name)
            if tool is not None and _needs_confirm(tool, allow_write, allow_actions):
                prompt = describe_effect(name, arguments)
                return HarnessResult(
                    text=prompt,
                    steps=step,
                    pending=PendingAction(
                        name=name,
                        arguments=dict(arguments),
                        prompt=prompt,
                        messages=list(working),
                        rest_calls=list(calls[index + 1:]),
                        allow_write=allow_write,
                        allow_actions=allow_actions,
                        max_steps=max_steps,
                        step=step,
                    ),
                )
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
            self._emit(f"шаг {step}/{max_steps}: {name} -> {payload[:160]}")
        return None

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


def _needs_confirm(tool: Any, allow_write: bool, allow_actions: bool) -> bool:
    """Спрашивать «да», только если настройка уже разрешает это действие."""
    if tool.risk == RISK_WRITE and allow_write:
        return True
    if tool.risk == RISK_ACTION and allow_actions:
        return True
    return False
