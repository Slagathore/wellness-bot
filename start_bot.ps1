# Wellness Bot Startup Script
# Starts all workers and polling mode

Write-Host "`n🤖 WELLNESS BOT STARTUP SEQUENCE`n" -ForegroundColor Cyan

# Set Python path
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

$tempRoot = Get-WellnessTempRoot
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
$env:WELLNESS_TEMP_DIR = $tempRoot
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:TMPDIR = $tempRoot

Write-Host "Using temp directory: $tempRoot" -ForegroundColor DarkGray

# Check Ollama
Write-Host "Checking Ollama..." -NoNewline
try {
    $null = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -TimeoutSec 2
    Write-Host " ✅" -ForegroundColor Green
} catch {
    Write-Host " ❌ Ollama not running!" -ForegroundColor Red
    exit 1
}

Write-Host "`nStarting workers...`n" -ForegroundColor Yellow

# Start outbox sender
Write-Host "  [1/3] Outbox Sender..." -NoNewline
Start-Job -Name "OutboxSender" -ScriptBlock {
    param($dir, $tempDir)
    Set-Location $dir
    $env:PYTHONPATH = $dir
    $env:WELLNESS_TEMP_DIR = $tempDir
    $env:TEMP = $tempDir
    $env:TMP = $tempDir
    $env:TMPDIR = $tempDir
    python app/workers/outbox_sender.py 2>&1
} -ArgumentList $PWD, $tempRoot | Out-Null
Start-Sleep 1
Write-Host " ✅" -ForegroundColor Green

# Start embeddings worker
Write-Host "  [2/3] Embeddings Worker..." -NoNewline
Start-Job -Name "Embeddings" -ScriptBlock {
    param($dir, $tempDir)
    Set-Location $dir
    $env:PYTHONPATH = $dir
    $env:WELLNESS_TEMP_DIR = $tempDir
    $env:TEMP = $tempDir
    $env:TMP = $tempDir
    $env:TMPDIR = $tempDir
    python app/workers/embeddings.py 2>&1
} -ArgumentList $PWD, $tempRoot | Out-Null
Start-Sleep 1
Write-Host " ✅" -ForegroundColor Green

# Start sentiments worker
Write-Host "  [3/3] Sentiments Worker..." -NoNewline
Start-Job -Name "Sentiments" -ScriptBlock {
    param($dir, $tempDir)
    Set-Location $dir
    $env:PYTHONPATH = $dir
    $env:WELLNESS_TEMP_DIR = $tempDir
    $env:TEMP = $tempDir
    $env:TMP = $tempDir
    $env:TMPDIR = $tempDir
    python app/workers/sentiments.py 2>&1
} -ArgumentList $PWD, $tempRoot | Out-Null
Start-Sleep 1
Write-Host " ✅" -ForegroundColor Green

Write-Host "`n✅ All workers started!`n" -ForegroundColor Green

# Show job status
Write-Host "Active Jobs:" -ForegroundColor Cyan
Get-Job | Format-Table -Property Id, Name, State

Write-Host "`n📱 Starting Modular Runtime (Polling)...`n" -ForegroundColor Yellow
Write-Host "Press Ctrl+C to stop all services`n"

# Run polling in foreground
try {
    python -m app.main_modular
} finally {
    Write-Host "`n`n🛑 Shutting down all workers..." -ForegroundColor Yellow
    Get-Job | Stop-Job
    Get-Job | Remove-Job
    Write-Host "✅ All workers stopped`n" -ForegroundColor Green
}
