# Запускать из PowerShell:  powershell -ExecutionPolicy Bypass -File C:\ytm_sync\build_release.ps1
$ErrorActionPreference = "Stop"
$root = "C:\ytm_sync"
Set-Location $root
$utf8 = New-Object System.Text.UTF8Encoding($false)

# --- 1. Служебные файлы ---------------------------------------------------
$configExample = @'
{
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
  "AUTO_CHECK_ON_START": true,
  "AUTO_DOWNLOAD_ON_START": false
}
'@
$gitignore = @'
# Личные данные (живут в %APPDATA%\YTMusicSync, но на всякий случай)
config.json
library_index.json
downloaded_ids.txt
sync.log
cookies.txt
*.log

# Сборка и окружение
build/
dist/
release/
*.spec
__pycache__/
*.pyc
.venv/
venv/

# Внешние бинарники (скачиваются отдельно)
bin/
*.exe
'@
$requirements = @'
customtkinter
requests
mutagen
rapidfuzz
transliterate
yt-dlp
pyinstaller
'@
[IO.File]::WriteAllText("$root\config.example.json", $configExample, $utf8)
[IO.File]::WriteAllText("$root\.gitignore", $gitignore, $utf8)
[IO.File]::WriteAllText("$root\requirements.txt", $requirements, $utf8)
Write-Host "[OK] config.example.json, .gitignore, requirements.txt созданы" -ForegroundColor Green

# --- 2. Зависимости и сборка ----------------------------------------------
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Remove-Item -Recurse -Force "$root\build", "$root\release", "$root\YTMusicSync.spec" -ErrorAction SilentlyContinue

python -m PyInstaller --noconfirm --onedir --windowed --name YTMusicSync --distpath release `
    --collect-all customtkinter --collect-all yt_dlp --collect-all transliterate gui.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }

# --- 3. Копирование bin ---------------------------------------------------
$binSrc = "$root\bin"
$binDst = "$root\release\YTMusicSync\bin"
if (Test-Path $binSrc) {
    New-Item -ItemType Directory -Force -Path $binDst | Out-Null
    Copy-Item "$binSrc\*" $binDst -Recurse -Force
    foreach ($f in "ffmpeg.exe", "ffprobe.exe", "fpcalc.exe") {
        if (Test-Path "$binDst\$f") { Write-Host "[OK] bin\$f" -ForegroundColor Green }
        else { Write-Host "[!!] bin\$f не найден" -ForegroundColor Yellow }
    }
} else {
    Write-Host "[!!] Папка $binSrc не найдена - ffmpeg/ffprobe/fpcalc не скопированы" -ForegroundColor Yellow
}

# Убираем артефакты сборки, содержащие абсолютные пути
Remove-Item -Recurse -Force "$root\build", "$root\YTMusicSync.spec" -ErrorAction SilentlyContinue

# --- 4. Проверка на личные данные -----------------------------------------
Write-Host "`n=== Проверка личных данных ===" -ForegroundColor Cyan
$problems = 0

foreach ($n in "config.json", "library_index.json", "downloaded_ids.txt", "sync.log", "cookies.txt") {
    $hits = Get-ChildItem $root -Recurse -File -Filter $n -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch '\\(\.venv|venv)\\' }
    foreach ($h in $hits) { Write-Host "[!!] Личный файл: $($h.FullName)" -ForegroundColor Red; $problems++ }
}

$patterns = @(
    'list=(PL|OLAK|RDCLAK|LM)[A-Za-z0-9_-]{6,}',
    '(?i)C:\\Users\\[^\\"'' ]+',
    '(?i)\\AppData\\(Roaming|Local)\\(?!YTMusicSync)',
    [regex]::Escape($env:USERNAME),
    '(?i)(api[_-]?key|client)\s*[:=]\s*["''][A-Za-z0-9]{8,}["'']'
)
$sources = Get-ChildItem $root -Recurse -File -Include *.py, *.json, *.txt, *.md, *.ps1, *.cfg |
           Where-Object { $_.FullName -notmatch '\\(release|build|dist|\.venv|venv|__pycache__|bin)\\' -and
                          $_.Name -ne 'build_release.ps1' -and $_.Name -notlike 'prompt*' }
foreach ($file in $sources) {
    foreach ($p in $patterns) {
        $m = Select-String -Path $file.FullName -Pattern $p -ErrorAction SilentlyContinue
        foreach ($x in $m) {
            Write-Host "[!!] $($file.Name):$($x.LineNumber): $($x.Line.Trim())" -ForegroundColor Red
            $problems++
        }
    }
}

if ($problems -eq 0) {
    Write-Host "[OK] Личных данных не найдено. Папка готова к загрузке на GitHub." -ForegroundColor Green
} else {
    Write-Host "[!!] Найдено проблем: $problems. Удалите/замените перед публикацией." -ForegroundColor Red
}
Write-Host "`nГотовая программа: $root\release\YTMusicSync\YTMusicSync.exe" -ForegroundColor Cyan
