"""UI Зевса на Flet.

Класс собран из трёх зон:
  * boot — сплэш, трей, горячие клавиши, закрытие окна;
  * layout — сайдбар, главная, логи, настройки;
  * handlers — голос, чат, настройки, состояние.
"""
from __future__ import annotations

import os
import queue
import sys
import threading

import flet as ft

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_SRC_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ui.boot import ZeusBootMixin
from ui.handlers import ZeusHandlersMixin
from ui.layout import ZeusLayoutMixin
from ui.shell_globals import C, PALETTES, SECTIONS
from ui.theme import get_theme_manager
from ui.zeus_state import ZeusState, ZeusStateController


class ZeusUI(ZeusBootMixin, ZeusLayoutMixin, ZeusHandlersMixin):
    """Связывает контроллер с окном. Логика зон — в mixin-ах."""

    def __init__(self, page: ft.Page):
        self.page = page

        # --- Маршализация UI-обновлений через один поток ---
        # Все коллбэки контроллера (аудио, zeus-commands, boot, watchdog)
        # и обработчики событий Flet исполняются в разных потоках.
        # Конкурентные control.update() из нескольких потоков — известная
        # причина зависания отрисовки Flet («Working…»). Поэтому каждое
        # изменение контролов ставится в очередь и исполняется строго
        # последовательно единственным потоком zeus-ui.
        self._ui_queue: queue.Queue = queue.Queue()
        threading.Thread(
            target=self._ui_loop, daemon=True, name="zeus-ui"
        ).start()

        # Подписка на записи logging_service ([AUDIO]-диагностика → терминал)
        self._core_log_unsubscribe = None

        # Контроллер создаётся отложенно в фоновом потоке (см. _boot_worker),
        # чтобы тяжёлая инициализация движков (Vosk, аудио, Ollama, сканеры)
        # не блокировала отрисовку интерфейса на экране «Загрузка…».
        self.controller = None
        self._ready = False
        self._boot_error = None
        self._boot_timeout_sec = 40.0
        self._boot_thread: threading.Thread | None = None

        # Текущий выбранный раздел
        self.selected_index = 0

        # Атрибуты голографического визуализатора (ТЗ v1.2: центральный техно-круг)
        self.holo_image: ft.Image | None = None
        # Анимированный контейнер масштабирования при речи (ТЗ v1.2 §3)
        self.holo_container: ft.Container | None = None
        # Флаг для остановки анимации при закрытии окна (close-handler)
        self._holo_running = False

        # Статистика сессии (для лога голосового помощника)
        self._stats_cmds = 0

        # UI элементы
        self.sidebar: ft.Container | None = None
        self.content_area: ft.Container | None = None
        self.app_bar: ft.AppBar | None = None

        # --- Раздел 0: Голосовой помощник ---
        self.log_text: ft.Text | None = None
        self.voice_status: ft.Text | None = None
        self.sleep_button: ft.IconButton | None = None
        self.listen_button: ft.IconButton | None = None
        self.voice_switch: ft.Switch | None = None
        self.voice_content: ft.Container | None = None

        # --- Раздел 1: Настройки ---
        self.settings_content: ft.Container | None = None

        # Общий автомат. После загрузки движков boot подменяет его
        # на контроллерский — голограмма и микрофон смотрят в одно место.
        self.state_controller = ZeusStateController(ZeusState.IDLE)
        self._theme_manager = get_theme_manager()
        self.main_window = None

        self._watcher_started = False
        self._hotkey = None
        self._tray = None
        self._exit_requested = False

        # Мгновенный «сплэш» загрузки: UI отрисовывается сразу, а не после
        # инициализации движков. Окно не зависает на надписи «Загрузка…».
        self._build_splash()
        self._show_page_text("Загрузка ресурсов...")

        # Фоновая инициализация тяжёлых движков (2.1 — перенос в поток).
        self._boot_thread = threading.Thread(
            target=self._boot_worker, daemon=True, name="zeus-boot"
        )
        self._boot_thread.start()

        # Защита от бесконечного ожидания (2.2): если инициализация затянулась,
        # выводим понятное сообщение в UI/лог, а не висим на «Загрузка…».
        threading.Thread(
            target=self._timeout_guard, daemon=True, name="zeus-boot-timeout"
        ).start()



def main(page: ft.Page):
    ZeusUI(page)



if __name__ == "__main__":
    ft.run(main, view=ft.AppView.FLET_APP)
