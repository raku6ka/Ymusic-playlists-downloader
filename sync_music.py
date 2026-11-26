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
from mutagen.id3 import (APIC, ID3, ID3NoHeaderError, TALB, TBPM, TCON, TCOP, TDRC, TIT2,
                         TPE1, TPE2, TPOS, TPUB, TRCK, TSRC)
from mutagen.mp3 import MP3
from rapidfuzz import fuzz

try:
    from transliterate import translit
except Exception:
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

    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    path = Path(base) / "YTMusicSync"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_bundle_dir() -> Path:

    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(sys.executable)).resolve()
    return Path(__file__).resolve().parent


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
_ARTIST_NOISE_RE = re.compile(
    r"\s*[\(\[]\s*official\s*[\)\]]\s*$|\s+official\s*$|\s*vevo\s*$", re.IGNORECASE
)
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
    artist = _ARTIST_NOISE_RE.sub("", artist)
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
    cover_tried: bool = False
    meta_tried: bool = False

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
    result = {n: resolve_binary(n) for n in ("ffmpeg", "ffprobe", "fpcalc", "deno")}
    for name, path in result.items():
        if name == "deno":
            if path:
                log_ok("deno: найден")
            elif resolve_binary("node"):
                log_ok("node: найден (используется вместо deno)")
            else:
                log_warn("deno/node не найден: YouTube может отвечать 'Requested format is not available'. "
                         "Поместите deno.exe в папку bin/.")
            continue
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

    full = _itunes_full(artist, title, duration)
    if not full:
        return None
    return {
        "album": full.get("album") or "",
        "year": full.get("year") or "",
        "cover_url": full.get("cover_url") or "",
        "artist": full.get("artist") or artist,
        "title": full.get("title") or title,
    }


