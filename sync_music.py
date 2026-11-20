#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_music.py - движок синхронизации плейлиста YouTube Music с локальной папкой.
Все личные настройки, индекс и логи хранятся исключительно в %APPDATA%\\YTMusicSync.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import requests
from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TDRC, TIT2, TPE1
from mutagen.mp3 import MP3
from rapidfuzz import fuzz

try:
    from transliterate import translit
except Exception:  # библиотека необязательна, есть запасная таблица
    translit = None

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav", ".aac"}
CONFIG_FILE = "config.json"
COOKIES_FILE = "cookies.txt"
REQUEST_TIMEOUT = 15
API_PAUSE_SEC = 0.8

DEFAULTS: dict[str, Any] = {
    "YOUTUBE_PLAYLIST_URL": "",
    "MUSIC_DIR": "",
    "ACOUSTID_API_KEY": "",
    "PROXY_URL": "",
    "BROWSER_FOR_COOKIES": "",
    "OUTPUT_FORMAT": "mp3",
    "MP3_BITRATE": "320k",
    "DURATION_TOLERANCE_SEC": 3,
    "FUZZY_SIMILARITY_THRESHOLD": 92,
    "DUPLICATES_SUBDIR": "_duplicates",
    "LIBRARY_INDEX_FILE": "library_index.json",
    "DOWNLOADED_IDS_FILE": "downloaded_ids.txt",
    "AUTO_CHECK_ON_START": True,
    "AUTO_DOWNLOAD_ON_START": False,
}


