"""Глобальная горячая клавиша Ctrl+Alt+Space для Windows."""
from __future__ import annotations

import threading

class GlobalHotkeyManager:
    """Потокобезопасный менеджер глобальных комбинаций клавиш."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hotkeys = {}

    def start(self, callback, hotkey: str = "ctrl+alt+space") -> bool:
        """Регистрирует основную комбинацию один раз."""
        return self.register(hotkey, callback)

    def register(self, hotkey: str, callback) -> bool:
        """Добавляет независимую глобальную комбинацию клавиш."""
        try:
            import keyboard
        except Exception as exc:  # noqa: BLE001
            print(f"[Hotkeys] Библиотека keyboard недоступна: {exc}")
            return False

        with self._lock:
            if hotkey in self._hotkeys:
                return True
            try:
                handle = keyboard.add_hotkey(
                    hotkey, callback, suppress=False
                )
                self._hotkeys[hotkey] = handle
                print(f"[Hotkeys] Глобальная клавиша {hotkey} зарегистрирована")
                return True
            except Exception as exc:  # noqa: BLE001
                print(f"[Hotkeys] Не удалось зарегистрировать {hotkey}: {exc}")
                return False

    def stop(self) -> None:
        """Удаляет зарегистрированную комбинацию без блокировки UI."""
        try:
            import keyboard
        except Exception:
            keyboard = None

        with self._lock:
            handles = list(self._hotkeys.items())
            self._hotkeys.clear()
            if keyboard is not None:
                for hotkey, handle in handles:
                    try:
                        keyboard.remove_hotkey(handle)
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"[Hotkeys] Не удалось удалить {hotkey}: {exc}"
                        )

    def _on_hotkey(self) -> None:
        """Оставлен для совместимости со старыми интеграциями."""
        return

    @property
    def is_running(self) -> bool:
        with self._lock:
            return bool(self._hotkeys)

    @property
    def registered_hotkeys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._hotkeys)


HotkeyManager = GlobalHotkeyManager