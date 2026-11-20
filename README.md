# YTMusicSync

🇬🇧 [English](#english) · 🇷🇺 [Русский](#русский)

---

<a id="english"></a>
## 🇬🇧 English

This repository contains a desktop application for downloading music directly from playlists. It is designed for users with large playlists (100+ tracks) who want to easily transfer their music library from a streaming service to a local drive.

Paste a YouTube Music playlist link, pick a folder, press one button – the app downloads everything that is missing, converts it to MP3 or FLAC, tags it and adds cover art. Run it again later and it fetches only the new tracks.

### Features

- **Simple GUI** (dark theme, built with customtkinter): playlist link, music folder, format switch, live log and progress bar.
- **MP3 (320 kbps) or FLAC** output via ffmpeg.
- **Incremental sync** – only tracks that are not in your library yet are downloaded.
- **Smart duplicate protection**
  - compares "Artist + Title" and, separately, the cleaned track title together with the duration (±3 s) – catches re-uploads on fan channels;
  - computes an audio fingerprint (`fpcalc` / Chromaprint) right after download; if the same sound is already in your library, the new file is moved to `_duplicates`.
- **Fallback search** – if a video is unavailable, blocked in your country or has no suitable format, the app retries with a `ytsearch1:Artist - Title` search.
- **Tags and covers** – iTunes Search (with a second title-only query matched by duration ±5 s) and MusicBrainz / Cover Art Archive as a backup.
- **Optional AcoustID** key to recognise untagged local files by sound.
- **Dry Run mode** – shows what would happen without touching any files.
- **Auto-check on start** – a soft background check of the playlist 2 seconds after launch, with optional automatic download of new tracks.
- **Update notification** – checks GitHub Releases and shows a button when a newer version is available.
- **Privacy** – all your data lives only in `%APPDATA%\YTMusicSync`, nothing personal is stored in the program folder.

### Quick start (ready-made build)

1. Download the latest `YTMusicSync-vX.Y.Z.zip` from the **Releases** page and unpack it.
2. Make sure the `bin` folder next to `YTMusicSync.exe` contains `ffmpeg.exe`, `ffprobe.exe` and `fpcalc.exe` (see [Dependencies](#dependencies)).
3. Run `YTMusicSync.exe`.
4. Paste the playlist link (**Paste** button), choose the music folder (**Browse...**), pick **MP3** or **FLAC**.
5. Press **Save settings**, then **Sync now**.

> Tip: tick **Dry Run** on the first launch to see what will be downloaded.

### Settings

| Option | Description |
|---|---|
| Playlist | Link to a YouTube Music / YouTube playlist (must be public or accessible with cookies). |
| Music folder | Where your library lives and where new tracks are saved. |
| Format | MP3 (320k) or FLAC. |
| Load cookies.txt | Copies your exported `cookies.txt` to `%APPDATA%\YTMusicSync`. Needed for private playlists or age-restricted tracks. |
| AcoustID API Key | Optional. Lets the app identify untagged local files by sound. |
| Proxy URL | Optional proxy for all requests. |
| Check for new tracks on start | Background check of the playlist after launch. |
| Auto-download new tracks | Starts the download automatically if new tracks are found. |
| Dry Run | Simulation, no files are changed. |

### Where data is stored

Everything is kept in `%APPDATA%\YTMusicSync`:

```
config.json          settings
library_index.json   index of your local library (tags, durations, fingerprints)
downloaded_ids.txt   IDs of already processed videos
sync.log             log file
cookies.txt          optional cookies
```

### <a id="dependencies"></a>Dependencies

The app needs three external tools placed in a `bin` folder next to the executable (or available in `PATH`):

- [ffmpeg and ffprobe](https://ffmpeg.org/download.html)
- `fpcalc` from [Chromaprint](https://acoustid.org/chromaprint)

### Run from source

```powershell
git clone https://github.com/raku6ka/Ymusic-playlists-downloader.git
cd Ymusic-playlists-downloader
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python gui.py
```

A command-line mode is also available: `python sync_music.py [--dry-run] [--limit N]` (settings are read from `%APPDATA%\YTMusicSync\config.json`).

### Build an .exe

Put `ffmpeg.exe`, `ffprobe.exe`, `fpcalc.exe` into `bin\` and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_release.ps1
```

The script creates the helper files, builds the app with PyInstaller, copies `bin` next to the executable and checks the folder for personal data. The result is in `release\YTMusicSync\`.

### Disclaimer

This tool is intended for personal use, for example to back up music you have the right to keep. You are responsible for complying with copyright law and the terms of service of the platforms you use.

---

<a id="русский"></a>
## 🇷🇺 Русский

Этот репозиторий содержит десктопное приложение для скачивания музыки напрямую из плейлистов. Оно рассчитано на пользователей с большими плейлистами (100+ треков), которым нужно легко перенести свою музыкальную библиотеку из стримингового сервиса на локальный диск.

Вставьте ссылку на плейлист YouTube Music, выберите папку и нажмите одну кнопку – программа скачает всё, чего у вас ещё нет, конвертирует в MP3 или FLAC, проставит теги и добавит обложки. При следующем запуске будут скачаны только новые треки.

### Возможности

- **Простой интерфейс** (тёмная тема, customtkinter): ссылка на плейлист, папка с музыкой, выбор формата, живой лог и прогресс-бар.
- **MP3 (320 кбит/с) или FLAC** через ffmpeg.
- **Инкрементальная синхронизация** – скачиваются только треки, которых ещё нет в библиотеке.
- **Умная защита от дублей**
  - сравнение «Исполнитель + Название» и, отдельно, очищенного названия вместе с длительностью (±3 сек) – ловит перезаливы на фанатских каналах;
  - сразу после скачивания вычисляется звуковой отпечаток (`fpcalc` / Chromaprint); если такой звук уже есть в библиотеке, новый файл перемещается в `_duplicates`.
- **Резервный поиск** – если видео недоступно, заблокировано в вашей стране или нет подходящего формата, программа повторяет попытку через поиск `ytsearch1:Исполнитель - Название`.
- **Теги и обложки** – iTunes Search (со вторым запросом только по названию и отбором по длительности ±5 сек) и MusicBrainz / Cover Art Archive как запасной вариант.
- **AcoustID (по желанию)** – распознавание локальных файлов без тегов по звуку.
- **Пробный режим (Dry Run)** – показывает, что будет сделано, не трогая файлы.
- **Проверка при запуске** – мягкая фоновая проверка плейлиста через 2 секунды после старта и, по желанию, автоматическая докачка новинок.
- **Уведомление об обновлениях** – проверка GitHub Releases и кнопка, если вышла новая версия.
- **Приватность** – все ваши данные хранятся только в `%APPDATA%\YTMusicSync`, в папке программы ничего личного нет.

### Быстрый старт (готовая сборка)

1. Скачайте свежий `YTMusicSync-vX.Y.Z.zip` на странице **Releases** и распакуйте.
2. Убедитесь, что в папке `bin` рядом с `YTMusicSync.exe` лежат `ffmpeg.exe`, `ffprobe.exe` и `fpcalc.exe` (см. [Зависимости](#зависимости)).
3. Запустите `YTMusicSync.exe`.
4. Вставьте ссылку на плейлист (кнопка **Вставить**), выберите папку (**Обзор...**), укажите **MP3** или **FLAC**.
5. Нажмите **Сохранить настройки**, затем **Синхронизировать сейчас**.

> Совет: при первом запуске включите **Пробный режим**, чтобы увидеть, что будет скачано.

### Настройки

| Параметр | Описание |
|---|---|
| Плейлист | Ссылка на плейлист YouTube Music / YouTube (публичный или доступный через cookies). |
| Папка для музыки | Где лежит ваша библиотека и куда сохраняются новые треки. |
| Формат | MP3 (320k) или FLAC. |
| Загрузить cookies.txt | Копирует выбранный `cookies.txt` в `%APPDATA%\YTMusicSync`. Нужен для приватных плейлистов и треков с возрастным ограничением. |
| AcoustID API Key | Необязательно. Позволяет распознавать файлы без тегов по звуку. |
| Proxy URL | Необязательно. Прокси для всех запросов. |
| Проверять новинки при запуске | Фоновая проверка плейлиста после старта. |
| Автоматически докачивать новинки | Запускает загрузку сама, если найдены новые треки. |
| Пробный режим (Dry Run) | Имитация, файлы не изменяются. |

### Где хранятся данные

Всё лежит в `%APPDATA%\YTMusicSync`:

```
config.json          настройки
library_index.json   индекс локальной библиотеки (теги, длительности, отпечатки)
downloaded_ids.txt   ID уже обработанных видео
sync.log             лог
cookies.txt          cookies (по желанию)
```

### Зависимости

Программе нужны три внешние утилиты в папке `bin` рядом с исполняемым файлом (или в `PATH`):

- [ffmpeg и ffprobe](https://ffmpeg.org/download.html)
- `fpcalc` из [Chromaprint](https://acoustid.org/chromaprint)

### Запуск из исходников

```powershell
git clone https://github.com/raku6ka/Ymusic-playlists-downloader.git
cd Ymusic-playlists-downloader
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python gui.py
```

Есть и консольный режим: `python sync_music.py [--dry-run] [--limit N]` (настройки читаются из `%APPDATA%\YTMusicSync\config.json`).

### Сборка .exe

Положите `ffmpeg.exe`, `ffprobe.exe`, `fpcalc.exe` в `bin\` и выполните:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_release.ps1
```

Скрипт создаст вспомогательные файлы, соберёт программу через PyInstaller, скопирует `bin` рядом с exe и проверит папку на личные данные. Результат окажется в `release\YTMusicSync\`.

### Отказ от ответственности

Программа предназначена для личного использования, например для резервной копии музыки, которую вы вправе хранить. Вы сами отвечаете за соблюдение законов об авторском праве и правил сервисов, которыми пользуетесь.

