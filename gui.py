from __future__ import annotations

import os
import re
import shutil
import threading
import webbrowser
from tkinter import filedialog, messagebox

import customtkinter as ctk
import requests

import sync_music as core

APP_VERSION = "1.0.2"
GITHUB_REPO = "raku6ka/YTMusicSync"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class YTMusicSyncApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"YTMusicSync v{APP_VERSION}")
        self.geometry("760x660")
        self.minsize(680, 580)

        self.cfg = core.load_config(create_if_missing=True)
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.latest_release_url: str | None = None

        self._build_ui()
        core.set_gui_logger(self._on_log_message)

       
        self.after(2000, self._start_background_checks)


    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        top_frame = ctk.CTkFrame(self, fg_color="transparent")
        top_frame.grid(row=0, column=0, padx=16, pady=(12, 4), sticky="ew")
        top_frame.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            top_frame, text="Готов к работе", font=ctk.CTkFont(size=13, weight="bold"), anchor="w"
        )
        self.status_label.grid(row=0, column=0, sticky="w")

        # кнопка будет обновления скрыта, пока GitHub не вернёт более свежий релиз
        self.update_btn = ctk.CTkButton(
            top_frame, text="Доступна новая версия! Скачать",
            fg_color="#2e7d32", hover_color="#1b5e20", height=28,
            command=self._open_release_page,
        )

        main_card = ctk.CTkFrame(self)
        main_card.grid(row=1, column=0, padx=16, pady=6, sticky="ew")
        main_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(main_card, text="Плейлист YouTube Music:").grid(
            row=0, column=0, padx=12, pady=(12, 6), sticky="w")
        self.url_entry = ctk.CTkEntry(main_card, placeholder_text="https://music.youtube.com/playlist?list=...")
        self.url_entry.grid(row=0, column=1, padx=6, pady=(12, 6), sticky="ew")
        self.url_entry.insert(0, self.cfg.get("YOUTUBE_PLAYLIST_URL", ""))
        ctk.CTkButton(main_card, text="Вставить", width=95, command=self._paste_url).grid(
            row=0, column=2, padx=(4, 12), pady=(12, 6))

        ctk.CTkLabel(main_card, text="Папка для музыки:").grid(row=1, column=0, padx=12, pady=6, sticky="w")
        self.dir_entry = ctk.CTkEntry(main_card, placeholder_text="Выберите папку, где хранится музыка...")
        self.dir_entry.grid(row=1, column=1, padx=6, pady=6, sticky="ew")
        self.dir_entry.insert(0, self.cfg.get("MUSIC_DIR", ""))
        ctk.CTkButton(main_card, text="Обзор...", width=95, command=self._browse_dir).grid(
            row=1, column=2, padx=(4, 12), pady=6)

        opts_row = ctk.CTkFrame(main_card, fg_color="transparent")
        opts_row.grid(row=2, column=0, columnspan=3, padx=12, pady=(4, 12), sticky="ew")

        ctk.CTkLabel(opts_row, text="Формат:").pack(side="left", padx=(0, 8))
        self.format_var = ctk.StringVar(value=str(self.cfg.get("OUTPUT_FORMAT", "mp3")).upper())
        self.format_seg = ctk.CTkSegmentedButton(opts_row, values=["MP3", "FLAC"],
                                                 variable=self.format_var, width=140)
        self.format_seg.pack(side="left", padx=(0, 16))

        ctk.CTkButton(
            opts_row, text="Загрузить cookies.txt", width=155,
            fg_color="#37474f", hover_color="#263238", command=self._import_cookies,
        ).pack(side="left", padx=4)

        self.cookies_status = ctk.CTkLabel(opts_row, text=self._get_cookies_text(), text_color="gray")
        self.cookies_status.pack(side="left", padx=8)

        adv_card = ctk.CTkFrame(self)
        adv_card.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        adv_card.grid_columnconfigure(1, weight=1)
        adv_card.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(adv_card, text="AcoustID API Key:").grid(
            row=0, column=0, padx=(12, 6), pady=(10, 6), sticky="w")
        self.acoustid_entry = ctk.CTkEntry(adv_card, placeholder_text="Опционально (теги по звуку)")
        self.acoustid_entry.grid(row=0, column=1, padx=6, pady=(10, 6), sticky="ew")
        self.acoustid_entry.insert(0, self.cfg.get("ACOUSTID_API_KEY", ""))

        ctk.CTkLabel(adv_card, text="Proxy URL:").grid(
            row=0, column=2, padx=(12, 6), pady=(10, 6), sticky="w")
        self.proxy_entry = ctk.CTkEntry(adv_card, placeholder_text="Опционально, если нужен прокси")
        self.proxy_entry.grid(row=0, column=3, padx=(6, 12), pady=(10, 6), sticky="ew")
        self.proxy_entry.insert(0, self.cfg.get("PROXY_URL", ""))

        checks_frame = ctk.CTkFrame(adv_card, fg_color="transparent")
        checks_frame.grid(row=1, column=0, columnspan=4, padx=12, pady=(2, 10), sticky="ew")

        self.auto_check_var = ctk.BooleanVar(value=bool(self.cfg.get("AUTO_CHECK_ON_START", True)))
        ctk.CTkCheckBox(checks_frame, text="Проверять новинки при запуске",
                        variable=self.auto_check_var).pack(side="left", padx=(0, 16))

        self.auto_dl_var = ctk.BooleanVar(value=bool(self.cfg.get("AUTO_DOWNLOAD_ON_START", False)))
        ctk.CTkCheckBox(checks_frame, text="Автоматически докачивать новинки",
                        variable=self.auto_dl_var).pack(side="left", padx=(0, 16))

        self.dry_run_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(checks_frame, text="Пробный режим (Dry Run)",
                        variable=self.dry_run_var).pack(side="left")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=3, column=0, padx=16, pady=6, sticky="ew")
        btn_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            btn_frame, text="Сохранить настройки", width=160,
            fg_color="#455a64", hover_color="#37474f", command=self._save_settings,
        ).grid(row=0, column=0, padx=(0, 8))

        self.start_btn = ctk.CTkButton(
            btn_frame, text="Синхронизировать сейчас",
            font=ctk.CTkFont(size=14, weight="bold"), height=38, command=self._start_sync,
        )
        self.start_btn.grid(row=0, column=1, padx=4, sticky="ew")

        self.stop_btn = ctk.CTkButton(
            btn_frame, text="Остановить", width=120, height=38,
            fg_color="#c62828", hover_color="#8e0000", state="disabled", command=self._stop_sync,
        )
        self.stop_btn.grid(row=0, column=2, padx=(8, 0))

        tools_row = ctk.CTkFrame(btn_frame, fg_color="transparent")
        tools_row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.cover_btn = ctk.CTkButton(
            tools_row, text="Проверить обложки", width=170,
            fg_color="#6a4c93", hover_color="#4a3468", command=self._start_cover_check,
        )
        self.cover_btn.pack(side="left", padx=(0, 8))

        self.meta_btn = ctk.CTkButton(
            tools_row, text="Дополнить теги библиотеки", width=210,
            fg_color="#8d5a1f", hover_color="#6b4416", command=self._start_meta_enrich,
        )
        self.meta_btn.pack(side="left")

        self.progress = ctk.CTkProgressBar(self)
        self.progress.grid(row=4, column=0, padx=16, pady=(4, 6), sticky="ew")
        self.progress.set(0)

        self.log_box = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=12), state="disabled")
        self.log_box.grid(row=5, column=0, padx=16, pady=(0, 14), sticky="nsew")

    
    def _get_cookies_text(self) -> str:
        cfile = core.get_app_data_dir() / core.COOKIES_FILE
        return "cookies.txt активен" if cfile.exists() else "cookies.txt не задан"

    def _paste_url(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except Exception:
            return
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, text)

    def _browse_dir(self) -> None:
        chosen = filedialog.askdirectory(title="Выберите папку для музыки")
        if chosen:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, os.path.normpath(chosen))

    def _import_cookies(self) -> None:
        src = filedialog.askopenfilename(
            title="Выберите экспортированный файл cookies.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not src:
            return
        try:
            dst = core.get_app_data_dir() / core.COOKIES_FILE
            shutil.copy2(src, dst)
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось скопировать cookies.txt:\n{exc}")
            return
        self.cookies_status.configure(text="cookies.txt загружен", text_color="#66bb6a")
        self._append_log("[INFO] Файл cookies.txt сохранён в настройки приложения.")

    def _collect_cfg(self) -> dict:
        self.cfg["YOUTUBE_PLAYLIST_URL"] = self.url_entry.get().strip()
        self.cfg["MUSIC_DIR"] = self.dir_entry.get().strip()
        self.cfg["OUTPUT_FORMAT"] = self.format_var.get().lower()
        self.cfg["ACOUSTID_API_KEY"] = self.acoustid_entry.get().strip()
        self.cfg["PROXY_URL"] = self.proxy_entry.get().strip()
        self.cfg["AUTO_CHECK_ON_START"] = bool(self.auto_check_var.get())
        self.cfg["AUTO_DOWNLOAD_ON_START"] = bool(self.auto_dl_var.get())
        return self.cfg

    def _save_settings(self, silent: bool = False) -> None:
        core.save_config(self._collect_cfg())
        if not silent:
            self.status_label.configure(text="Настройки сохранены")
            self._append_log("[OK] Настройки сохранены в %APPDATA%\\YTMusicSync\\config.json")

    
    def _on_log_message(self, line: str, level: str) -> None:
        self.after(0, self._append_log, line)

    def _append_log(self, line: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _on_progress(self, current: int, total: int) -> None:
        val = (current / total) if total > 0 else 0.0
        self.after(0, self.progress.set, val)
        self.after(0, lambda: self.status_label.configure(text=f"Обработка треков: {current} из {total}"))

    
    def _launch(self, job, status_text: str) -> None:
        """Запускает задачу в отдельном потоке, интерфейс не зависает."""
        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.cover_btn.configure(state="disabled")
        self.meta_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.set(0)
        self.status_label.configure(text=status_text)

        def _worker() -> None:
            try:
                job()
            except Exception as exc:
                core.log_err(f"Критическая ошибка: {exc}")
            finally:
                self.after(0, self._on_sync_finished)

        self.worker_thread = threading.Thread(target=_worker, daemon=True)
        self.worker_thread.start()

    def _start_sync(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        cfg = self._collect_cfg()
        if not cfg["YOUTUBE_PLAYLIST_URL"] or not cfg["MUSIC_DIR"]:
            messagebox.showwarning("Не заполнены поля",
                                   "Укажите ссылку на плейлист YouTube Music и папку для музыки.")
            return
        core.save_config(cfg)
        dry_run = bool(self.dry_run_var.get())
        job_cfg = dict(cfg)
        self._launch(
            lambda: core.run_sync(job_cfg, dry_run=dry_run, stop_event=self.stop_event,
                                  progress_cb=self._on_progress),
            "Идёт синхронизация...",
        )

    def _start_cover_check(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        cfg = self._collect_cfg()
        if not cfg["MUSIC_DIR"]:
            messagebox.showwarning("Не заполнено поле", "Сначала укажите папку для музыки.")
            return
        core.save_config(cfg)
        dry_run = bool(self.dry_run_var.get())
        job_cfg = dict(cfg)
        self._launch(
            lambda: core.run_cover_check(job_cfg, stop_event=self.stop_event,
                                         progress_cb=self._on_progress, force=True, dry_run=dry_run),
            "Проверка обложек...",
        )

    def _start_meta_enrich(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        cfg = self._collect_cfg()
        if not cfg["MUSIC_DIR"]:
            messagebox.showwarning("Не заполнено поле", "Сначала укажите папку для музыки.")
            return
        dry_run = bool(self.dry_run_var.get())
        if not dry_run:
            ok = messagebox.askyesno(
                "Дополнить теги библиотеки",
                "Программа найдёт в интернете данные о каждом треке (альбом, исполнитель альбома, "
                "номер трека и диска, жанр, год, издатель, авторские права, ISRC, BPM, обложку) "
                "и ДОПИШЕТ их в теги ваших MP3/FLAC файлов в папке:\n\n"
                f"{cfg['MUSIC_DIR']}\n\n"
                "• Заполняются только пустые поля, существующие данные не перезаписываются.\n"
                "• Файлы будут изменены. Рекомендуется сделать резервную копию.\n"
                "• Для большой библиотеки это может занять долго. Процесс можно остановить "
                "и продолжить позже: обработанные файлы пропускаются.\n\n"
                "Продолжить?",
                icon="warning",
            )
            if not ok:
                return
        core.save_config(cfg)
        job_cfg = dict(cfg)
        self._launch(
            lambda: core.run_metadata_enrich(job_cfg, stop_event=self.stop_event,
                                             progress_cb=self._on_progress, dry_run=dry_run),
            "Дополнение тегов...",
        )

    def _stop_sync(self) -> None:
        self.stop_event.set()
        self.status_label.configure(text="Остановка после текущего шага...")
        self.stop_btn.configure(state="disabled")

    def _on_sync_finished(self) -> None:
        self.start_btn.configure(state="normal")
        self.cover_btn.configure(state="normal")
        self.meta_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.progress.set(1.0)
        self.status_label.configure(text="Готово")

    
    def _start_background_checks(self) -> None:
        threading.Thread(target=self._check_github_release, daemon=True).start()
        cfg = self.cfg
        if cfg.get("AUTO_CHECK_ON_START") and cfg.get("YOUTUBE_PLAYLIST_URL") and cfg.get("MUSIC_DIR"):
            threading.Thread(target=self._soft_check_playlist, daemon=True).start()

    def _check_github_release(self) -> None:
        if "USERNAME" in GITHUB_REPO:
            return
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                timeout=3,
                headers={"Accept": "application/vnd.github+json"},
            )
            if resp.status_code != 200:
                return
            data = resp.json()
            if data.get("draft") or data.get("prerelease"):
                return
            tag = (data.get("tag_name") or "").lstrip("vV").strip()
            html_url = data.get("html_url")
            core.log(f"Проверка обновлений: на GitHub {tag or '?'}, установлена {APP_VERSION}")
            if tag and html_url and self._is_newer_version(tag, APP_VERSION):
                self.latest_release_url = html_url
                self.after(0, lambda: self._show_update_button(tag))
        except Exception:
            pass

    def _show_update_button(self, tag: str) -> None:
        self.update_btn.configure(text=f"Доступна новая версия v{tag}! Скачать")
        self.update_btn.grid(row=0, column=1, sticky="e")

    @staticmethod
    def _parse_version(text: str) -> tuple[int, ...] | None:
        m = re.match(r"^\s*[vV]?(\d+(?:\.\d+){0,3})", text or "")
        if not m:
            return None
        return tuple(int(x) for x in m.group(1).split("."))

    @staticmethod
    def _is_newer_version(remote: str, local: str) -> bool:
        r = YTMusicSyncApp._parse_version(remote)
        l = YTMusicSyncApp._parse_version(local)
        if r is None or l is None:
            return False  # непонятный тег, не показываем кнопку
        n = max(len(r), len(l))
        r += (0,) * (n - len(r))
        l += (0,) * (n - len(l))
        return r > l

    def _open_release_page(self) -> None:
        if self.latest_release_url:
            webbrowser.open(self.latest_release_url)

    def _soft_check_playlist(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        self.after(0, lambda: self.status_label.configure(text="Проверка обновлений плейлиста..."))
        try:
            total, new_count = core.check_playlist_updates(dict(self.cfg))
        except Exception as exc:
            core.log_warn(f"Не удалось проверить плейлист: {exc}")
            self.after(0, lambda: self.status_label.configure(text="Готов к работе"))
            return
        if new_count > 0:
            self.after(0, lambda: self.status_label.configure(
                text=f"Найдено новых треков: {new_count}"))
            if self.cfg.get("AUTO_DOWNLOAD_ON_START"):
                self.after(500, self._start_sync)
        else:
            self.after(0, lambda: self.status_label.configure(
                text=f"Плейлист актуален ({total} треков)"))


if __name__ == "__main__":
    app = YTMusicSyncApp()
    app.mainloop()
