# Bootstrap a Windows remote host (dual RTX 4500 Ada) to run scripts/label_photos.py.
# Starts one ollama serve instance per GPU so both cards label in parallel.
# Idempotent: safe to re-run.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_gemma_host.ps1
#
# Env-style overrides:
#   powershell -File scripts\setup_gemma_host.ps1 -Model gemma4:26b -Gpus 0,1

[CmdletBinding()]
param(
    [string]$Model     = "gemma4:31b",
    [int[]] $Gpus      = @(0, 1),
    [int]   $BasePort  = 11434,
    [string]$KeepAlive = "24h"
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

Write-Step "Checking NVIDIA driver"
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Error "nvidia-smi not found. Install the NVIDIA driver for RTX 4500 Ada first."
}
nvidia-smi --query-gpu=index,name,memory.total --format=csv

Write-Step "Checking Python"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "python not found on PATH. Install Python 3.10+ from python.org (tick 'Add to PATH')."
}
python --version

Write-Step "Installing Ollama (skipped if already installed)"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    $installer = Join-Path $env:TEMP "OllamaSetup.exe"
    Write-Host "   downloading OllamaSetup.exe ..."
    Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile $installer -UseBasicParsing
    Write-Host "   running installer (silent) ..."
    Start-Process -FilePath $installer -ArgumentList "/silent" -Wait
    # Add to PATH for this session; installer adds it permanently for new shells.
    $ollamaDir = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
    if (Test-Path $ollamaDir) { $env:PATH = "$ollamaDir;$env:PATH" }
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Write-Error "ollama still not on PATH after install. Open a new PowerShell and re-run, or add '$ollamaDir' to PATH manually."
    }
} else {
    Write-Host "   ollama already installed: $(ollama --version)"
}

Write-Step "Stopping any running Ollama processes"
# Default Windows install runs a tray app ('ollama app.exe') plus a server ('ollama.exe').
# Kill both so we can bind our own port-pinned instances.
Get-Process -Name "ollama","ollama app" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

$logDir = Join-Path $env:TEMP "ollama-logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Write-Step "Starting one ollama serve per GPU"
$hostUrls = @()
for ($i = 0; $i -lt $Gpus.Count; $i++) {
    $gpu  = $Gpus[$i]
    $port = $BasePort + $i
    $log  = Join-Path $logDir "ollama-gpu$gpu.log"
    $url  = "http://127.0.0.1:$port"
    Write-Host "   GPU $gpu -> $url (log: $log)"

    # Launch each instance in a detached PowerShell so its env vars are isolated.
    $inner = @"
`$env:CUDA_VISIBLE_DEVICES = '$gpu'
`$env:OLLAMA_HOST          = '127.0.0.1:$port'
`$env:OLLAMA_KEEP_ALIVE    = '$KeepAlive'
ollama serve *>&1 | Out-File -Encoding utf8 -FilePath '$log'
"@
    Start-Process -FilePath "powershell.exe" `
                  -ArgumentList @("-NoProfile","-WindowStyle","Hidden","-Command",$inner) `
                  -WindowStyle Hidden | Out-Null

    $hostUrls += $url
}

Start-Sleep -Seconds 5

Write-Step "Pulling model: $Model (on each instance)"
foreach ($u in $hostUrls) {
    $hostNoScheme = $u -replace '^http://',''
    Write-Host "   $u"
    $env:OLLAMA_HOST = $hostNoScheme
    ollama pull $Model
}

Write-Step "Installing Python deps"
python -m pip install --upgrade pip
python -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")

Write-Step "Smoke test"
foreach ($u in $hostUrls) {
    $hostNoScheme = $u -replace '^http://',''
    $env:OLLAMA_HOST = $hostNoScheme
    python -c "import ollama, os; h=os.environ['OLLAMA_HOST']; c=ollama.Client(host='http://'+h); print(h,'models =',[m['model'] for m in c.list()['models']])"
}

$joined = $hostUrls -join ','
Write-Host ""
Write-Step "Done. Run the labeler across all configured GPUs with:"
Write-Host "    python scripts\label_photos.py --model $Model --hosts `"$joined`""
Write-Host ""
Write-Host "Or set it once in your shell profile:"
Write-Host "    `$env:OLLAMA_HOSTS = `"$joined`""
Write-Host "    python scripts\label_photos.py --model $Model"
Write-Host ""
Write-Host "Logs: $logDir\ollama-gpu*.log"
