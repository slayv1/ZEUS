# -*- coding: utf-8 -*-
"""Централизованный асинхронный диспетчер команд «Зевса» v1.2.

Устраняет плашку «Working...» в Flet: все команды выполняются в фоне,
а event loop освобождается мгновенно (asyncio.sleep(0) перед работой).

Особенности:
  - Автоопределение типа обработчика: async-функции исполняются в loop,
    синхронные уходят в пул потоков через asyncio.to_thread.
  - Ошибки обработчиков изолированы и логируются, диспетчер не падает.
  - dispatch() не ждёт завершения команды — управление возвращается сразу.
"""
from __future__ import annotations

import inspect
import threading
import traceback


class CommandRouter:
    """Централизованный асинхронный диспетчер команд для ассистента «Зевс» v1.2."""

    def __init__(self):
        self.routes: dict[str, object] = {}

    def register(self, command_name: str, func) -> None:
        """Регистрация обработчика команды."""
        if command_name in self.routes:
            print(f"[Router] Перезапись обработчика: '{command_name}'")
        self.routes[command_name] = func

    def unregister(self, command_name: str) -> None:
        self.routes.pop(command_name, None)

    def has(self, command_name: str) -> bool:
        return command_name in self.routes

    async def dispatch(self, command_name: str, *args, **kwargs) -> None:
        """Асинхронный запуск команды в фоне.

        Управление возвращается вызывающему (UI) мгновенно — до завершения
        команды, что предотвращает плашку «Working...» в Flet.
        """
        import asyncio

        if command_name not in self.routes:
            print(f"[Router] Неизвестная команда: '{command_name}'")
            return

        handler = self.routes[command_name]

        async def wrapper():
            try:
                # Даём интерфейсу Flet мгновенно обновить состояние.
                await asyncio.sleep(0)
                if inspect.iscoroutinefunction(handler):
                    await handler(*args, **kwargs)
                else:
                    # Синхронные задачи уходят в пул потоков, не блокируя Event Loop.
                    await asyncio.to_thread(handler, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                print(f"[Router Error] Ошибка в команде '{command_name}': {exc}")
                traceback.print_exc()

        # Создаём фоновую задачу — UI остаётся полностью отзывчивым.
        asyncio.create_task(wrapper())

    # ------------------------------------------------------------------
    # Интеграция с существующей потоковой архитектурой (не-async код)
    # ------------------------------------------------------------------
    def dispatch_sync(self, command_name: str, *args, **kwargs) -> None:
        """Запуск команды из синхронного кода (threads/обработчики кнопок).

        В проекте Зевса UI-события уже обрабатываются в фоновых потоках
        (_run_bg / command-loop), где asyncio.create_task недоступен.
        dispatch_sync поднимает (один раз) приватный event loop в отдельном
        daemon-потоке и выполняет dispatch там — семантика та же: вызов
        возвращается мгновенно, команда работает в фоне.
        """
        import asyncio

        loop = _get_shared_loop()
        asyncio.run_coroutine_threadsafe(
            self._dispatch_and_wait(command_name, *args, **kwargs), loop
        )

    async def _dispatch_and_wait(self, command_name: str, *args, **kwargs) -> None:
        """Внутренний вызов dispatch, дополненный task_done-семантикой."""
        import asyncio

        if command_name not in self.routes:
            print(f"[Router] Неизвестная команда: '{command_name}'")
            return
        handler = self.routes[command_name]
        try:
            await asyncio.sleep(0)
            if inspect.iscoroutinefunction(handler):
                await handler(*args, **kwargs)
            else:
                await asyncio.to_thread(handler, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            print(f"[Router Error] Ошибка в команде '{command_name}': {exc}")
            traceback.print_exc()


# --- Синглтон-роутер проекта --------------------------------------------
router = CommandRouter()

_shared_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_shared_loop() -> asyncio.AbstractEventLoop:
    """Лениво поднимает общий event loop в daemon-потоке для dispatch_sync."""
    import asyncio

    global _shared_loop
    with _loop_lock:
        if _shared_loop is None or _shared_loop.is_closed():
            ready = threading.Event()

            def _run_loop():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                globals()["_shared_loop"] = loop
                ready.set()
                loop.run_forever()

            threading.Thread(
                target=_run_loop, daemon=True, name="zeus-router-loop"
            ).start()
            ready.wait(timeout=5.0)
        return _shared_loop
