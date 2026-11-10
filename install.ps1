<#
    litca-ru — установка локальной нейросети (Ollama + Gemma 4) на Windows 10/11.

    Что делает скрипт:
      1. Проверяет Windows, объём RAM и свободное место на диске.
      2. Ставит Ollama (если ещё не установлена) и проверяет подпись установщика.
      3. Включает локальный режим (облачные модели и поиск в интернете отключаются).
      4. Задаёт размер контекста 8192 токена (по умолчанию у Ollama 4096).
      5. Скачивает модель.
      6. Делает пробный запрос на русском и замеряет скорость (токенов в секунду).

    Запуск:
      install.cmd                                    (двойной клик, проще всего)
      powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Lite
      powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Model "gemma4:12b"

    Скрипт ничего не ломает: всё ставится в профиль пользователя, права
    администратора не нужны, удаляется через «Установка и удаление программ».
#>
[CmdletBinding()]
param(
    # Основная модель. По умолчанию 4B-класс — разумный компромисс для CPU.
    [string]$Model = 'gemma4:e4b',

    # Размер контекстного окна в токенах (записывается в переменную окружения
    # OLLAMA_CONTEXT_LENGTH для текущего пользователя).
    [int]$ContextLength = 8192,

    # Лёгкий режим: модель в 2 раза меньше и примерно в 2 раза быстрее,
    # но заметно слабее на сложных текстах.
    [switch]$Lite,

    # Не скачивать модель (например, если она уже есть или нет интернета).
    [switch]$SkipPull,

    # Оставить облачные функции Ollama включёнными.
    [switch]$KeepCloud
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Windows PowerShell 5.1 по умолчанию может пробовать TLS 1.0 — серверы это отвергают.
try {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11
} catch {
    # Старые сборки .NET не знают Tls12 — идём дальше, HTTPS всё равно обычно работает.
}

$ApiBase = 'http://127.0.0.1:11434'
$ExitCode = 0

if ($Lite -and -not $PSBoundParameters.ContainsKey('Model')) {
    $Model = 'gemma4:e2b'
}

# ---------------------------------------------------------------- вспомогательное

function Write-Title([string]$Text) {
    Write-Host ''
    Write-Host ('=' * 68) -ForegroundColor DarkGray
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host ('=' * 68) -ForegroundColor DarkGray
}

function Write-Step([string]$Text) { Write-Host "`n>> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "   [ok] $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "   [!]  $Text" -ForegroundColor Yellow }
function Write-Info([string]$Text) { Write-Host "        $Text" -ForegroundColor Gray }

function Get-FreeSpaceGB([string]$DriveLetter) {
    try {
        $disk = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$DriveLetter':"
        if ($disk) { return [math]::Round($disk.FreeSpace / (1GB), 1) }
    } catch { }
    return -1
}

function Get-OllamaExe {
    $candidates = @()

    $cmd = Get-Command -Name 'ollama.exe' -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { $candidates += $cmd.Source }

    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe')
        $candidates += (Join-Path $env:LOCALAPPDATA 'Ollama\ollama.exe')
    }
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
    }

    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    return $null
}

function Add-ToSessionPath([string]$Directory) {
    if (-not $Directory) { return }
    $parts = $env:PATH -split ';'
    foreach ($p in $parts) {
        if ($p -and ($p.TrimEnd('\') -ieq $Directory.TrimEnd('\'))) { return }
    }
    $env:PATH = $Directory + ';' + $env:PATH
}

function Get-RemoteFile([string]$Url, [string]$Destination) {
    # curl.exe есть в Windows 10 начиная с 1803 и качает большие файлы заметно
    # надёжнее, чем Invoke-WebRequest (тот держит всё в памяти и молчит).
    $curl = $null
    if ($env:SystemRoot) {
        $candidate = Join-Path $env:SystemRoot 'System32\curl.exe'
        if (Test-Path -LiteralPath $candidate) { $curl = $candidate }
    }

    if ($curl) {
        & $curl --fail --location --retry 3 --retry-delay 2 --output $Destination $Url
        if ($LASTEXITCODE -eq 0) { return }
        throw "Не удалось скачать файл (curl, код $LASTEXITCODE): $Url"
    }

    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
}

function Wait-OllamaApi([int]$TimeoutSeconds = 180) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $version = Invoke-RestMethod -Uri "$ApiBase/api/version" -TimeoutSec 3 -UseBasicParsing
            if ($version.version) { return $version.version }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    return $null
}

function Stop-OllamaProcesses {
    $running = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -eq 'ollama app' -or $_.Name -eq 'ollama' -or $_.Name -eq 'llama-server'
    }
    foreach ($proc in $running) {
        try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop } catch { }
    }
    Start-Sleep -Seconds 2
}

