import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import urllib.request

import customtkinter as ctk
import pyttsx3
import speech_recognition as sr
from ollama import AsyncClient

# Обеспечиваем доступность модулей src/ при запуске файла напрямую
# (python src/ui/main_ctk.py), когда корень src/ не в sys.path.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_ROOT = os.path.dirname(_THIS_DIR)  # src/
for _p in (_SRC_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import config
from core import actions

# === Глобальные константы ===
APP_TITLE = "Зевс v1.1"
OLLAMA_HOST = "http://localhost:11434"
ICON_PATH = config.resource_path("zeus.ico") if os.path.exists(config.resource_path("zeus.ico")) else None


class ZeusApp:
    def __init__(self):
        # Загрузка настроек (автоматически при запуске)
        self.settings = config.load_settings()
        self.model_name = self.settings.get("model", "llama3.1")
        self.system_prompt = self.settings.get("system_prompt", "")
        self.voice_enabled = self.settings.get("voice_enabled", True)

        # Цветовая схема выбранной темы
        self.colors = config.get_theme(self.settings.get("theme", "Dark Gold"))

        self.root = ctk.CTk()
        self.root.title(APP_TITLE)
        self.root.geometry("860x680")
        if ICON_PATH:
            try:
                self.root.iconbitmap(ICON_PATH)
            except Exception:
                pass
        self.root.resizable(False, False)

        # Асинхронный клиент Ollama (мозг Зевса)
        self.ollama = AsyncClient(host=OLLAMA_HOST)

        # Голосовой ввод (распознавание речи)
        self.recognizer = sr.Recognizer()
        self._mic_available = self._check_microphone()

        # Флаг прерывания озвучки
        self._stop_speaking = False

        # История сообщений текущей сессии (для контекста LLM)
        self.conversation = []

        # Анимация индикатора активности
        self._thinking = False
        self._anim_index = 0

        # Отдельный event loop в фоновом потоке, чтобы GUI не зависал
        self.loop = asyncio.new_event_loop()
        self._start_async_loop()

        self._apply_theme()
        self._build_ui()

        # Проверка наличия и запуска службы Ollama
        self._check_ollama_on_startup()
        
        # Запускаем фоновую индексацию дисков
        self._start_background_indexing()

        # Этап 12: запускаем реактивный мониторинг подключений (флешек)
        self._start_drive_monitor()

    # ------------------------------------------------------------------
    # Темы и оформление
    # ------------------------------------------------------------------
    def _apply_theme(self):
        """Применяет текущую тему к CustomTkinter."""
        ctk.set_appearance_mode("dark")
        c = self.colors
        self.root.configure(fg_color=c["bg"])

    def _refresh_colors(self):
        """Обновляет self.colors при смене темы."""
        self.colors = config.get_theme(self.settings.get("theme", "Dark Gold"))

    # ------------------------------------------------------------------
    # Асинхронный loop
    # ------------------------------------------------------------------
    def _start_async_loop(self):
        def _run_loop():
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        thread = threading.Thread(target=_run_loop, daemon=True)
        thread.start()

    def _check_microphone(self) -> bool:
        """Проверяет доступность микрофона (через sounddevice)."""
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            # Ищем входное устройство (микрофон)
            for i, device in enumerate(devices):
                if device['max_input_channels'] > 0:
                    self._mic_device_index = i
                    self._mic_device_name = device['name']
                    print(f"[DEBUG] Microphone found: {device['name']} (index {i})")
                    return True
            self._mic_device_index = None
            self._mic_device_name = None
            print("[DEBUG] No microphone found")
            return False
        except Exception as e:  # noqa: BLE001
            self._mic_device_index = None
            self._mic_device_name = None
            print(f"[DEBUG] Microphone check error: {e}")
            return False

    # ------------------------------------------------------------------
    # Проверка / запуск Ollama
    # ------------------------------------------------------------------
    def _start_background_indexing(self):
        """Запускает фоновую индексацию дисков C: и D:."""
        def _index_thread():
            try:
                from core.file_scanner import rebuild_system_index
                result = rebuild_system_index(max_workers=4)
                if result.get("success"):
                    self._set_status(f"Индексация завершена. Найдено {result.get('files_count', 0)} программ")
                else:
                    self._set_status("Готов к работе")
            except Exception:
                self._set_status("Готов к работе")
        
        threading.Thread(target=_index_thread, daemon=True).start()
        self._set_status("Индексация дисков... Это займёт 1-2 минуты")

    # ------------------------------------------------------------------
    # Этап 12: Реактивный мониторинг подключений (флешек)
    # ------------------------------------------------------------------
    def _start_drive_monitor(self):
        """Запускает фоновый монитор «горячего» подключения носителей."""
        try:
            actions.start_drive_monitor(
                on_new_drive=self._on_new_drive,
                on_drive_removed=self._on_drive_removed,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[DEBUG] Drive monitor start error: {exc}")

    def _on_new_drive(self, drive: str):
        """Вызывается монитором при обнаружении нового носителя.

        Задаёт пользователю голосовой вопрос о сканировании.
        """
        # Запоминаем ожидающий диск
        actions.set_pending_drive_scan(drive)
        question = (
            f"Обнаружен новый накопитель {drive}, сэр. "
            "Нужно ли просканировать его на наличие программ?"
        )
        self.root.after(0, self._append_chat, "Зевс", question)
        if self.voice_enabled:
            self.root.after(0, self._speak, question)
        else:
            self._set_status(f"Новый носитель {drive}. Сказать «да» или «сканируй» для индексации.")

    def _on_drive_removed(self, drive: str):
        """Вызывается монитором при отключении носителя.

        Уведомляет, что внешние записи очищены из индекса.
        """
        msg = f"Носитель {drive} отключён. Внешние программы удалены из индекса, сэр."
        self.root.after(0, self._append_chat, "Зевс", msg)
        self._set_status(msg)

    def _start_drive_scan_monitor(self, drive: str):
        """Мониторит завершение локального сканирования флешки и сообщает итог."""
        def _monitor():
            # Ждём, пока confirm_drive_scan запишет результат в _last_result
            for _ in range(120):  # до 2 минут
                res = getattr(actions.confirm_drive_scan, "_last_result", None)
                if res is not None and res.get("drive") == drive:
                    added = res.get("added", 0)
                    if added > 0:
                        done_msg = (
                            f"Готово, сэр. На {drive} найдено и добавлено "
                            f"{added} программ(ы) в индекс как внешние."
                        )
                    else:
                        done_msg = f"На {drive} не найдено исполняемых файлов, сэр."
                    self.root.after(0, self._append_chat, "Зевс", done_msg)
                    if self.voice_enabled:
                        self.root.after(0, self._speak, done_msg)
                    else:
                        self._set_status(done_msg)
                    # Сбрасываем результат, чтобы не сработало повторно
                    actions.confirm_drive_scan._last_result = None
                    return
                threading.Event().wait(1)
            # Таймаут
            timeout_msg = f"Не удалось завершить сканирование {drive} вовремя, сэр."
            self.root.after(0, self._append_chat, "Зевс", timeout_msg)
            self._set_status(timeout_msg)

        threading.Thread(target=_monitor, daemon=True).start()

    def _check_ollama_on_startup(self):
        """Проверяет доступность Ollama и пытается запустить службу."""
        threading.Thread(target=self._ollama_check_thread, daemon=True).start()

    def _ollama_check_thread(self):
        try:
            with urllib.request.urlopen(OLLAMA_HOST, timeout=2) as resp:
                if resp.status == 200:
                    self._set_status("Готов к работе")
                    return
        except Exception:
            pass

        # Служба не отвечает — пытаемся запустить
        self._set_status("Запуск службы Ollama...")
        started = self._try_start_ollama()
        if started:
            self._set_status("Ollama запущена. Готов к работе")
        else:
            self._set_status("⚠ Ollama не запущена! Запустите 'ollama serve'")

    def _try_start_ollama(self) -> bool:
        """Пытается запустить ollama serve. Возвращает True при успехе."""
        try:
            # Пытаемся запустить фоновую службу Ollama
            if os.name == "nt":
                # Windows: запуск через start, без окна
                subprocess.Popen(
                    "ollama serve",
                    shell=True,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                )
            else:
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            return False

        # Ждём, пока служба поднимется (до 15 сек)
        for _ in range(30):
            try:
                with urllib.request.urlopen(OLLAMA_HOST, timeout=1) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            threading.Event().wait(0.5)
        return False

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------
    def _build_ui(self):
        c = self.colors
        # Заголовок с золотым акцентом
        self.title_label = ctk.CTkLabel(
            self.root,
            text="⚡ ЗЕВС ⚡",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color=c["accent"],
            fg_color="transparent",
        )
        self.title_label.pack(pady=(14, 2))

        self.subtitle_label = ctk.CTkLabel(
            self.root,
            text="интеллектуальный ассистент",
            font=ctk.CTkFont(size=12),
            text_color=c["text_dim"],
            fg_color="transparent",
        )
        self.subtitle_label.pack(pady=(0, 8))

        # История чата (вывод ответов Зевса)
        self.chat_box = ctk.CTkTextbox(
            self.root,
            width=800,
            height=330,
            state="disabled",
            font=ctk.CTkFont(size=13),
            wrap="word",
            fg_color=c["surface"],
            text_color=c["text"],
            border_color=c["accent"],
            border_width=1,
        )
        self.chat_box.pack(pady=8, padx=24, fill="both", expand=True)

        # Контекстное меню для копирования (правая кнопка мыши)
        try:
            self._build_context_menu()
        except Exception as exc:  # noqa: BLE001
            import traceback
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ctx_err.log"), "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())

        # Поле ввода текста
        self.input_entry = ctk.CTkEntry(
            self.root,
            placeholder_text="Введите сообщение для Зевса или нажмите «Слушать»...",
            width=640,
            height=42,
            font=ctk.CTkFont(size=13),
            fg_color=c["surface"],
            text_color=c["text"],
            border_color=c["electric"],
            border_width=1,
            placeholder_text_color=c["text_dim"],
        )
        self.input_entry.pack(pady=(0, 8))
        self.input_entry.bind("<Return>", lambda e: self._on_send())

        # Кнопки управления
        self.btn_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        self.btn_frame.pack(pady=(0, 4))

        self.send_button = ctk.CTkButton(
            self.btn_frame,
            text="Отправить",
            width=140,
            height=40,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=c["accent"],
            text_color=c["accent_text"],
            hover_color=c["accent_hover"],
            command=self._on_send,
        )
        self.send_button.grid(row=0, column=0, padx=5)

        self.listen_button = ctk.CTkButton(
            self.btn_frame,
            text="🎤 Слушать",
            width=140,
            height=40,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=c["electric"],
            text_color=c["electric_text"],
            hover_color=c["accent_hover"],
            command=self._on_listen,
        )
        self.listen_button.grid(row=0, column=1, padx=5)

        if not self._mic_available:
            self.listen_button.configure(state="disabled")
            print("[DEBUG] Listen button disabled - no microphone")
        else:
            print("[DEBUG] Listen button enabled - microphone available")

        self.new_chat_button = ctk.CTkButton(
            self.btn_frame,
            text="🗑 Новый чат",
            width=140,
            height=40,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=c["surface_2"],
            text_color=c["accent"],
            border_color=c["accent"],
            border_width=1,
            hover_color=c["surface"],
            command=self._on_new_chat,
        )
        self.new_chat_button.grid(row=0, column=2, padx=5)

        self.settings_button = ctk.CTkButton(
            self.btn_frame,
            text="⚙",
            width=40,
            height=40,
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color=c["surface_2"],
            text_color=c["accent"],
            border_color=c["accent"],
            border_width=1,
            hover_color=c["surface"],
            command=self._open_settings,
        )
        self.settings_button.grid(row=0, column=3, padx=5)

        self.stop_button = ctk.CTkButton(
            self.btn_frame,
            text="⏹ Стоп",
            width=140,
            height=40,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=c["surface_2"],
            text_color=c["accent"],
            border_color=c["accent"],
            border_width=1,
            hover_color=c["surface"],
            command=self._on_stop,
        )
        self.stop_button.grid(row=0, column=4, padx=5)

        # Индикатор статуса (электрический акцент)
        self.status_label = ctk.CTkLabel(
            self.root,
            text="Готов к работе",
            font=ctk.CTkFont(size=12),
            text_color=c["electric"],
            fg_color="transparent",
        )
        self.status_label.pack(pady=(4, 10))

    def _build_context_menu(self):
        """Создаёт контекстное меню для копирования выделенного текста."""
        self.context_menu = ctk.CTkToplevel(self.root)
        self.context_menu.withdraw()
        self.context_menu.overrideredirect(True)
        self.context_menu.attributes("-topmost", True)

        self._ctx_label = ctk.CTkLabel(
            self.context_menu,
            text="📋 Копировать",
            font=ctk.CTkFont(size=12),
            fg_color=self.colors["surface_2"],
            text_color=self.colors["text"],
            corner_radius=4,
            width=120,
            height=28,
        )
        self._ctx_label.pack()
        self._ctx_label.bind("<Button-1>", lambda e: self._copy_selection())
        self._ctx_label.bind("<Enter>", lambda e: self._ctx_label.configure(fg_color=self.colors["accent"]))
        self._ctx_label.bind("<Leave>", lambda e: self._ctx_label.configure(fg_color=self.colors["surface_2"]))

        # Правая кнопка мыши на чате
        self.chat_box.bind("<Button-3>", self._show_context_menu)

    def _show_context_menu(self, event):
        try:
            selected = self.chat_box.selection_get()
        except Exception:
            selected = ""
        if not selected.strip():
            return
        # Позиция меню рядом с курсором
        try:
            x = self.root.winfo_x() + event.x_root - self.root.winfo_rootx() + self.chat_box.winfo_x()
            y = self.root.winfo_y() + event.y_root - self.root.winfo_rooty() + self.chat_box.winfo_y()
        except Exception:
            x = event.x_root
            y = event.y_root
        self.context_menu.geometry(f"+{event.x_root}+{event.y_root}")
        self.context_menu.deiconify()

    def _copy_selection(self):
        try:
            selected = self.chat_box.selection_get()
            if selected:
                self.root.clipboard_clear()
                self.root.clipboard_append(selected)
                self._set_status("Текст скопирован в буфер обмена")
        except Exception:
            pass
        self.context_menu.withdraw()

    # ------------------------------------------------------------------
    # Панель настроек (модальное окно)
    # ------------------------------------------------------------------
    def _open_settings(self):
        """Открывает модальное окно настроек."""
        win = ctk.CTkToplevel(self.root)
        win.title("Настройки Зевса")
        win.geometry("460x740")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        if ICON_PATH:
            try:
                win.iconbitmap(ICON_PATH)
            except Exception:
                pass

        c = self.colors
        win.configure(fg_color=c["bg"])

        ctk.CTkLabel(
            win, text="⚙ НАСТРОЙКИ", font=ctk.CTkFont(size=20, weight="bold"),
            text_color=c["accent"], fg_color="transparent",
        ).pack(pady=(16, 10))

        # --- Тема ---
        ctk.CTkLabel(win, text="Тема оформления:", font=ctk.CTkFont(size=13),
                     text_color=c["text"], fg_color="transparent").pack(anchor="w", padx=30, pady=(6, 0))

        theme_var = ctk.StringVar(value=self.settings.get("theme", "Dark Gold"))
        theme_frame = ctk.CTkFrame(win, fg_color="transparent")
        theme_frame.pack(padx=30, pady=4, fill="x")
        for name in config.THEMES.keys():
            rb = ctk.CTkRadioButton(
                theme_frame, text=name, variable=theme_var, value=name,
                font=ctk.CTkFont(size=13), text_color=c["text"],
                fg_color=c["accent"], border_color=c["electric"],
                command=lambda v=theme_var: self._on_theme_change(v.get()),
            )
            rb.pack(anchor="w", pady=3)

        # --- Модель Ollama ---
        ctk.CTkLabel(win, text="Модель Ollama:", font=ctk.CTkFont(size=13),
                     text_color=c["text"], fg_color="transparent").pack(anchor="w", padx=30, pady=(10, 0))
        model_entry = ctk.CTkEntry(
            win, width=380, height=36, font=ctk.CTkFont(size=13),
            fg_color=c["surface"], text_color=c["text"],
            border_color=c["electric"], border_width=1,
        )
        model_entry.insert(0, self.model_name)
        model_entry.pack(padx=30, pady=4)

        # --- Системный промпт ---
        ctk.CTkLabel(win, text="Системный промпт:", font=ctk.CTkFont(size=13),
                     text_color=c["text"], fg_color="transparent").pack(anchor="w", padx=30, pady=(10, 0))
        prompt_box = ctk.CTkTextbox(
            win, width=380, height=110, font=ctk.CTkFont(size=12),
            wrap="word", fg_color=c["surface"], text_color=c["text"],
            border_color=c["electric"], border_width=1,
        )
        prompt_box.insert("0.0", self.system_prompt)
        prompt_box.pack(padx=30, pady=4)

        # --- Голос ---
        voice_var = ctk.BooleanVar(value=self.voice_enabled)
        voice_switch = ctk.CTkSwitch(
            win, text="Озвучка ответов (голос)",
            variable=voice_var, font=ctk.CTkFont(size=13),
            text_color=c["text"], fg_color=c["accent"], progress_color=c["electric"],
        )
        voice_switch.pack(anchor="w", padx=30, pady=(10, 0))

        # --- TTS движок (Piper / pyttsx3) ---
        vs = self.settings.get("voice_settings", {})
        engine_var = ctk.StringVar(value=str(vs.get("tts_engine", "piper")).lower())

        ctk.CTkLabel(
            win, text="TTS движок (piper — нейросеть, pyttsx3 — системный):",
            font=ctk.CTkFont(size=13), text_color=c["text"], fg_color="transparent",
        ).pack(anchor="w", padx=30, pady=(8, 0))
        ctk.CTkComboBox(
            win, variable=engine_var, values=["piper", "pyttsx3"],
            width=380, height=34, font=ctk.CTkFont(size=13),
            fg_color=c["surface"], text_color=c["text"], button_color=c["electric"],
            border_color=c["electric"], border_width=1,
        ).pack(padx=30, pady=4)

        # --- Темп Piper ---
        scale_def = float(vs.get("piper_length_scale", 0.9))
        scale_var = ctk.DoubleVar(value=scale_def)
        scale_txt = ctk.CTkLabel(
            win, text=f"Темп Piper (меньше — быстрее): {scale_def:.2f}",
            font=ctk.CTkFont(size=13), text_color=c["text"], fg_color="transparent",
        )
        scale_txt.pack(anchor="w", padx=30, pady=(6, 0))
        ctk.CTkSlider(
            win, from_=0.5, to=1.5, number_of_steps=20,
            variable=scale_var, fg_color=c["surface"],
            progress_color=c["electric"], button_color=c["accent"],
            button_hover_color=c["accent_hover"],
            command=lambda v: scale_txt.configure(
                text=f"Темп Piper (меньше — быстрее): {float(v):.2f}"
            ),
        ).pack(padx=30, pady=4, fill="x")

        # --- Проверка голоса ---
        ctk.CTkButton(
            win, text="🔊 Проверить голос", width=180, height=34,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=c["electric"], text_color=c["electric_text"],
            hover_color=c["accent_hover"],
            command=lambda: self._speak(
                "Это Зевс. Проверка голоса пипер включена."
            ),
        ).pack(anchor="w", padx=30, pady=(8, 0))

        # --- Кнопки сохранения ---
        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(pady=(16, 10))

        def _save():
            self.settings["theme"] = theme_var.get()
            self.settings["model"] = model_entry.get().strip() or "llama3.1"
            self.settings["system_prompt"] = prompt_box.get("0.0", "end").strip()
            self.settings["voice_enabled"] = voice_var.get()
            # TTS движок и параметры Piper — в voice_settings (иерархический формат)
            vs = self.settings.setdefault("voice_settings", {})
            vs["tts_engine"] = engine_var.get().strip().lower()
            vs["piper_length_scale"] = round(float(scale_var.get()), 2)
            if config.save_settings(self.settings):
                self.model_name = self.settings["model"]
                self.system_prompt = self.settings["system_prompt"]
                self.voice_enabled = self.settings["voice_enabled"]
                self._refresh_colors()
                self._apply_theme()
                self._retheme_ui()
                self._set_status("Настройки сохранены")
                win.destroy()
            else:
                self._set_status("Ошибка сохранения настроек")

        ctk.CTkButton(
            btn_row, text="Сохранить", width=160, height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=c["accent"], text_color=c["accent_text"],
            hover_color=c["accent_hover"], command=_save,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_row, text="Отмена", width=160, height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=c["surface_2"], text_color=c["accent"],
            border_color=c["accent"], border_width=1,
            hover_color=c["surface"], command=win.destroy,
        ).pack(side="left", padx=8)

    def _on_theme_change(self, theme_name: str):
        """Переключение темы на лету (без перезапуска)."""
        self.settings["theme"] = theme_name
        self._refresh_colors()
        self._apply_theme()
        self._retheme_ui()

    def _retheme_ui(self):
        """Применяет новые цвета ко всем виджетам без пересоздания."""
        c = self.colors
        self.title_label.configure(text_color=c["accent"])
        self.subtitle_label.configure(text_color=c["text_dim"])
        self.chat_box.configure(fg_color=c["surface"], text_color=c["text"], border_color=c["accent"])
        self.input_entry.configure(fg_color=c["surface"], text_color=c["text"],
                                   border_color=c["electric"], placeholder_text_color=c["text_dim"])
        self.send_button.configure(fg_color=c["accent"], text_color=c["accent_text"], hover_color=c["accent_hover"])
        self.listen_button.configure(fg_color=c["electric"], text_color=c["electric_text"], hover_color=c["accent_hover"])
        self.new_chat_button.configure(fg_color=c["surface_2"], text_color=c["accent"],
                                       border_color=c["accent"], hover_color=c["surface"])
        self.settings_button.configure(fg_color=c["surface_2"], text_color=c["accent"],
                                       border_color=c["accent"], hover_color=c["surface"])
        self.stop_button.configure(fg_color=c["surface_2"], text_color=c["accent"],
                                   border_color=c["accent"], hover_color=c["surface"])
        self.status_label.configure(text_color=c["electric"])
        # Обновляем контекстное меню
        try:
            self._ctx_label.configure(fg_color=c["surface_2"], text_color=c["text"])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Чат: логика
    # ------------------------------------------------------------------
    def _append_chat(self, who: str, text: str):
        self.chat_box.configure(state="normal")
        if who == "Вы":
            prefix = f"[{self.colors['electric']}] Вы: {text}\n\n"
        else:
            prefix = f"[{self.colors['accent']}] Зевс: {text}\n\n"
        self.chat_box.insert("end", prefix)
        self.chat_box.configure(state="disabled")
        self.chat_box.see("end")

    def _set_status(self, text: str):
        self.root.after(0, self.status_label.configure, {"text": text})

    def _set_buttons(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.root.after(0, self.send_button.configure, {"state": state})
        self.root.after(0, self.listen_button.configure, {"state": state})

    def _start_thinking_animation(self):
        """Запускает анимацию индикатора 'Зевс думает...'."""
        self._thinking = True
        self._anim_index = 0
        self._animate()

    def _animate(self):
        if not self._thinking:
            return
        dots = "." * (self._anim_index % 4)
        self._set_status(f"Зевс думает{dots}")
        self._anim_index += 1
        self.root.after(300, self._animate)

    def _stop_thinking_animation(self):
        self._thinking = False

    def _on_send(self):
        text = self.input_entry.get().strip()
        if not text:
            return

        self._append_chat("Вы", text)
        self.input_entry.delete(0, "end")
        
        # === ЖЕСТКИЙ ФИЛЬТР: проверяем системные команды ПЕРЕД отправкой в Ollama ===
        command_result = actions.execute_command(text)
        if command_result is not None:
            # Это системная команда - выполняем сразу, не трогаем ИИ
            self._handle_command_result(command_result)
            return
        
        # Не системная команда - отправляем в LLM
        self.conversation.append({"role": "user", "content": text})
        self._set_buttons(False)
        self._start_thinking_animation()
        asyncio.run_coroutine_threadsafe(self._ask_zeus(text), self.loop)

    def _on_new_chat(self):
        """Очищает историю контекста текущего диалога."""
        self.conversation.clear()
        self.chat_box.configure(state="normal")
        self.chat_box.delete("0.0", "end")
        self.chat_box.configure(state="disabled")
        self._set_status("Новый чат начат")

    def _on_listen(self):
        if not self._mic_available:
            self._set_status("Микрофон недоступен")
            return
        self._set_status("Зевс слушает...")
        self._set_buttons(False)
        threading.Thread(target=self._listen_thread, daemon=True).start()

    def _listen_thread(self):
        try:
            import sounddevice as sd
            import numpy as np
            from scipy.io.wavfile import write
            import io
            import wave
            
            # Параметры записи
            sample_rate = 16000  # Гц
            channels = 1
            dtype = 'int16'
            duration = 5  # максимальная длительность записи
            
            self._set_status("Записываю речь...")
            
            # Запись аудио
            audio_data = sd.rec(
                int(sample_rate * duration),
                samplerate=sample_rate,
                channels=channels,
                dtype=dtype,
                device=self._mic_device_index
            )
            sd.wait()  # ждём завершения записи
            
            # Конвертация в AudioData для speech_recognition
            # Создаём WAV в памяти
            byte_io = io.BytesIO()
            with wave.open(byte_io, 'wb') as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)  # 16 бит = 2 байта
                wf.setframerate(sample_rate)
                wf.writeframes(audio_data.tobytes())
            
            byte_io.seek(0)
            audio = sr.AudioData(byte_io.read(), sample_rate, 2)
            
            self._set_status("Распознаю речь...")
            text = self.recognizer.recognize_google(audio, language="ru-RU")
            self.root.after(0, self.input_entry.insert, "end", text)
            self._set_status("Готов к работе")
            self._set_buttons(True)
            self.root.after(0, self._on_send)
        except sr.WaitTimeoutError:
            self._set_status("Не услышал речи. Попробуйте ещё раз.")
            self._set_buttons(True)
        except sr.UnknownValueError:
            self._set_status("Не удалось распознать. Говорите чётче.")
            self._set_buttons(True)
        except sr.RequestError as exc:
            self._set_status(f"Ошибка сервиса распознавания: {exc}")
            self._set_buttons(True)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Ошибка микрофона: {exc}")
            self._set_buttons(True)

    def _on_stop(self):
        self._stop_speaking = True
        try:
            from modules.tts_piper import set_stop
            set_stop()
        except Exception:
            pass
        self._set_status("Озвучка прервана")

    async def _ask_zeus(self, prompt: str):
        full_text = ""
        try:
            messages = []
            if self.system_prompt:
                messages.append({"role": "system", "content": self.system_prompt})
            # Добавляем историю сессии для контекста
            messages.extend(self.conversation)

            stream = await self.ollama.chat(
                model=self.model_name,
                messages=messages,
                stream=True,
            )
            self.root.after(0, self._append_chat, "Зевс", "")
            async for chunk in stream:
                piece = chunk["message"]["content"]
                full_text += piece
                self.root.after(0, self._append_streamed, piece)
            self.root.after(0, self._finish_response)
            # Сохраняем ответ в историю
            self.conversation.append({"role": "assistant", "content": full_text})
            # Озвучка ответа Зевса
            if self.voice_enabled:
                self._speak(full_text)
            else:
                self._set_status("Готов к работе")
                self._set_buttons(True)
        except Exception as exc:  # noqa: BLE001
            self._stop_thinking_animation()
            self.root.after(
                0,
                self._append_chat,
                "Зевс",
                f"Ошибка подключения к Ollama: {exc}\n"
                "Убедитесь, что Ollama запущена (команда 'ollama serve') "
                "и модель загружена (команда 'ollama pull llama3.1').",
            )
            self.root.after(0, self._finish_response)

    def _speak(self, text: str):
        """Синтез речи в отдельном потоке (Piper или pyttsx3-фолбэк).

        Выбирает движок по voice_settings.tts_engine: "piper" — нейросеть
        Piper (локальная модель models/tts/), иначе / при недоступности —
        системный pyttsx3 (SAPI5).
        """
        self._stop_speaking = False
        self._set_status("Зевс говорит...")

        def _speak_thread():
            # 1) Определяем движок из настроек
            engine = "piper"
            try:
                from core.config import load_settings
                engine = str(
                    load_settings().get("voice_settings", {}).get("tts_engine", "piper")
                ).lower()
            except Exception:  # noqa: BLE001
                engine = "piper"

            # 2) Piper (потоково, без temp-файлов)
            spoken = False
            if engine == "piper":
                try:
                    from modules.tts_piper import speak_piper
                    spoken = speak_piper(text)
                except Exception:  # noqa: BLE001
                    spoken = False

            # 3) pyttsx3 — фолбэк или движок по умолчанию
            if not spoken:
                try:
                    engine_fb = pyttsx3.init()
                    voices = engine_fb.getProperty("voices")
                    for v in voices:
                        if "russian" in v.name.lower() or "ru" in v.id.lower():
                            engine_fb.setProperty("voice", v.id)
                            break
                    engine_fb.setProperty("rate", 170)
                    engine_fb.setProperty("volume", 1.0)

                    sentences = re.split(r"(?<=[.!?])\s+", text)
                    for sent in sentences:
                        if self._stop_speaking:
                            engine_fb.stop()
                            break
                        engine_fb.say(sent)
                    engine_fb.runAndWait()
                except Exception as exc:  # noqa: BLE001
                    self._set_status(f"Ошибка синтеза речи: {exc}")

            # Итоговый статус
            if not self._stop_speaking:
                self._set_status("Готов к работе")
            self._set_buttons(True)

        threading.Thread(target=_speak_thread, daemon=True).start()

    def _append_streamed(self, piece: str):
        self.chat_box.configure(state="normal")
        self.chat_box.insert("end", piece)
        self.chat_box.configure(state="disabled")
        self.chat_box.see("end")

    def _handle_command_result(self, result: dict[str, Any]):
        """Обрабатывает результат выполнения системной команды."""
        success = result.get("success", False)
        message = result.get("message", "Неизвестная команда, сэр.")
        action = result.get("action", "unknown")
        
        # Останавливаем анимацию если она была
        self._stop_thinking_animation()
        
        # Выводим ответ
        self._append_chat("Зевс", message)
        
        # Озвучка если включена
        if self.voice_enabled:
            self._speak(message)
        else:
            self._set_status("Готов к работе")
            self._set_buttons(True)
        
        # Специальная обработка для некоторых действий
        if action == "file_search_confirm" and success:
            # Сохраняем в историю для контекста
            self.conversation.append({"role": "assistant", "content": message})
        elif action == "delete_command_confirm":
            # Здесь можно добавить логику подтверждения удаления
            # Пока что просто выводим сообщение
            pass
        elif action == "drive_scan_started":
            # Запускаем мониторинг завершения локального сканирования флешки
            self._start_drive_scan_monitor(result.get("drive", ""))
    
    def _finish_response(self):
        self._stop_thinking_animation()
        self.chat_box.configure(state="normal")
        self.chat_box.insert("end", "\n\n")
        self.chat_box.configure(state="disabled")
        self.chat_box.see("end")
        if not self.voice_enabled and not self._stop_speaking:
            self._set_status("Готов к работе")
        self._set_buttons(True)

    def run(self):
        self.root.mainloop()


def _bootstrap():
    try:
        app = ZeusApp()
        app.run()
    except Exception as exc:  # noqa: BLE001
        import traceback
        log_path = os.path.join(
            os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)),
            "zeus_error.log",
        )
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("Zeus startup error:\n")
            f.write(traceback.format_exc())
        raise


if __name__ == "__main__":
    _bootstrap()
