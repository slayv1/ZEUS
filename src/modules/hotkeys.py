"""Глобальные горячие клавиши (Global Hotkeys) для Зевса.

Позволяет в любой программе Windows принудительно вызвать ассистента
сочетанием клавиш (по умолчанию Ctrl + Alt + Space), дублируя тем самым
голосовую активацию по ключевому слову «Зевс».

Реализация на pywin32 (RegisterHotKey + поток с перекачкой сообщений).
Не требует скрытого окна: hotkey регистрируется на поток, который затем
крутит стандартный цикл GetMessage и реагирует на WM_HOTKEY.

Если pywin32 недоступен или ОС не Windows — менеджер корректно
деградирует (start() вернёт False) и не ломает запуск приложения.
"""
from __future__ import annotations

import os
import threading

# Модификаторы Windows (из winuser.h)
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

# Коды виртуальных клавиш
VK_SPACE = 0x20

# Идентификатор hotkey (произвольный)
_HOTKEY_ID = 0x4A01

# WM_HOTKEY
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


class HotkeyManager:
    """Менеджер одной глобальной горячей клавиши.

    Attributes:
        on_hotkey: callable() — вызывается в фоновом потоке при нажатии комбинации.
    """

    def __init__(self):
        self.on_hotkey = None
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self, callback,
              mods: int = MOD_CONTROL | MOD_ALT,
              vk: int = VK_SPACE,
              key_desc: str = "Ctrl+Alt+Space") -> bool:
        """Регистрирует глобальную клавишу и запускает поток прослушивания.

        Returns:
            True при успешной регистрации, иначе False (без исключений наружу).
        """
        self.on_hotkey = callback
        if os.name != "nt":
            print("[Hotkeys] Глобальные клавиши доступны только на Windows")
            return False

        try:
            import win32api
            import win32gui  # noqa: F401 — для корректной инициализации очереди
        except Exception as e:  # noqa: BLE001
            print(f"[Hotkeys] pywin32 недоступен, горячие клавиши выключены: {e}")
            return False

        # Регистрируем hotkey на ТЕКУЩИЙ поток — он будет владельцем очереди.
        try:
            import win32gui
            ok = win32gui.RegisterHotKey(None, _HOTKEY_ID, mods, vk)
        except Exception as e:  # noqa: BLE001
            print(f"[Hotkeys] Не удалось зарегистрировать {key_desc}: {e}")
            return False
        if not ok:
            print(f"[Hotkeys] Не удалось зарегистрировать {key_desc} (вероятно, комбинация занята другой программой)")
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._message_loop, args=(key_desc,), daemon=True
        )
        self._thread.start()
        print(f"[Hotkeys] Глобальная клавиша {key_desc} зарегистрирована")
        return True

    def stop(self) -> None:
        """Останавливает прослушивание горячей клавиши."""
        self._running = False
        # Разбудить заблокированный GetMessage поток через WM_QUIT.
        try:
            import win32api
            import win32con
            win32api.PostThreadMessage(
                self._thread.ident if self._thread else 0, WM_QUIT, 0, 0
            )
        except Exception:  # noqa: BLE001
            pass

    def _message_loop(self, key_desc: str) -> None:
        """Цикл Windows message loop на потоке-владельце hotkey."""
        import win32gui

        try:
            while self._running:
                msg = win32gui.GetMessage(None, 0, 0, 0)
                if msg is None:
                    break
                if msg.message == WM_QUIT:
                    break
                if msg.message == WM_HOTKEY:
                    if self.on_hotkey:
                        try:
                            self.on_hotkey()
                        except Exception as e:  # noqa: BLE001
                            print(f"[Hotkeys] Ошибка в обработчике: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[Hotkeys] Ошибка цикла сообщений: {e}")
        finally:
            self._running = False
            try:
                import win32gui
                win32gui.UnregisterHotKey(None, _HOTKEY_ID)
            except Exception:  # noqa: BLE001
                pass
            print(f"[Hotkeys] Слушатель {key_desc} остановлен")