def _musicbrainz_full(artist: str, title: str, duration: float) -> Optional[dict[str, Any]]:
    try:
        resp = requests.get(
            "https://musicbrainz.org/ws/2/recording/",
            params={"query": f'artist:"{artist}" AND recording:"{title}"', "fmt": "json", "limit": 5},
            headers={"User-Agent": "YTMusicSync/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        recordings = resp.json().get("recordings") or []
    except (requests.RequestException, ValueError):
        return None
    for rec in recordings:
        if not _title_ok(title, rec.get("title") or ""):
            continue
        names = []
        for credit in rec.get("artist-credit") or []:
            if isinstance(credit, dict):
                names.append(credit.get("name") or "")
                names.append((credit.get("artist") or {}).get("name") or "")
        if not any(_artist_ok(artist, n) for n in names if n):
            continue
        if not _dur_ok(duration, float(rec.get("length") or 0) / 1000.0):
            continue
        out: dict[str, Any] = {"title": rec.get("title") or "", "artist": artist}
        isrcs = rec.get("isrcs") or []
        if isrcs:
            first = isrcs[0]
            out["isrc"] = (first.get("id") if isinstance(first, dict) else str(first)) or ""
        releases = rec.get("releases") or []
        if releases:
            rel = releases[0]
            out["album"] = rel.get("title") or ""
            out["year"] = (rel.get("date") or "")[:4]
            media = (rel.get("media") or [{}])[0] or {}
            if "track-offset" in media:
                out["track_no"] = int(media.get("track-offset") or 0) + 1
            out["track_total"] = media.get("track-count") or 0
            out["disc_no"] = media.get("position") or 0
            rg = rel.get("release-group") or {}
            if isinstance(rg, dict) and rg.get("id"):
                out["cover_url"] = f"https://coverartarchive.org/release-group/{rg['id']}/front-500"
        return out
    return None


def musicbrainz_lookup(artist: str, title: str, duration: float = 0.0) -> Optional[dict[str, Any]]:
    full = _musicbrainz_full(artist, title, duration)
    if not full:
        return None
    return {
        "album": full.get("album") or "",
        "year": full.get("year") or "",
        "cover_url": full.get("cover_url") or "",
        "artist": artist,
        "title": title,
    }


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


def has_cover(path: Path) -> bool:

    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            return bool(ID3(path).getall("APIC"))
        if ext == ".flac":
            return bool(FLAC(path).pictures)
    except ID3NoHeaderError:
        return False
    except Exception:
        return False
    return True


def embed_cover(path: Path, cover: tuple[bytes, str]) -> bool:
    data, mime = cover
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            tags.save(path)
            return True
        if ext == ".flac":
            audio = FLAC(path)
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.desc = "Cover"
            pic.data = data
            audio.clear_pictures()
            audio.add_picture(pic)
            audio.save()
            return True
    except Exception as exc:
        log_warn(f"  Не удалось встроить обложку в {path.name}: {exc}")
    return False


def deezer_cover_url(artist: str, title: str, duration: float = 0.0) -> str:

    item = _deezer_match(artist, title, duration)
    if not item:
        return ""
    album = item.get("album") or {}
    return album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium") or ""


def find_cover(artist: str, title: str, duration: float = 0.0,
               extra_urls: Iterable[str] = ()) -> Optional[tuple[bytes, str]]:


    def _valid(img: Optional[tuple[bytes, str]]) -> Optional[tuple[bytes, str]]:
        return img if img and len(img[0]) > 2000 else None

    meta = itunes_lookup(artist, title, duration)
    time.sleep(API_PAUSE_SEC)
    if meta and meta.get("cover_url"):
        img = _valid(download_image(meta["cover_url"]))
        if img:
            return img
    url = deezer_cover_url(artist, title, duration)
    if url:
        img = _valid(download_image(url))
        if img:
            return img
    meta = musicbrainz_lookup(artist, title, duration)
    time.sleep(API_PAUSE_SEC)
    if meta and meta.get("cover_url"):
        img = _valid(download_image(meta["cover_url"]))
        if img:
            return img
    for extra in extra_urls:
        if extra:
            img = _valid(download_image(extra))
            if img:
                return img
    return None


def _artist_title_for_file(path: Path, entry: Optional[LibraryEntry]) -> tuple[str, str]:
    artist = (entry.artist if entry else "") or ""
    title = (entry.title if entry else "") or ""
    if not (artist and title):
        tags = read_tags(path)
        artist, title = tags["artist"], tags["title"]
    if not (artist and title):
        stem = strip_track_number_prefix(path.stem)
        if " - " in stem:
            left, right = stem.split(" - ", 1)
            artist, title = artist or left.strip(), title or right.strip()
        else:
            title = title or stem
    return clean_artist(artist), clean_title(title)


def run_cover_check(cfg: dict[str, Any], stop_event: Optional[threading.Event] = None,
                    progress_cb: Optional[Callable[[int, int], None]] = None,
                    force: bool = False, dry_run: bool = False,
                    index: Optional[LibraryIndex] = None) -> tuple[int, int]:


    music_dir = Path(cfg.get("MUSIC_DIR") or "").expanduser()
    if not cfg.get("MUSIC_DIR") or not music_dir.exists():
        log_err("Папка музыки не найдена - проверка обложек пропущена.")
        return 0, 0
    own_index = index is None
    if own_index:
        index = LibraryIndex(get_app_data_dir() / cfg.get("LIBRARY_INDEX_FILE", "library_index.json"))
        index.load()
    log("=== Проверка обложек ===")
    dup_name = cfg.get("DUPLICATES_SUBDIR", "_duplicates")
    missing: list[Path] = []
    for path in iter_audio_files(music_dir, dup_name):
        if path.suffix.lower() not in (".mp3", ".flac"):
            continue
        entry = index.get(path)
        if entry and entry.cover_tried and not force:
            continue
        if not has_cover(path):
            missing.append(path)
    if not missing:
        log_ok("У всех треков есть обложки.")
        return 0, 0
    log(f"Треков без обложки: {len(missing)}")
    added = 0
    total = len(missing)
    for i, path in enumerate(missing, 1):
        if stop_event and stop_event.is_set():
            log_warn("Проверка обложек остановлена пользователем.")
            break
        if progress_cb:
            progress_cb(i - 1, total)
        entry = index.get(path)
        artist, title = _artist_title_for_file(path, entry)
        if not title:
            continue
        if dry_run:
            log_dry(f"  [dry-run] найти обложку: {path.name}")
            continue
        duration = (entry.duration if entry and entry.duration else read_audio_duration(path))
        cover = find_cover(artist, title, duration)
        if cover and embed_cover(path, cover):
            added += 1
            log_ok(f"  Обложка добавлена: {path.name}")
        else:
            log_warn(f"  Обложка не найдена: {path.name}")
        if entry:
            entry.cover_tried = True
            try:
                st = path.stat()
                entry.size, entry.mtime = st.st_size, st.st_mtime
            except OSError:
                pass
            index.put(entry)
        if progress_cb:
            progress_cb(i, total)
        time.sleep(API_PAUSE_SEC)
    if not dry_run and own_index:
        index.save()
    log_ok(f"Проверка обложек завершена. Добавлено: {added} из {total}.")
    return total, added


def _title_ok(a: str, b: str) -> bool:

    ka, kb = normalize_key(clean_title(a)), normalize_key(clean_title(b))
    return bool(ka and kb) and similarity(ka, kb) >= 88


def _artist_ok(a: str, b: str) -> bool:
    if not a:
        return True
    if not b:
        return False
    ka, kb = normalize_key(a), normalize_key(b)
    if ka and kb:
        if similarity(ka, kb) >= 70:
            return True
        if len(ka) >= 3 and len(kb) >= 3 and (ka in kb or kb in ka):
            return True
    return float(fuzz.token_set_ratio(a.lower(), b.lower())) >= 80


def _dur_ok(want: float, got: float, tol: float = 5.0) -> bool:
    if want > 0 and got > 0:
        return abs(want - got) <= tol
    return True


def _itunes_full(artist: str, title: str, duration: float) -> Optional[dict[str, Any]]:
    best, best_score = None, 0.0
    for term in (f"{artist} {title}".strip(), title):
        try:
            resp = requests.get(
                "https://itunes.apple.com/search",
                params={"term": term, "media": "music", "entity": "song", "limit": 10},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            results = resp.json().get("results") or []
        except (requests.RequestException, ValueError):
            continue
        for item in results:
            tname = item.get("trackName") or ""
            if not _title_ok(title, tname):
                continue
            if not _artist_ok(artist, item.get("artistName") or ""):
                continue
            if not _dur_ok(duration, float(item.get("trackTimeMillis") or 0) / 1000.0):
                continue
            score = similarity(normalize_key(title), normalize_key(tname))
            if score > best_score:
                best_score, best = score, item
        if best:
            break
        time.sleep(API_PAUSE_SEC)
    if not best:
        return None
    artwork = (best.get("artworkUrl100") or best.get("artworkUrl60") or "")
    artwork = artwork.replace("100x100bb", "1000x1000bb").replace("60x60bb", "1000x1000bb")
    out: dict[str, Any] = {
        "title": best.get("trackName") or "",
        "artist": best.get("artistName") or "",
        "album": best.get("collectionName") or "",
        "album_artist": best.get("collectionArtistName") or best.get("artistName") or "",
        "track_no": best.get("trackNumber") or 0,
        "track_total": best.get("trackCount") or 0,
        "disc_no": best.get("discNumber") or 0,
        "disc_total": best.get("discCount") or 0,
        "genre": best.get("primaryGenreName") or "",
        "year": (best.get("releaseDate") or "")[:4],
        "cover_url": artwork,
    }
    cid = best.get("collectionId")
    if cid:
        try:
            r = requests.get("https://itunes.apple.com/lookup", params={"id": cid}, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                res = r.json().get("results") or []
                if res:
                    out["copyright"] = res[0].get("copyright") or ""
        except (requests.RequestException, ValueError):
            pass
    return out


def _deezer_match(artist: str, title: str, duration: float) -> Optional[dict[str, Any]]:
    for query in (f'artist:"{artist}" track:"{title}"', f"{artist} {title}".strip(), title):
        if not query.strip():
            continue
        try:
            resp = requests.get("https://api.deezer.com/search",
                                params={"q": query, "limit": 5}, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue
            items = resp.json().get("data") or []
        except (requests.RequestException, ValueError):
            continue
        for item in items:
            if not _title_ok(title, item.get("title") or ""):
                continue
            if not _artist_ok(artist, (item.get("artist") or {}).get("name") or ""):
                continue
            if not _dur_ok(duration, float(item.get("duration") or 0)):
                continue
            return item
    return None


def _deezer_get(path: str) -> dict[str, Any]:
    try:
        resp = requests.get(f"https://api.deezer.com/{path}", timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and not data.get("error"):
                return data
    except (requests.RequestException, ValueError):
        pass
    return {}


def _deezer_full(artist: str, title: str, duration: float) -> Optional[dict[str, Any]]:
    item = _deezer_match(artist, title, duration)
    if not item:
        return None
    album = item.get("album") or {}
    out: dict[str, Any] = {
        "title": item.get("title") or "",
        "artist": (item.get("artist") or {}).get("name") or "",
        "album": album.get("title") or "",
        "cover_url": album.get("cover_xl") or album.get("cover_big") or "",
    }
    track = _deezer_get(f"track/{item.get('id')}")
    if track:
        out["isrc"] = track.get("isrc") or ""
        bpm = track.get("bpm") or 0
        out["bpm"] = int(round(float(bpm))) if bpm else 0
        out["track_no"] = track.get("track_position") or 0
        out["disc_no"] = track.get("disk_number") or 0
        out["year"] = (track.get("release_date") or "")[:4]
    album_id = (track.get("album") or album).get("id") if (track or album) else None
    if album_id:
        alb = _deezer_get(f"album/{album_id}")
        if alb:
            genres = (alb.get("genres") or {}).get("data") or []
            out["genre"] = genres[0].get("name", "") if genres else ""
            out["publisher"] = alb.get("label") or ""
            out["track_total"] = alb.get("nb_tracks") or 0
            out["album_artist"] = (alb.get("artist") or {}).get("name") or ""
    return out


_ALBUM_GROUP = ("album", "album_artist", "track_no", "track_total", "disc_no", "disc_total", "year", "cover_url")


class MetadataProvider:
    name = ""

    def lookup(self, artist: str, title: str, duration: float) -> Optional[dict[str, Any]]:
        return None


class ItunesProvider(MetadataProvider):
    name = "itunes"

    def lookup(self, artist: str, title: str, duration: float) -> Optional[dict[str, Any]]:
        return _itunes_full(artist, title, duration)


class DeezerProvider(MetadataProvider):
    name = "deezer"

    def lookup(self, artist: str, title: str, duration: float) -> Optional[dict[str, Any]]:
        return _deezer_full(artist, title, duration)


class MusicBrainzProvider(MetadataProvider):
    name = "musicbrainz"

    def lookup(self, artist: str, title: str, duration: float) -> Optional[dict[str, Any]]:
        return _musicbrainz_full(artist, title, duration)


METADATA_PROVIDERS: list[MetadataProvider] = [ItunesProvider(), DeezerProvider(), MusicBrainzProvider()]


def register_metadata_provider(provider: MetadataProvider) -> None:
    METADATA_PROVIDERS.append(provider)


def _collect_metadata(artist: str, title: str, duration: float) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for provider in METADATA_PROVIDERS:
        try:
            found = provider.lookup(artist, title, duration)
        except Exception as exc:
            log_warn(f"  Источник {provider.name}: {exc}")
            found = None
        time.sleep(API_PAUSE_SEC)
        if found:
            results.append(found)
    return results


def _merge_metadata(results: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not results:
        return None
    primary = next((r for r in results if r.get("album")), results[0])
    meta = {k: v for k, v in primary.items() if v not in (None, "", 0)}
    for other in results:
        if other is primary:
            continue
        for key, value in other.items():
            if key in _ALBUM_GROUP or value in (None, "", 0):
                continue
            if not meta.get(key):
                meta[key] = value
    return meta


def fetch_full_metadata(artist: str, title: str, duration: float = 0.0, fingerprint: str = "",
                        cfg: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    meta = _merge_metadata(_collect_metadata(artist, title, duration))
    api_key = ((cfg or {}).get("ACOUSTID_API_KEY") or "").strip()
    if meta or not api_key or not fingerprint:
        return meta
    found = acoustid_lookup(fingerprint, duration, api_key)
    time.sleep(API_PAUSE_SEC)
    if not found or not found.get("title"):
        return None
    log(f"  AcoustID определил трек: {found.get('artist')} - {found['title']}")
    meta = _merge_metadata(_collect_metadata(found.get("artist") or artist, found["title"], duration))
    if meta:
        return meta
    fallback = {k: found.get(k) for k in ("artist", "title", "album", "year") if found.get(k)}
    return fallback or None


_YT_SIZE_RE = re.compile(r"=w\d+-h\d+.*$")


def ytmusic_cover_url(track: dict[str, Any]) -> str:
    thumbs = sorted(track.get("thumbnails") or [],
                    key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), reverse=True)
    for thumb in thumbs:
        url = thumb.get("url") or ""
        if "googleusercontent.com" in url or "ggpht.com" in url:
            return _YT_SIZE_RE.sub("=w1000-h1000-l90-rj", url)
    return ""


def pop_ytdlp_info(info_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if info_path.exists():
        try:
            with info_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
        try:
            info_path.unlink()
        except OSError:
            pass
    if not data.get("track"):
        return {}
    artist = data.get("artist") or ", ".join(data.get("artists") or []) or ""
    year = str(data.get("release_year") or (data.get("release_date") or "")[:4] or "")
    return {
        "album": data.get("album") or "",
        "year": year,
        "artist": clean_artist(artist),
        "title": clean_title(data.get("track") or ""),
    }


def _album_key(text: str) -> str:
    return normalize_key(re.sub(r"[\(\[].*?[\)\]]", "", text or ""))


def merge_ytmusic_meta(yt: dict[str, Any], meta: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not yt:
        return meta
    out = dict(meta or {})
    yt_album = yt.get("album") or ""
    if yt_album:
        current = out.get("album") or ""
        same = bool(current) and similarity(_album_key(yt_album), _album_key(current)) >= 85
        if not same:
            for key in _ALBUM_GROUP:
                out.pop(key, None)
            out["album"] = yt_album
            out["_album_from_yt"] = True
        if yt.get("year") and not out.get("year"):
            out["year"] = yt["year"]
    elif yt.get("year") and not out.get("year"):
        out["year"] = yt["year"]
    if yt.get("artist") and not out.get("artist"):
        out["artist"] = yt["artist"]
    if yt.get("title") and not out.get("title"):
        out["title"] = yt["title"]
    return out or None


def write_full_tags(path: Path, meta: dict[str, Any], only_missing: bool = True) -> list[str]:


    ext = path.suffix.lower()
    changed: list[str] = []

    def pair(a: Any, b: Any) -> str:
        try:
            a_i, b_i = int(a or 0), int(b or 0)
        except (TypeError, ValueError):
            return ""
        if a_i <= 0:
            return ""
        return f"{a_i}/{b_i}" if b_i > 0 else str(a_i)

    try:
        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()

            def put(frame: str, cls: Any, value: Any, label: str) -> None:
                if not value:
                    return
                cur = tags.get(frame)
                if cur is not None and str(cur).strip():
                    if only_missing or frame in ("TIT2", "TPE1"):
                        return
                    if str(cur).strip() == str(value).strip():
                        return
                tags.delall(frame)
                tags.add(cls(encoding=3, text=[str(value)]))
                changed.append(label)

            put("TIT2", TIT2, meta.get("title"), "название")
            put("TPE1", TPE1, meta.get("artist"), "исполнитель")
            put("TALB", TALB, meta.get("album"), "альбом")
            put("TPE2", TPE2, meta.get("album_artist"), "исп. альбома")
            put("TRCK", TRCK, pair(meta.get("track_no"), meta.get("track_total")), "трек №")
            put("TPOS", TPOS, pair(meta.get("disc_no"), meta.get("disc_total")), "диск №")
            put("TCON", TCON, meta.get("genre"), "жанр")
            put("TDRC", TDRC, meta.get("year"), "год")
            put("TCOP", TCOP, meta.get("copyright"), "авторские права")
            put("TPUB", TPUB, meta.get("publisher"), "издатель")
            put("TSRC", TSRC, meta.get("isrc"), "ISRC")
            put("TBPM", TBPM, meta.get("bpm"), "BPM")
            if changed:
                tags.save(path)
        elif ext == ".flac":
            audio = FLAC(path)

            def putv(key: str, value: Any, label: str) -> None:
                if not value:
                    return
                cur = audio.get(key)
                if cur and any(str(x).strip() for x in cur):
                    if only_missing or key in ("title", "artist"):
                        return
                    if [str(x).strip() for x in cur] == [str(value).strip()]:
                        return
                audio[key] = [str(value)]
                changed.append(label)

            putv("title", meta.get("title"), "название")
            putv("artist", meta.get("artist"), "исполнитель")
            putv("album", meta.get("album"), "альбом")
            putv("albumartist", meta.get("album_artist"), "исп. альбома")
            putv("tracknumber", meta.get("track_no"), "трек №")
            putv("tracktotal", meta.get("track_total"), "всего треков")
            putv("discnumber", meta.get("disc_no"), "диск №")
            putv("disctotal", meta.get("disc_total"), "всего дисков")
            putv("genre", meta.get("genre"), "жанр")
            putv("date", meta.get("year"), "год")
            putv("copyright", meta.get("copyright"), "авторские права")
            putv("organization", meta.get("publisher"), "издатель")
            putv("isrc", meta.get("isrc"), "ISRC")
            putv("bpm", meta.get("bpm"), "BPM")
            if changed:
                audio.save()
    except Exception as exc:
        log_warn(f"  Не удалось записать теги в {path.name}: {exc}")
        return []
    return changed


def run_metadata_enrich(cfg: dict[str, Any], stop_event: Optional[threading.Event] = None,
                        progress_cb: Optional[Callable[[int, int], None]] = None,
                        dry_run: bool = False, force: bool = False, overwrite: bool = False,
                        index: Optional[LibraryIndex] = None) -> tuple[int, int]:


    music_dir = Path(cfg.get("MUSIC_DIR") or "").expanduser()
    if not cfg.get("MUSIC_DIR") or not music_dir.exists():
        log_err("Папка музыки не найдена.")
        return 0, 0
    own_index = index is None
    if own_index:
        index = LibraryIndex(get_app_data_dir() / cfg.get("LIBRARY_INDEX_FILE", "library_index.json"))
        index.load()
    log("=== Исправление тегов библиотеки (перезапись) ===" if overwrite else "=== Дополнение тегов библиотеки ===")
    dup_name = cfg.get("DUPLICATES_SUBDIR", "_duplicates")
    todo: list[Path] = []
    for path in iter_audio_files(music_dir, dup_name):
        if path.suffix.lower() not in (".mp3", ".flac"):
            continue
        entry = index.get(path)
        if entry and entry.meta_tried and not force and not overwrite:
            continue
        todo.append(path)
    if not todo:
        log_ok("Все файлы уже обработаны.")
        return 0, 0
    log(f"Файлов к обработке: {len(todo)}")
    total, updated = len(todo), 0
    for i, path in enumerate(todo, 1):
        if stop_event and stop_event.is_set():
            log_warn("Остановлено. Обработанные файлы будут пропущены при следующем запуске.")
            break
        if progress_cb:
            progress_cb(i - 1, total)
        entry = index.get(path)
        artist, title = _artist_title_for_file(path, entry)
        if not title:
            continue
        if dry_run:
            log_dry(f"  [dry-run] [{i}/{total}] {path.name}")
            continue
        duration = (entry.duration if entry and entry.duration else read_audio_duration(path))
        fingerprint = entry.fingerprint if entry and entry.fingerprint else ""
        if not fingerprint and (cfg.get("ACOUSTID_API_KEY") or "").strip():
            fingerprint, _ = compute_fingerprint(path)
        meta = fetch_full_metadata(artist, title, duration, fingerprint=fingerprint, cfg=cfg)
        if meta:
            changed = write_full_tags(path, meta, only_missing=not overwrite)
            if meta.get("cover_url") and (overwrite or not has_cover(path)):
                img = download_image(meta["cover_url"])
                if img and len(img[0]) > 2000 and embed_cover(path, img):
                    changed.append("обложка")
            if changed:
                updated += 1
                log_ok(f"  [{i}/{total}] {path.name}: добавлено - {', '.join(changed)}")
            else:
                log(f"  [{i}/{total}] {path.name}: данные уже заполнены")
        else:
            log_warn(f"  [{i}/{total}] Не найдено в базах (пропуск): {path.name}")
        try:
            st = path.stat()
        except OSError:
            st = None
        if entry is None:
            entry = LibraryEntry(path=str(path))
        entry.meta_tried = True
        if st:
            entry.size, entry.mtime = st.st_size, st.st_mtime
        index.put(entry)
        if own_index and i % 25 == 0:
            index.save()
        if progress_cb:
            progress_cb(i, total)
    if not dry_run and own_index:
        index.save()
    log_ok(f"Дополнение тегов завершено. Изменено файлов: {updated} из {total}.")
    return total, updated


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
    entry.cover_tried = False
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

    for runtime in ("deno", "node"):
        rt_path = resolve_binary(runtime)
        if rt_path:
            base += ["--js-runtimes", f"{runtime}:{rt_path}"]
            break
    base += ["--remote-components", "ejs:github"]
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


class PlaylistSource:
    name = ""

    def matches(self, url: str) -> bool:
        return False

    def fetch(self, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        return []


class YouTubeMusicSource(PlaylistSource):
    name = "youtube"

    def matches(self, url: str) -> bool:
        return True

    def fetch(self, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        url = (cfg.get("YOUTUBE_PLAYLIST_URL") or "").strip()
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
                "thumbnails": data.get("thumbnails") or [],
                "url": data.get("url") or f"https://www.youtube.com/watch?v={vid}",
            })
        if not tracks:
            err = (proc.stderr or "").strip()
            if err:
                log_err(f"yt-dlp: {err[:300]}")
        return tracks


EXTRA_PLAYLIST_SOURCES: list[PlaylistSource] = []
DEFAULT_PLAYLIST_SOURCE = YouTubeMusicSource()


def register_playlist_source(source: PlaylistSource) -> None:
    EXTRA_PLAYLIST_SOURCES.append(source)


def fetch_playlist(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    url = (cfg.get("YOUTUBE_PLAYLIST_URL") or "").strip()
    if not url:
        return []
    log("=== Этап 2: получение плейлиста ===")
    source = next((src for src in EXTRA_PLAYLIST_SOURCES if src.matches(url)), DEFAULT_PLAYLIST_SOURCE)
    try:
        tracks = source.fetch(cfg)
    except Exception as exc:
        log_err(f"Ошибка источника {source.name}: {exc}")
        return []
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

        for cand in (entry.track_key, entry.name_key):
            if not cand:
                continue
            if cand == key_full:
                return entry, 100.0
            score = similarity(cand, key_full)
            if score > best_score:
                best_score, best = score, (entry, score)

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

    url = track.get("url") or f"ytsearch1:{track.get('artist')} - {track.get('title')}"
    out_tmpl = str(music_dir / f"{artist} - {title}.%(ext)s")
    info_path = music_dir / f"{artist} - {title}.info.json"
    log(f"  Скачивание: {filename}")

    def _build_tail(target_url: str) -> list[str]:
        t = ["-f", "bestaudio/best", "-x", "--audio-format", out_format,
             "--audio-quality", "0", "--write-info-json", "-o", out_tmpl, "--no-playlist", target_url]
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
            pop_ytdlp_info(info_path)
            log_err(f"  Не удалось скачать: {(proc.stderr or '').strip()[-200:]}")
            return False
    yt_info = pop_ytdlp_info(info_path)

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


    fp, dur = compute_fingerprint(downloaded)
    if fp:
        for existing in index.active_entries():
            if existing.fingerprint and existing.fingerprint == fp:
                log_warn(f"  Дубль по звуку ({Path(existing.path).name}) -> в _duplicates")
                dup_dir = music_dir / cfg.get("DUPLICATES_SUBDIR", "_duplicates")
                _move_to_duplicates(downloaded, dup_dir, None, index, dry_run=False)
                return True

    lookup_artist = clean_artist(track.get("artist", ""))
    lookup_title = clean_title(track.get("title", ""))
    real_dur = dur or read_audio_duration(downloaded) or float(track.get("duration") or 0)
    meta = fetch_full_metadata(lookup_artist, lookup_title, real_dur, fingerprint=fp, cfg=cfg)
    meta = merge_ytmusic_meta(yt_info, meta)
    album = (meta or {}).get("album") or ""
    year = (meta or {}).get("year") or ""
    yt_cover = ytmusic_cover_url(track)
    if (meta or {}).get("_album_from_yt"):
        cover_urls = [yt_cover]
    else:
        cover_urls = [(meta or {}).get("cover_url") or "", yt_cover]
    cover = None
    for cover_url in cover_urls:
        if not cover_url:
            continue
        img = download_image(cover_url)
        if img and len(img[0]) > 2000:
            cover = img
            break
    if write_tags(downloaded, lookup_artist or "Unknown Artist", lookup_title, album, year, cover):
        log_ok(f"  Готово: {album or 'сингл'} {year}".strip())
    if meta:
        extra_fields = write_full_tags(downloaded, meta, only_missing=True)
        if extra_fields:
            log(f"  Теги: {', '.join(extra_fields)}")

    if not has_cover(downloaded):
        log_warn("  Обложки нет, ищу в других источниках...")
        extra = find_cover(lookup_artist, lookup_title, real_dur, extra_urls=[track.get("thumbnail") or ""])
        if extra and embed_cover(downloaded, extra):
            log_ok("  Обложка добавлена из резервного источника.")
        else:
            log_warn("  Обложку найти не удалось.")
    index_single_file(downloaded, index, api_key="", dry_run=False)
    return True


def check_playlist_updates(cfg: dict[str, Any]) -> tuple[int, int]:

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
        if not (stop_event and stop_event.is_set()):
            run_cover_check(cfg, stop_event, progress_cb, dry_run=dry_run, index=index)
            if not dry_run:
                index.save()
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

    if not (stop_event and stop_event.is_set()):
        run_cover_check(cfg, stop_event, progress_cb, dry_run=dry_run, index=index)
        if not dry_run:
            index.save()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Синхронизация плейлиста YouTube Music.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    return run_sync(load_config(), dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
