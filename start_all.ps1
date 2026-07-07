# Wellness Bot Full Startup Script
# Starts workers, admin API, and runtime polling mode in one command.

Write-Host "`nWELLNESS BOT FULL STARTUP`n" -ForegroundColor Cyan

$env:PYTHONPATH = $PWD
$legacyTempRoot = Join-Path $PSScriptRoot "wellness_data\tmp"

function Test-WellnessLegacyRepoTempRoot {
    param([string]$PathToCheck)

    if (-not $PathToCheck) {
        return $false
    }

    try {
        $resolved = [System.IO.Path]::GetFullPath($PathToCheck).TrimEnd('\')
        $legacy = [System.IO.Path]::GetFullPath($legacyTempRoot).TrimEnd('\')
        return $resolved -eq $legacy -or $resolved.StartsWith("$legacy\", [System.StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Get-WellnessTempRoot {
    if ($env:WELLNESS_TEMP_DIR) {
        if (Test-WellnessLegacyRepoTempRoot $env:WELLNESS_TEMP_DIR) {
            Write-Host "Ignoring legacy repo temp directory: $($env:WELLNESS_TEMP_DIR)" -ForegroundColor DarkYellow
        } else {
            return $env:WELLNESS_TEMP_DIR
        }
    }
    if ($env:LOCALAPPDATA) {
        return (Join-Path $env:LOCALAPPDATA "wellness-bot\tmp")
    }
    return (Join-Path $HOME ".cache\wellness-bot\tmp")
}

$adminPort = if ($env:ADMIN_PORT) { $env:ADMIN_PORT } else { "8110" }
$jobNames = @("OutboxSender", "Embeddings", "Sentiments", "AdminServer")
$tempRoot = Get-WellnessTempRoot

New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
$env:WELLNESS_TEMP_DIR = $tempRoot
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:TMPDIR = $tempRoot

Write-Host "Using temp directory: $tempRoot" -ForegroundColor DarkGray

function Start-ServiceJob {
    param(
        [string]$Name,
        [string]$Module
    )
    Start-Job -Name $Name -ScriptBlock {
        param($dir, $moduleName, $tempDir)
        Set-Location $dir
        $env:PYTHONPATH = $dir
        $env:WELLNESS_TEMP_DIR = $tempDir
        $env:TEMP = $tempDir
        $env:TMP = $tempDir
        $env:TMPDIR = $tempDir
        python -m $moduleName 2>&1
    } -ArgumentList $PWD, $Module, $tempRoot | Out-Null
}

function Stop-ServiceJobs {
    foreach ($name in $jobNames) {
        $job = Get-Job -Name $name -ErrorAction SilentlyContinue
        if ($job) {
            $job | Stop-Job -ErrorAction SilentlyContinue
            $job | Remove-Job -ErrorAction SilentlyContinue
        }
    }
}

function Wait-AdminServer {
    param(
        [string]$Port,
        [int]$TimeoutSeconds = 15
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $job = Get-Job -Name "AdminServer" -ErrorAction SilentlyContinue
        if (-not $job) {
            return $false
        }
        if ($job.State -in @("Completed", "Failed", "Stopped")) {
            return $false
        }

        try {
            $response = Invoke-WebRequest `
                -UseBasicParsing `
                -Uri "http://127.0.0.1:$Port/openapi.json" `
                -TimeoutSec 2 `
                -ErrorAction Stop
            if ($response.StatusCode -eq 200) {
                return $true
            }
        } catch {
        }

        Start-Sleep -Milliseconds 400
    }

    return $false
}

# Avoid duplicate-name errors if stale jobs exist in this shell.
foreach ($name in $jobNames) {
    $existing = Get-Job -Name $name -ErrorAction SilentlyContinue
    if ($existing) {
        $existing | Stop-Job -ErrorAction SilentlyContinue
        $existing | Remove-Job -ErrorAction SilentlyContinue
    }
}

Write-Host "Checking Ollama..." -NoNewline
try {
    $null = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:11434/api/tags" -TimeoutSec 2
    Write-Host " OK" -ForegroundColor Green
} catch {
    Write-Host " FAILED (Ollama not running)" -ForegroundColor Red
    exit 1
}

Write-Host "`nStarting background services...`n" -ForegroundColor Yellow
# Note: image generation runs in the separate DungeonMaster SDXL server
# (`python dm_imagegen.py --serve`, :8500) — start it there when you want images.

Write-Host "  [1/4] Outbox Sender..." -NoNewline
Start-ServiceJob -Name "OutboxSender" -Module "app.workers.outbox_sender"
Start-Sleep 1
Write-Host " OK" -ForegroundColor Green

Write-Host "  [2/4] Embeddings Worker..." -NoNewline
Start-ServiceJob -Name "Embeddings" -Module "app.workers.embeddings"
Start-Sleep 1
Write-Host " OK" -ForegroundColor Green

Write-Host "  [3/4] Sentiments Worker..." -NoNewline
Start-ServiceJob -Name "Sentiments" -Module "app.workers.sentiments"
Start-Sleep 1
Write-Host " OK" -ForegroundColor Green

Write-Host "  [4/4] Admin Server (127.0.0.1:$adminPort)..." -NoNewline
# Release any stale process holding the admin port before starting a fresh one.
$stalePid = (Get-NetTCPConnection -LocalPort $adminPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
if ($stalePid) {
    Write-Host " (clearing stale process $stalePid on :$adminPort)" -ForegroundColor DarkGray -NoNewline
    Stop-Process -Id $stalePid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 600
}
Start-Job -Name "AdminServer" -ScriptBlock {
    param($dir, $port, $tempDir)
    Set-Location $dir
    $env:PYTHONPATH = $dir
    $env:WELLNESS_TEMP_DIR = $tempDir
    $env:TEMP = $tempDir
    $env:TMP = $tempDir
    $env:TMPDIR = $tempDir
    python -m app.interfaces.admin.server --host 127.0.0.1 --port $port 2>&1
} -ArgumentList $PWD, $adminPort, $tempRoot | Out-Null
if (Wait-AdminServer -Port $adminPort -TimeoutSeconds 120) {
    Write-Host " OK" -ForegroundColor Green
} else {
    Write-Host " FAILED" -ForegroundColor Red
    $job = Get-Job -Name "AdminServer" -ErrorAction SilentlyContinue
    if ($job) {
        Write-Host "    Admin job state: $($job.State)" -ForegroundColor Yellow
        $output = Receive-Job -Name "AdminServer" -Keep -ErrorAction SilentlyContinue
        if ($output) {
            Write-Host "    Recent admin output:" -ForegroundColor Yellow
            $output | Select-Object -Last 30 | ForEach-Object {
                Write-Host "      $_"
            }
        }
    }
    Stop-ServiceJobs
    exit 1
}

Write-Host "`nActive Jobs:" -ForegroundColor Cyan
Get-Job | Format-Table -Property Id, Name, State

Start-Process "http://127.0.0.1:$adminPort/"

Write-Host "`nStarting runtime (polling) in foreground...`n" -ForegroundColor Yellow
Write-Host "Press Ctrl+C to stop runtime + workers + admin.`n"

$runtimeExitCode = 0
try {
    python -u -m app.main_modular
    $runtimeExitCode = $LASTEXITCODE
} finally {
    if ($runtimeExitCode -ne 0) {
        Write-Host "Runtime exited with code $runtimeExitCode." -ForegroundColor Red
    } else {
        Write-Host "Runtime exited cleanly." -ForegroundColor DarkGray
    }
    Write-Host "`nStopping background services..." -ForegroundColor Yellow
    Stop-ServiceJobs
    Write-Host "All services stopped.`n" -ForegroundColor Green
}

exit $runtimeExitCode