function Start-OllamaApp([string]$ExePath) {
    $appExe = Join-Path (Split-Path -Parent $ExePath) 'ollama app.exe'
    if (Test-Path -LiteralPath $appExe) {
        Start-Process -FilePath $appExe -ArgumentList '--hide' -WindowStyle Hidden | Out-Null
    } else {
        Start-Process -FilePath $ExePath -ArgumentList 'serve' -WindowStyle Hidden | Out-Null
    }
}

function Set-ServerJson([string]$Key, $Value) {
    # ВАЖНО: файл пишется без BOM и без UTF-16. Если PowerShell 5.1 пишет через
    # Out-File или ">", получается UTF-16/BOM, и Ollama молча игнорирует настройку.
    $dir = Join-Path $env:USERPROFILE '.ollama'
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $path = Join-Path $dir 'server.json'

    $config = @{}
    if (Test-Path -LiteralPath $path) {
        try {
            $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
            if ($raw -and $raw.Trim().Length -gt 0) {
                $existing = $raw | ConvertFrom-Json
                foreach ($prop in $existing.PSObject.Properties) {
                    $config[$prop.Name] = $prop.Value
                }
            }
        } catch {
            Write-Warn "Файл server.json был повреждён — перезаписываю."
            $config = @{}
        }
    }

    $config[$Key] = $Value
    $json = ($config | ConvertTo-Json -Depth 5) + "`r`n"

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $json, $utf8NoBom)
    Write-Ok "Записан $path"
}