def get_app_data_dir() -> Path:
    """Возвращает %APPDATA%\\YTMusicSync (создаёт при отсутствии)."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    path = Path(base) / "YTMusicSync"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_bundle_dir() -> Path:
    """Папка рядом с .exe (в сборке) или папка проекта."""
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(sys.executable)).resolve()
    return Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

LOG_FILE_HANDLE: Any = None
GUI_LOG_CALLBACK: Optional[Callable[[str, str], None]] = None


def set_gui_logger(callback: Optional[Callable[[str, str], None]]) -> None:
    global GUI_LOG_CALLBACK
    GUI_LOG_CALLBACK = callback


def log(msg: str, level: str = "INFO") -> None:
    global LOG_FILE_HANDLE
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [{level}] {msg}"
    if GUI_LOG_CALLBACK:
        try:
            GUI_LOG_CALLBACK(line, level)
        except Exception:
            pass
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except Exception:
        pass
    if LOG_FILE_HANDLE is None:
        try:
            LOG_FILE_HANDLE = open(get_app_data_dir() / "sync.log", "a", encoding="utf-8")
        except OSError:
            LOG_FILE_HANDLE = False
    if LOG_FILE_HANDLE:
        try:
            LOG_FILE_HANDLE.write(line + "\n")
            LOG_FILE_HANDLE.flush()
        except OSError:
            pass


def log_warn(msg: str) -> None:
    log(msg, "WARN")


def log_err(msg: str) -> None:
    log(msg, "ERROR")


def log_ok(msg: str) -> None:
    log(msg, "OK")


def log_dry(msg: str) -> None:
    log(msg, "DRY")


# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

def get_config_path() -> Path:
    return get_app_data_dir() / CONFIG_FILE


def load_config(create_if_missing: bool = True) -> dict[str, Any]:
    cfg_path = get_config_path()
    cfg = dict(DEFAULTS)
    if not cfg_path.exists():
        if create_if_missing:
            save_config(cfg)
        return cfg
    try:
        with cfg_path.open("r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            cfg.update(raw)
    except (OSError, json.JSONDecodeError) as exc:
        log_err(f"Ошибка чтения config.json: {exc}")
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    clean = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    with get_config_path().open("w", encoding="utf-8") as fh:
        json.dump(clean, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Нормализация имён
# ---------------------------------------------------------------------------

_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def cyr_to_lat(text: str) -> str:
    if not text:
        return ""
    if translit is not None:
        try:
            return translit(text, "ru", reversed=True)
        except Exception:
            pass
    out = []
    for ch in text:
        low = ch.lower()
        if low in _TRANSLIT_MAP:
            tr = _TRANSLIT_MAP[low]
            out.append(tr.capitalize() if ch.isupper() else tr)
        else:
            out.append(ch)
    return "".join(out)


def normalize_key(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    if any("\u0400" <= ch <= "\u04ff" for ch in text):
        text = cyr_to_lat(text)
    text = unicodedata.normalize("NFKD", text.lower())
    return re.sub(r"[^a-z0-9]+", "", text)


def strip_track_number_prefix(name: str) -> str:
    return re.sub(r"^\s*[\[(]?\d{1,3}[\])]?\s*[-._)]*\s*", "", name)


def build_name_key(stem: str) -> str:
    return normalize_key(strip_track_number_prefix(stem))


_ARTIST_SUFFIX_RE = re.compile(r"\s*[-–—]\s*topic\s*$", re.IGNORECASE)
_NOISE_SUFFIX_RE = re.compile(
    r"\s*[\(\[](official\s*(music\s*)?video|official\s*audio|audio|lyrics?|visualizer|hd|4k)[\)\]]\s*",
    re.IGNORECASE,
)
_UNAVAILABLE_TITLES = {
    "[deleted video]", "[private video]", "[unavailable video]",
    "[removed video]", "deleted video", "private video",
}


def clean_artist(artist: str) -> str:
    if not artist:
        return ""
    artist = _ARTIST_SUFFIX_RE.sub("", artist)
    return re.sub(r"\s+", " ", artist).strip(" -–—\t\r\n")


def clean_title(title: str) -> str:
    if not title:
        return ""
    title = _NOISE_SUFFIX_RE.sub(" ", title)
    return re.sub(r"\s+", " ", title).strip(" -–—\t\r\n")


def is_unavailable_track(track: dict[str, Any]) -> Optional[str]:
    raw_title = (track.get("title") or "").strip()
    title = clean_title(raw_title)
    artist = clean_artist(track.get("artist") or "")
    if not title:
        return "пустое название"
    if raw_title.lower() in _UNAVAILABLE_TITLES or title.lower() in _UNAVAILABLE_TITLES:
        return f"недоступное видео ({raw_title})"
    if re.fullmatch(r"\[?\s*(deleted|private|unavailable|removed)\s+video\s*\]?", title, re.IGNORECASE):
        return f"недоступное видео ({title})"
    if not artist or artist.lower() in ("unknown artist", "unknown", "na", "n/a"):
        return f"неизвестный исполнитель ({artist or 'пусто'})"
    return None


def normalize_track(track: dict[str, Any]) -> dict[str, Any]:
    out = dict(track)
    out["artist"] = clean_artist(track.get("artist") or "")
    out["title"] = clean_title(track.get("title") or "")
    return out


def build_track_key(artist: str, title: str) -> str:
    return normalize_key(f"{clean_artist(artist)}{clean_title(title)}")


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return float(fuzz.ratio(a, b))


# ---------------------------------------------------------------------------
# Индекс фонотеки
# ---------------------------------------------------------------------------

@dataclass
class LibraryEntry:
    path: str
    size: int = 0
    mtime: float = 0.0
    name_key: str = ""
    fingerprint: str = ""
    duration: float = 0.0
    artist: str = ""
    title: str = ""
    album: str = ""
    year: str = ""
    track_key: str = ""
    title_key: str = ""
    tags_ok: bool = False
    tags_checked: bool = False
    duplicated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LibraryEntry":
        allowed = set(cls.__dataclass_fields__)
        entry = cls(**{k: v for k, v in data.items() if k in allowed})
        if not entry.title_key:
            entry.title_key = normalize_key(clean_title(entry.title or Path(entry.path).stem))
        return entry


class LibraryIndex:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, LibraryEntry] = {}

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log_err(f"Не удалось прочитать индекс: {exc}")
            return
        items = raw.get("files", []) if isinstance(raw, dict) else raw
        if isinstance(items, list):
            for item in items:
                try:
                    entry = LibraryEntry.from_dict(item)
                except TypeError:
                    continue
                self.entries[entry.path] = entry
        log(f"Индекс загружен: {len(self.entries)} записей.")

    def save(self) -> None:
        payload = {
            "version": 3,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "files": [e.to_dict() for e in self.entries.values()],
        }
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, path: Path | str) -> Optional[LibraryEntry]:
        return self.entries.get(str(path))

    def put(self, entry: LibraryEntry) -> None:
        self.entries[entry.path] = entry

    def active_entries(self) -> Iterable[LibraryEntry]:
        for entry in list(self.entries.values()):
            if not entry.duplicated and Path(entry.path).exists():
                yield entry


# ---------------------------------------------------------------------------
# Внешние утилиты: bin/ рядом с .exe -> sys._MEIPASS -> PATH
# ---------------------------------------------------------------------------

def resolve_binary(name: str) -> Optional[str]:
    exe = f"{name}.exe" if os.name == "nt" else name
    candidates = [get_bundle_dir() / "bin" / exe]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "bin" / exe)
        candidates.append(Path(meipass) / exe)
    for cand in candidates:
        if cand.exists():
            return str(cand)
    return shutil.which(name) or shutil.which(exe)


def check_binaries() -> dict[str, Optional[str]]:
    result = {n: resolve_binary(n) for n in ("ffmpeg", "ffprobe", "fpcalc")}
    for name, path in result.items():
        if path:
            log_ok(f"{name}: найден")
            bin_dir = str(Path(path).parent)
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        else:
            log_warn(f"{name} не найден. Поместите {name}.exe в папку bin/ рядом с программой.")
    return result


def _no_window_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def read_audio_duration(path: Path) -> float:
    try:
        audio = MutagenFile(path)
        if audio is not None and audio.info and audio.info.length:
            return float(audio.info.length)
    except Exception:
        pass
    ffprobe = resolve_binary("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=60, **_no_window_kwargs(),
            )
            if out.returncode == 0 and out.stdout.strip():
                return float(out.stdout.strip())
        except (subprocess.SubprocessError, ValueError, OSError):
            pass
    return 0.0


def compute_fingerprint(path: Path) -> tuple[str, float]:
    fpcalc = resolve_binary("fpcalc")
    if not fpcalc:
        return "", 0.0
    try:
        out = subprocess.run(
            [fpcalc, "-json", str(path)],
            capture_output=True, text=True, timeout=120, **_no_window_kwargs(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log_warn(f"fpcalc ошибка для {path.name}: {exc}")
        return "", 0.0
    if out.returncode != 0:
        return "", 0.0
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return "", 0.0
    return str(data.get("fingerprint", "")), float(data.get("duration", 0.0) or 0.0)


# ---------------------------------------------------------------------------
# Теги
# ---------------------------------------------------------------------------

def read_tags(path: Path) -> dict[str, str]:
    info = {"artist": "", "title": "", "album": "", "year": ""}
    try:
        audio = MutagenFile(path)
    except Exception:
        return info
    if audio is None:
        return info

    def first(tags: Any, keys: tuple[str, ...]) -> str:
        for k in keys:
            try:
                val = tags.get(k)
            except Exception:
                val = None
            if val:
                return str(val[0] if isinstance(val, list) else val)
        return ""

    try:
        if isinstance(audio, MP3):
            tags = audio.tags
            if tags is not None:
                for field, frame in (("artist", "TPE1"), ("title", "TIT2"),
                                     ("album", "TALB"), ("year", "TDRC")):
                    if frame in tags and tags[frame].text:
                        info[field] = str(tags[frame].text[0])
            return info
        tags = audio.tags
        if tags is None:
            return info
        info["artist"] = first(tags, ("artist", "\xa9ART", "ARTIST"))
        info["title"] = first(tags, ("title", "\xa9nam", "TITLE"))
        info["album"] = first(tags, ("album", "\xa9alb", "ALBUM"))
        info["year"] = first(tags, ("date", "year", "\xa9day", "DATE"))
    except Exception:
        pass
    return info


def write_tags(path: Path, artist: str, title: str, album: str = "", year: str = "",
               cover: Optional[tuple[bytes, str]] = None) -> bool:
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("TPE1"); tags.add(TPE1(encoding=3, text=[artist]))
            tags.delall("TIT2"); tags.add(TIT2(encoding=3, text=[title]))
            if album:
                tags.delall("TALB"); tags.add(TALB(encoding=3, text=[album]))
            if year:
                tags.delall("TDRC"); tags.add(TDRC(encoding=3, text=[year]))
            if cover:
                data, mime = cover
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            tags.save(path)
        elif ext == ".flac":
            audio = FLAC(path)
            audio["artist"] = artist
            audio["title"] = title
            if album:
                audio["album"] = album
            if year:
                audio["date"] = year
            if cover:
                data, mime = cover
                pic = Picture()
                pic.type = 3
                pic.mime = mime
                pic.desc = "Cover"
                pic.data = data
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
        else:
            audio = MutagenFile(path, easy=True)
            if audio is None:
                return False
            audio["artist"] = [artist]
            audio["title"] = [title]
            if album:
                audio["album"] = [album]
            if year:
                audio["date"] = [year]
            audio.save()
        return True
    except Exception as exc:
        log_warn(f"Не удалось записать теги в {path.name}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Метаданные: AcoustID, iTunes, MusicBrainz
# ---------------------------------------------------------------------------

def acoustid_lookup(fingerprint: str, duration: float, api_key: str) -> Optional[dict[str, Any]]:
    if not api_key or not fingerprint:
        return None
    try:
        resp = requests.get(
            "https://api.acoustid.org/v2/lookup",
            params={
                "client": api_key,
                "meta": "recordings releasegroups releases",
                "duration": int(duration or 0),
                "fingerprint": fingerprint,
                "format": "json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    results = data.get("results") or []
    if not results:
        return None
    best = max(results, key=lambda r: float(r.get("score", 0)))
    if float(best.get("score", 0)) < 0.5:
        return None
    recordings = best.get("recordings") or []
    if not recordings:
        return None
    rec = recordings[0]
    title = rec.get("title", "")
    if not title:
        return None
    artists = rec.get("artists") or []
    artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))
    album, year = "", ""
    releases = rec.get("releases") or []
    if releases:
        rel = releases[0]
        album = rel.get("title", "")
        date = rel.get("date") or {}
        if isinstance(date, dict):
            year = str(date.get("year") or "")
    return {"artist": artist or "Unknown Artist", "title": title, "album": album, "year": year}


def itunes_lookup(artist: str, title: str, duration: float = 0.0) -> Optional[dict[str, Any]]:
    """Сначала запрос 'Исполнитель Название'; если пусто - запрос по одному названию
    с отбором релиза по длительности (±5 сек)."""

    def _query(term: str, strict_artist: bool) -> Optional[dict[str, Any]]:
        if not term.strip():
            return None
        try:
            resp = requests.get(
                "https://itunes.apple.com/search",
                params={"term": term, "media": "music", "entity": "song", "limit": 10},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            results = resp.json().get("results") or []
        except (requests.RequestException, ValueError):
            return None
        best, best_score = None, 0.0
        for item in results:
            tname = item.get("trackName") or ""
            aname = item.get("artistName") or ""
            t_score = fuzz.token_set_ratio(tname.lower(), title.lower())
            if strict_artist:
                a_score = fuzz.token_set_ratio(aname.lower(), artist.lower())
                score = (t_score + a_score) / 2
            else:
                if t_score < 85:
                    continue
                item_dur = float(item.get("trackTimeMillis") or 0) / 1000.0
                if duration > 0 and item_dur > 0 and abs(item_dur - duration) <= 5.0:
                    score = t_score + 10.0
                else:
                    continue
            if score > best_score:
                best_score, best = score, item
        if not best or best_score < 65:
            return None
        artwork = best.get("artworkUrl100") or best.get("artworkUrl60") or ""
        artwork = artwork.replace("100x100bb", "1000x1000bb").replace("60x60bb", "1000x1000bb")
        release_date = best.get("releaseDate") or ""
        return {
            "album": best.get("collectionName") or "",
            "year": release_date[:4] if release_date else "",
            "cover_url": artwork,
            "artist": best.get("artistName") or artist,
            "title": best.get("trackName") or title,
        }

    return _query(f"{artist} {title}", True) or _query(title, False)


def musicbrainz_lookup(artist: str, title: str) -> Optional[dict[str, Any]]:
    try:
        resp = requests.get(
            "https://musicbrainz.org/ws/2/recording/",
            params={"query": f'artist:"{artist}" AND recording:"{title}"', "fmt": "json", "limit": 3},
            headers={"User-Agent": "YTMusicSync/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        recordings = resp.json().get("recordings") or []
    except (requests.RequestException, ValueError):
        return None
    if not recordings:
        return None
    releases = recordings[0].get("releases") or []
    album, year, cover_url = "", "", ""
    if releases:
        rel = releases[0]
        album = rel.get("title") or ""
        year = (rel.get("date") or "")[:4]
        rg = rel.get("release-group") or {}
        if isinstance(rg, dict) and rg.get("id"):
            cover_url = f"https://coverartarchive.org/release-group/{rg['id']}/front-500"
    return {"album": album, "year": year, "cover_url": cover_url, "artist": artist, "title": title}


def download_image(url: str) -> Optional[tuple[bytes, str]]:
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.content:
        return None
    ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if ctype not in ("image/jpeg", "image/png"):
        ctype = "image/jpeg"
    return resp.content, ctype


# ---------------------------------------------------------------------------
# Индексация и локальные дубли
# ---------------------------------------------------------------------------

def iter_audio_files(root: Path, dup_subdir: str = "_duplicates") -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != dup_subdir]
        for fname in filenames:
            if Path(fname).suffix.lower() in AUDIO_EXTENSIONS:
                yield Path(dirpath) / fname


def sanitize_filename(name: str, max_len: int = 150) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return (name[:max_len].rstrip(" .")) or "track"


def index_single_file(path: Path, index: LibraryIndex, api_key: str = "",
                      dry_run: bool = False) -> LibraryEntry:
    stat = path.stat()
    entry = index.get(path) or LibraryEntry(path=str(path))
    entry.size = stat.st_size
    entry.mtime = stat.st_mtime
    entry.name_key = build_name_key(path.stem)
    fingerprint, fp_duration = compute_fingerprint(path)
    duration = fp_duration or read_audio_duration(path)
    entry.fingerprint = fingerprint
    entry.duration = round(duration, 2)
    tags = read_tags(path)
    entry.artist, entry.title = tags["artist"], tags["title"]
    entry.album, entry.year = tags["album"], tags["year"]
    if not (tags["artist"] and tags["title"]) and api_key and fingerprint:
        found = acoustid_lookup(fingerprint, duration, api_key)
        time.sleep(API_PAUSE_SEC)
        if found:
            entry.artist, entry.title = found["artist"], found["title"]
            entry.album, entry.year = found["album"], found["year"]
            log_ok(f"  Распознано по звуку: {entry.artist} - {entry.title}")
            if not dry_run:
                write_tags(path, entry.artist, entry.title, entry.album, entry.year)
    entry.tags_ok = bool(entry.artist and entry.title)
    entry.tags_checked = True
    entry.track_key = build_track_key(entry.artist, entry.title) if entry.tags_ok else ""
    entry.title_key = normalize_key(clean_title(entry.title or path.stem))
    index.put(entry)
    return entry


def scan_library(music_dir: Path, index: LibraryIndex, cfg: dict[str, Any], dry_run: bool,
                 stop_event: Optional[threading.Event] = None) -> None:
    log("=== Этап 1: индексация локальной папки ===")
    if not music_dir.exists():
        log_err(f"Папка музыки не существует: {music_dir}")
        return
    api_key = (cfg.get("ACOUSTID_API_KEY") or "").strip() if not dry_run else ""
    dup_name = cfg.get("DUPLICATES_SUBDIR", "_duplicates")
    total = updated = 0
    for path in iter_audio_files(music_dir, dup_name):
        if stop_event and stop_event.is_set():
            return
        total += 1
        try:
            stat = path.stat()
        except OSError:
            continue
        existing = index.get(path)
        if (existing and existing.size == stat.st_size
                and abs(existing.mtime - stat.st_mtime) < 1.0 and existing.tags_checked):
            continue
        updated += 1
        log(f"  [{total}] Индексация: {path.name}")
        try:
            index_single_file(path, index, api_key, dry_run)
        except Exception as exc:
            log_warn(f"  Не удалось проиндексировать {path.name}: {exc}")
    log_ok(f"Просканировано файлов: {total}, новых/обновлённых: {updated}")
    if not dry_run:
        index.save()
    _remove_duplicates(music_dir, index, cfg, dry_run)


def _move_to_duplicates(src: Path, dup_dir: Path, entry: Optional[LibraryEntry],
                        index: LibraryIndex, dry_run: bool) -> None:
    if dry_run:
        log_dry(f"  [dry-run] переместить дубликат {src.name}")
        if entry:
            entry.duplicated = True
        return
    try:
        dup_dir.mkdir(parents=True, exist_ok=True)
        dst = dup_dir / src.name
        n = 1
        while dst.exists():
            dst = dup_dir / f"{src.stem}{n}{src.suffix}"
            n += 1
        shutil.move(str(src), str(dst))
        if entry:
            entry.duplicated = True
            index.put(entry)
    except OSError as exc:
        log_err(f"  Ошибка перемещения {src.name}: {exc}")


def _remove_duplicates(music_dir: Path, index: LibraryIndex, cfg: dict[str, Any], dry_run: bool) -> None:
    log("=== Поиск локальных дубликатов ===")
    dup_dir = music_dir / cfg.get("DUPLICATES_SUBDIR", "_duplicates")
    tol = float(cfg.get("DURATION_TOLERANCE_SEC", 3))
    entries = list(index.active_entries())
    entries.sort(key=lambda e: (not e.tags_ok, e.mtime))
    moved = 0
    keep: list[LibraryEntry] = []
    for entry in entries:
        reason = ""
        for k in keep:
            if entry.fingerprint and k.fingerprint and entry.fingerprint == k.fingerprint:
                reason = "совпадение отпечатка звука"
                break
            if entry.name_key and entry.name_key == k.name_key and abs(entry.duration - k.duration) <= tol:
                reason = "совпадение имени и длительности"
                break
            if (entry.title_key and k.title_key and len(entry.title_key) >= 4
                    and similarity(entry.title_key, k.title_key) >= 94
                    and entry.duration > 0 and abs(entry.duration - k.duration) <= tol):
                reason = "совпадение названия и длительности"
                break
        if not reason:
            keep.append(entry)
            continue
        src = Path(entry.path)
        log_warn(f"  Дубликат ({reason}): {src.name} -> {dup_dir.name}")
        _move_to_duplicates(src, dup_dir, entry, index, dry_run)
        moved += 1
    if moved:
        log_ok(f"Обработано дубликатов: {moved}")
    if not dry_run:
        index.save()


# ---------------------------------------------------------------------------
# yt-dlp: subprocess (из исходников) или встроенный модуль (в .exe)
# ---------------------------------------------------------------------------

def _cookie_attempts(cfg: dict[str, Any]) -> list[list[str]]:
    attempts: list[list[str]] = []
    browser = (cfg.get("BROWSER_FOR_COOKIES") or "").strip()
    if browser:
        attempts.append(["--cookies-from-browser", browser])
    cookie_file = get_app_data_dir() / COOKIES_FILE
    if cookie_file.exists():
        attempts.append(["--cookies", str(cookie_file)])
    attempts.append([])
    return attempts


def _exec_ytdlp_once(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    if getattr(sys, "frozen", False):
        # Внутри .exe: sys.executable - это сам exe, поэтому вызываем модуль напрямую.
        import yt_dlp
        out_buf, err_buf = io.StringIO(), io.StringIO()
        code = 0
        try:
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                yt_dlp.main(args)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        except Exception as exc:
            err_buf.write(str(exc))
            code = 1
        return subprocess.CompletedProcess(args, code, out_buf.getvalue(), err_buf.getvalue())
    return subprocess.run(
        [sys.executable, "-m", "yt_dlp"] + args,
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace", **_no_window_kwargs(),
    )


def _run_ytdlp(cfg: dict[str, Any], tail_args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    base: list[str] = []
    proxy = (cfg.get("PROXY_URL") or "").strip()
    if proxy:
        base += ["--proxy", proxy]
    base += ["--no-warnings", "--ignore-errors"]
    ffmpeg = resolve_binary("ffmpeg")
    if ffmpeg:
        base += ["--ffmpeg-location", str(Path(ffmpeg).parent)]
    proc: Optional[subprocess.CompletedProcess] = None
    for extra in _cookie_attempts(cfg):
        proc = _exec_ytdlp_once(base + extra + tail_args, timeout)
        if proc.returncode == 0:
            return proc
        blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
        if extra and ("cookie" in blob or "decrypt" in blob or "--cookies-from-browser" in extra):
            continue
        return proc
    return proc if proc is not None else subprocess.CompletedProcess(tail_args, 1, "", "yt-dlp failed")


def fetch_playlist(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    url = (cfg.get("YOUTUBE_PLAYLIST_URL") or "").strip()
    if not url:
        return []
    log("=== Этап 2: получение плейлиста ===")
    try:
        proc = _run_ytdlp(cfg, ["--flat-playlist", "--dump-json", url], timeout=180)
    except (subprocess.SubprocessError, OSError) as exc:
        log_err(f"yt-dlp ошибка: {exc}")
        return []
    tracks: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = data.get("id") or data.get("video_id")
        if not vid:
            continue
        title = data.get("title") or ""
        artist = (data.get("artist") or data.get("creator")
                  or data.get("uploader") or data.get("channel") or "")
        if " - " in title and (not artist or artist.lower().endswith("topic")):
            left, right = title.split(" - ", 1)
            artist = left.strip()
            title = right.strip()
        tracks.append({
            "video_id": vid,
            "title": clean_title(title),
            "artist": clean_artist(artist),
            "duration": float(data.get("duration") or 0),
            "thumbnail": data.get("thumbnail") or "",
            "url": data.get("url") or f"https://www.youtube.com/watch?v={vid}",
        })
    if not tracks:
        err = (proc.stderr or "").strip()
        if err:
            log_err(f"yt-dlp: {err[:300]}")
    log_ok(f"Получено треков в плейлисте: {len(tracks)}")
    return tracks


def load_downloaded_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    if not path.exists():
        return ids
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            for line in fh:
                s = line.strip()
                if s and not s.startswith("#"):
                    ids.add(s)
    except OSError:
        pass
    return ids


def append_downloaded_id(path: Path, video_id: str) -> None:
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(video_id + "\n")
    except OSError:
        pass


def find_local_match(track: dict[str, Any], index: LibraryIndex, threshold: float,
                     dur_tol: float) -> Optional[tuple[LibraryEntry, float]]:
    key_full = build_track_key(track["artist"], track["title"])
    key_title = normalize_key(track["title"])
    t_dur = float(track.get("duration") or 0)
    best: Optional[tuple[LibraryEntry, float]] = None
    best_score = 0.0
    for entry in index.active_entries():
        # 1) полная строка "Исполнитель + Название"
        for cand in (entry.track_key, entry.name_key):
            if not cand:
                continue
            if cand == key_full:
                return entry, 100.0
            score = similarity(cand, key_full)
            if score > best_score:
                best_score, best = score, (entry, score)
        # 2) только очищенное название + длительность (защита от перезаливов)
        if key_title and len(key_title) >= 4 and entry.title_key and t_dur > 0 and entry.duration > 0:
            t_score = similarity(entry.title_key, key_title)
            if t_score >= threshold and abs(entry.duration - t_dur) <= dur_tol:
                return entry, t_score
    if best and best_score >= threshold:
        return best
    return None


def filter_new_tracks(tracks: list[dict[str, Any]], index: LibraryIndex, downloaded: set[str],
                      cfg: dict[str, Any]) -> list[dict[str, Any]]:
    log("=== Этап 3: фильтрация дубликатов ===")
    threshold = float(cfg.get("FUZZY_SIMILARITY_THRESHOLD", 92))
    dur_tol = float(cfg.get("DURATION_TOLERANCE_SEC", 3))
    result: list[dict[str, Any]] = []
    skipped_id = skipped_local = skipped_unavail = 0
    for raw in tracks:
        track = normalize_track(raw)
        if is_unavailable_track(track):
            skipped_unavail += 1
            continue
        vid = track.get("video_id") or ""
        if not vid or vid in downloaded:
            skipped_id += 1
            continue
        match = find_local_match(track, index, threshold, dur_tol)
        if match:
            entry, score = match
            skipped_local += 1
            log(f"  ПРОПУСК ({score:.0f}%): {track['artist']} - {track['title']} ~ {Path(entry.path).name}")
            continue
        result.append(track)
    log_ok(f"Новых к скачиванию: {len(result)} (по id: {skipped_id}, "
           f"локально: {skipped_local}, недоступно: {skipped_unavail})")
    return result


# ---------------------------------------------------------------------------
# Скачивание и тегирование
# ---------------------------------------------------------------------------

_FALLBACK_MARKERS = ("video unavailable", "not available in your country",
                     "requested format is not available", "unavailable", "blocked")


def download_track(track: dict[str, Any], music_dir: Path, index: LibraryIndex,
                   cfg: dict[str, Any], dry_run: bool) -> bool:
    artist = sanitize_filename(track.get("artist") or "Unknown Artist")
    title = sanitize_filename(track.get("title") or track.get("video_id", "track"))
    out_format = (cfg.get("OUTPUT_FORMAT") or "mp3").lower()
    if out_format not in ("mp3", "flac"):
        out_format = "mp3"
    filename = f"{artist} - {title}.{out_format}"
    target = music_dir / filename
    if target.exists():
        log_warn(f"  Файл уже существует: {filename}")
        return False
    if dry_run:
        log_dry(f"  [dry-run] скачать {filename}")
        return False

    url = track.get("url") or f"https://www.youtube.com/watch?v={track['video_id']}"
    out_tmpl = str(music_dir / f"{artist} - {title}.%(ext)s")
    log(f"  Скачивание: {filename}")

    def _build_tail(target_url: str) -> list[str]:
        t = ["-f", "bestaudio/best", "-x", "--audio-format", out_format,
             "--audio-quality", "0", "-o", out_tmpl, "--no-playlist", target_url]
        if out_format == "mp3":
            t += ["--postprocessor-args", f"ffmpeg:-b:a {cfg.get('MP3_BITRATE', '320k')}"]
        return t

    proc = _run_ytdlp(cfg, _build_tail(url), timeout=600)
    if proc.returncode != 0 or not target.exists():
        blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
        if proc.returncode != 0 and any(m in blob for m in _FALLBACK_MARKERS):
            query = f"ytsearch1:{track.get('artist')} - {track.get('title')}"
            log_warn(f"  Прямая ссылка недоступна, резервный поиск: {query}")
            proc = _run_ytdlp(cfg, _build_tail(query), timeout=600)
        if proc.returncode != 0 and not target.exists():
            log_err(f"  Не удалось скачать: {(proc.stderr or '').strip()[-200:]}")
            return False

    downloaded: Optional[Path] = target if target.exists() else None
    if downloaded is None:
        needle = title.lower()[:20]
        for cand in music_dir.glob(f"*.{out_format}"):
            if cand.stem.lower().startswith(artist.lower()[:10]) and needle in cand.stem.lower():
                downloaded = cand
                break
    if downloaded is None:
        log_err("  Файл после скачивания не найден.")
        return False

    # Отпечаток нового файла: если такой звук уже есть - в _duplicates
    fp, dur = compute_fingerprint(downloaded)
    if fp:
        for existing in index.active_entries():
            if existing.fingerprint and existing.fingerprint == fp:
                log_warn(f"  Дубль по звуку ({Path(existing.path).name}) -> в _duplicates")
                dup_dir = music_dir / cfg.get("DUPLICATES_SUBDIR", "_duplicates")
                _move_to_duplicates(downloaded, dup_dir, None, index, dry_run=False)
                return True  # video_id будет записан в downloaded_ids.txt

    lookup_artist = clean_artist(track.get("artist", ""))
    lookup_title = clean_title(track.get("title", ""))
    real_dur = dur or read_audio_duration(downloaded) or float(track.get("duration") or 0)
    meta = itunes_lookup(lookup_artist, lookup_title, real_dur)
    time.sleep(API_PAUSE_SEC)
    if not meta:
        meta = musicbrainz_lookup(lookup_artist, lookup_title)
        time.sleep(API_PAUSE_SEC)
    album = (meta or {}).get("album") or ""
    year = (meta or {}).get("year") or ""
    cover_url = (meta or {}).get("cover_url") or track.get("thumbnail") or ""
    cover = download_image(cover_url) if cover_url else None
    if write_tags(downloaded, lookup_artist or "Unknown Artist", lookup_title, album, year, cover):
        log_ok(f"  Готово: {album or 'сингл'} {year}".strip())
    index_single_file(downloaded, index, api_key="", dry_run=False)
    return True


def check_playlist_updates(cfg: dict[str, Any]) -> tuple[int, int]:
    """Мягкая проверка: возвращает (всего_в_плейлисте, новых_треков)."""
    music_dir = Path(cfg.get("MUSIC_DIR") or "").expanduser()
    url = (cfg.get("YOUTUBE_PLAYLIST_URL") or "").strip()
    if not url or not cfg.get("MUSIC_DIR") or not music_dir.exists():
        return 0, 0
    app_data = get_app_data_dir()
    index = LibraryIndex(app_data / cfg.get("LIBRARY_INDEX_FILE", "library_index.json"))
    index.load()
    tracks = fetch_playlist(cfg)
    downloaded = load_downloaded_ids(app_data / cfg.get("DOWNLOADED_IDS_FILE", "downloaded_ids.txt"))
    new_tracks = filter_new_tracks(tracks, index, downloaded, cfg)
    return len(tracks), len(new_tracks)


def run_sync(cfg: dict[str, Any], dry_run: bool = False, limit: Optional[int] = None,
             stop_event: Optional[threading.Event] = None,
             progress_cb: Optional[Callable[[int, int], None]] = None) -> int:
    if dry_run:
        log_warn("Пробный режим (dry-run): файлы не изменяются.")
    if not cfg.get("MUSIC_DIR"):
        log_err("Папка музыки не указана.")
        return 1
    music_dir = Path(cfg["MUSIC_DIR"]).expanduser()
    if not music_dir.exists():
        log_err(f"Папка музыки не найдена: {music_dir}")
        return 1
    app_data = get_app_data_dir()
    check_binaries()
    index = LibraryIndex(app_data / cfg.get("LIBRARY_INDEX_FILE", "library_index.json"))
    index.load()

    scan_library(music_dir, index, cfg, dry_run, stop_event)
    if stop_event and stop_event.is_set():
        log_warn("Остановлено пользователем.")
        return 0

    tracks = fetch_playlist(cfg)
    if stop_event and stop_event.is_set():
        return 0
    ids_file = app_data / cfg.get("DOWNLOADED_IDS_FILE", "downloaded_ids.txt")
    downloaded_ids = load_downloaded_ids(ids_file)
    new_tracks = filter_new_tracks(tracks, index, downloaded_ids, cfg)

    log("=== Этап 4: скачивание новинок ===")
    if not new_tracks:
        log_ok("Новых треков нет - коллекция актуальна.")
        return 0
    if limit and limit > 0:
        new_tracks = new_tracks[:limit]

    ok = 0
    total = len(new_tracks)
    for i, track in enumerate(new_tracks, 1):
        if stop_event and stop_event.is_set():
            log_warn("Синхронизация остановлена пользователем.")
            break
        if progress_cb:
            progress_cb(i - 1, total)
        log(f"[{i}/{total}] {track.get('artist')} - {track.get('title')}")
        try:
            success = download_track(track, music_dir, index, cfg, dry_run)
        except Exception as exc:
            log_err(f"  Ошибка при обработке трека: {exc}")
            success = False
        if success:
            ok += 1
            if not dry_run:
                append_downloaded_id(ids_file, track["video_id"])
        if progress_cb:
            progress_cb(i, total)
        time.sleep(API_PAUSE_SEC)
    if not dry_run:
        index.save()
    log_ok(f"Синхронизация завершена. Скачано: {ok} из {total}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Синхронизация плейлиста YouTube Music.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    return run_sync(load_config(), dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