function Invoke-ChatRequest([string]$Prompt, [int]$TimeoutSeconds = 600) {
    $payload = @{
        model    = $Model
        messages = @(@{ role = 'user'; content = $Prompt })
        stream   = $false
        think    = $false
        options  = @{ temperature = 1.0; top_p = 0.95; top_k = 64 }
    } | ConvertTo-Json -Depth 6

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
    return Invoke-RestMethod -Uri "$ApiBase/api/chat" -Method Post `
        -ContentType 'application/json; charset=utf-8' -Body $bytes -TimeoutSec $TimeoutSeconds
}

# ---------------------------------------------------------------- 1. проверки

Write-Title 'Локальная нейросеть на ноутбуке: установка'

Write-Step 'Проверяю компьютер'

# В PowerShell 5.1 переменной $IsWindows нет, поэтому проверяем версию.
if ($PSVersionTable.PSVersion.Major -ge 6 -and -not $IsWindows) {
    Write-Host 'Этот скрипт предназначен для Windows.' -ForegroundColor Red
    exit 1
}

$build = [Environment]::OSVersion.Version.Build
if ($build -lt 19045) {
    Write-Warn "Сборка Windows $build старше 22H2 (19045). Ollama может не запуститься."
} else {
    Write-Ok "Windows сборка $build"
}

$ramGB = 0
try {
    $ramGB = [math]::Round((Get-CimInstance -ClassName Win32_ComputerSystem).TotalPhysicalMemory / (1GB))
} catch { }
if ($ramGB -ge 15) {
    Write-Ok "Оперативная память: ~$ramGB ГБ"
} elseif ($ramGB -gt 0) {
    Write-Warn "Оперативная память: ~$ramGB ГБ — для модели 4B это мало, лучше -Lite"
} else {
    Write-Warn 'Не удалось определить объём RAM'
}

$freeGB = Get-FreeSpaceGB 'C'
if ($freeGB -ge 0) {
    if ($freeGB -lt 12) {
        Write-Warn "Свободно на диске C: $freeGB ГБ — модели нужно около 7-8 ГБ плюс запас"
    } else {
        Write-Ok "Свободно на диске C: $freeGB ГБ"
    }
}

# ---------------------------------------------------------------- 2. Ollama

Write-Step 'Ollama'

$ollamaExe = Get-OllamaExe
if ($ollamaExe) {
    Write-Ok "Уже установлена: $ollamaExe"
} else {
    Write-Info 'Скачиваю установщик (около 1.9 ГБ, это займёт время)...'

    $setup = Join-Path $env:TEMP 'OllamaSetup.exe'
    Get-RemoteFile -Url 'https://ollama.com/download/OllamaSetup.exe' -Destination $setup

    $sizeGB = [math]::Round((Get-Item -LiteralPath $setup).Length / (1GB), 2)
    Write-Ok "Скачан установщик ($sizeGB ГБ)"

    Write-Info 'Проверяю цифровую подпись...'
    $signature = Get-AuthenticodeSignature -FilePath $setup
    if ($signature.Status -ne 'Valid') {
        throw "Подпись установщика недействительна ($($signature.Status)). Установка остановлена."
    }
    $subject = ''
    if ($signature.SignerCertificate) { $subject = $signature.SignerCertificate.Subject }
    if ($subject -notmatch 'O=Ollama') {
        throw "Установщик подписан не Ollama ($subject). Установка остановлена."
    }
    Write-Ok 'Подпись верна, издатель — Ollama'

    Write-Info 'Тихая установка (окно может мигнуть)...'
    # Start-Process -Wait тут не годится: установщик запускает ещё и приложение
    # Ollama, и -Wait ждал бы его вечно. Ждём только сам установщик.
    $installer = Start-Process -FilePath $setup -ArgumentList '/VERYSILENT /NORESTART /SUPPRESSMSGBOXES' -PassThru
    $installer.WaitForExit()
    if ($installer.ExitCode -ne 0) {
        throw "Установщик завершился с кодом $($installer.ExitCode)"
    }

    # Официальный скрипт Ollama создаёт этот файл, чтобы приложение стартовало скрыто.
    $marker = Join-Path $env:LOCALAPPDATA 'Ollama\upgraded'
    $markerDir = Split-Path -Parent $marker
    if (-not (Test-Path -LiteralPath $markerDir)) {
        New-Item -ItemType Directory -Path $markerDir -Force | Out-Null
    }
    if (-not (Test-Path -LiteralPath $marker)) {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($marker, '', $utf8NoBom)
    }

    $ollamaExe = Get-OllamaExe
    if (-not $ollamaExe) {
        throw 'Установка прошла, но ollama.exe не найден. Перезапустите компьютер и запустите скрипт снова.'
    }
    Write-Ok "Установлена: $ollamaExe"
}

Add-ToSessionPath (Split-Path -Parent $ollamaExe)

# ---------------------------------------------------------------- 3. локальный режим

Write-Step 'Настройки'

if ($KeepCloud) {
    Write-Warn 'Облачные функции Ollama оставлены включёнными (флаг -KeepCloud).'
} else {
    try {
        Set-ServerJson -Key 'disable_ollama_cloud' -Value $true
        Write-Info 'Облачные модели, веб-поиск и запросы «рекомендаций» к ollama.com отключены.'
    } catch {
        Write-Warn "Не удалось записать server.json: $($_.Exception.Message)"
        Write-Info 'Можно отключить облако вручную: Настройки приложения -> переключатель Cloud.'
    }
}

if ($ContextLength -ge 2048) {
    try {
        [Environment]::SetEnvironmentVariable('OLLAMA_CONTEXT_LENGTH', "$ContextLength", 'User')
        $env:OLLAMA_CONTEXT_LENGTH = "$ContextLength"
        Write-Ok "Контекст: $ContextLength токенов (переменная OLLAMA_CONTEXT_LENGTH)"
        Write-Info 'В приложении это же значение задаётся ползунком Settings -> Context length.'
    } catch {
        Write-Warn "Не удалось задать размер контекста: $($_.Exception.Message)"
    }
} else {
    Write-Warn "Слишком маленький контекст ($ContextLength) — оставляю значение по умолчанию."
}

# Перезапуск, чтобы сервер подхватил новые настройки.
Write-Info 'Перезапускаю сервер Ollama, чтобы применить настройки...'

$wasRunning = $false
try {
    Invoke-RestMethod -Uri "$ApiBase/api/version" -TimeoutSec 3 -UseBasicParsing | Out-Null
    $wasRunning = $true
} catch { }

$version = $null
if ($wasRunning) {
    Stop-OllamaProcesses
    $version = Wait-OllamaApi -TimeoutSeconds 45
    if (-not $version) {
        Write-Info 'Автоперезапуск не сработал — запускаю приложение Ollama вручную.'
        Start-OllamaApp -ExePath $ollamaExe
        $version = Wait-OllamaApi -TimeoutSeconds 120
    }
} else {
    Start-OllamaApp -ExePath $ollamaExe
    $version = Wait-OllamaApi -TimeoutSeconds 120
}

if (-not $version) {
    throw "Сервер Ollama не отвечает на $ApiBase. Откройте приложение Ollama вручную и запустите скрипт снова."
}
Write-Ok "Сервер работает, версия $version"

$cloudState = 'неизвестно'
try {
    $cloud = Invoke-RestMethod -Uri "$ApiBase/api/v1/cloud" -TimeoutSec 5 -UseBasicParsing
    if ($cloud.disabled) { $cloudState = 'выключено' } else { $cloudState = 'включено' }
} catch { }
Write-Info "Облако: $cloudState"

# ---------------------------------------------------------------- 4. модель

Write-Step "Модель $Model"

if ($SkipPull) {
    Write-Warn 'Скачивание пропущено (-SkipPull).'
} else {
    Write-Info 'Если модель уже есть, Ollama проверит её и ничего не будет качать заново.'
    & $ollamaExe pull $Model
    if ($LASTEXITCODE -ne 0) {
        throw "ollama pull $Model завершился с кодом $LASTEXITCODE"
    }
    Write-Ok 'Модель готова'
}

# ---------------------------------------------------------------- 5. проверка

Write-Step 'Пробный запрос на русском (заодно замер скорости)'

$replyText = ''
try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-ChatRequest -Prompt 'Напиши три коротких предложения о том, зачем нужен локальный ИИ на ноутбуке.'
    $sw.Stop()

    if ($response.message.content) { $replyText = $response.message.content.Trim() }

    Write-Host ''
    Write-Host '   --- ответ модели ---' -ForegroundColor DarkGray
    Write-Host "   $replyText" -ForegroundColor White
    Write-Host '   --------------------' -ForegroundColor DarkGray

    $genRate = 0
    if ($response.eval_count -and $response.eval_duration -and $response.eval_duration -gt 0) {
        $genRate = [math]::Round($response.eval_count / ($response.eval_duration / 1e9), 1)
    }
    if ($genRate -gt 0) {
        Write-Ok ("Скорость: {0} токенов/с ({1} токенов за {2:N1} с)" -f $genRate, $response.eval_count, ($response.eval_duration / 1e9))
        Write-Info ("Время до первого токена: {0:N1} с" -f ($response.prompt_eval_duration / 1e9))

        if ($genRate -lt 6) {
            Write-Warn 'Медленно для повседневной работы. Попробуйте лёгкую модель:'
            Write-Info 'install.cmd -Lite   (gemma4:e2b — примерно вдвое быстрее)'
        } elseif ($genRate -lt 12) {
            Write-Info 'Обычная скорость для ноутбука без видеокарты. На длинных ответах придётся подождать.'
        } else {
            Write-Ok 'Хорошая скорость для работы без видеокарты.'
        }
    } else {
        Write-Info ("Весь ответ занял {0:N1} с" -f $sw.Elapsed.TotalSeconds)
    }
} catch {
    Write-Warn "Пробный запрос не удался: $($_.Exception.Message)"
    Write-Info 'Это не мешает установке — попробуйте открыть приложение Ollama вручную.'
    $ExitCode = 2
}

Write-Step 'Что сейчас загружено в память'
& $ollamaExe ps

# ---------------------------------------------------------------- 6. итог

$appExe = Join-Path (Split-Path -Parent $ollamaExe) 'ollama app.exe'

Write-Title 'Готово'

Write-Host ''
Write-Host '  Как пользоваться:' -ForegroundColor Cyan
Write-Host '   - Откройте Ollama из меню «Пуск» — это обычное окно чата.'
Write-Host "   - Модель в списке: $Model"
Write-Host '   - Кнопка Think (размышления) по умолчанию выключена — так быстрее.'
Write-Host '     Включайте её только для сложных задач: ответ будет точнее, но дольше.'
Write-Host ''
Write-Host '  Из командной строки:' -ForegroundColor Cyan
Write-Host "   ollama run $Model --think=false"
Write-Host ''
Write-Host '  Офлайн:' -ForegroundColor Cyan
Write-Host '   После первой загрузки модели интернет не нужен.'
Write-Host '   Модели лежат в: ' -NoNewline
Write-Host "$env:USERPROFILE\.ollama\models"
Write-Host '   Логи при проблемах: ' -NoNewline
Write-Host "$env:LOCALAPPDATA\Ollama\server.log"
Write-Host ''
Write-Host '  Удалить всё:' -ForegroundColor Cyan
Write-Host '   «Параметры -> Приложения» (Ollama), затем удалите папку ~\.ollama'
Write-Host ''

exit $ExitCode
