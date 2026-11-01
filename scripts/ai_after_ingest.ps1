# 等 D:\视频记录 的导入跑完，然后自动做 AI 人脸识别（+ 人物聚类）。
#
# 用户要求：只跑人脸识别，打标签和视频摘要都不要。
# 顺序：faces → cluster。cluster 属于人脸识别的一部分（不聚类网页「人物」页就没有分组）。
#
# 设计成"等待型"任务：现在启动，每 30 秒看一眼还有没有 ingest 进程；
# 导入结束（无论 exit 0/1）后开始 AI，日志写 D:\photo\derived\logs\ai-chain-*.log。
# 全部跑完后自动关机（60 秒倒计时，期间可 shutdown /a 取消）。

$ErrorActionPreference = 'Continue'
$repo = 'C:\Users\zhang\Documents\Code\photo-vault'
$logDir = 'D:\photo\derived\logs'
$uv = 'C:\Users\zhang\.local\bin\uv.exe'
$log = Join-Path $logDir ("ai-chain-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

function Log([string]$m) {
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $m
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}
function Run([string]$label, [string[]]$argv) {
    Log "RUN  $label"
    & $uv @argv *>> $log
    $code = $LASTEXITCODE
    Log "DONE $label  exit=$code"
    return $code
}

Log "===== AI chain: waiting for ingest ====="
$idle = 0
while ($true) {
    $procs = @(Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match 'vault\.cli ingest' })
    if ($procs.Count -eq 0) {
        $idle++
        if ($idle -ge 2) { break }
    } else {
        $idle = 0
    }
    Start-Sleep -Seconds 30
}
Log 'ingest process gone'
Start-Sleep -Seconds 15
if (Test-Path (Join-Path $logDir 'ingest-records.exit')) {
    $exit = (Get-Content (Join-Path $logDir 'ingest-records.exit')) -join ' | '
    Log "ingest exit: $exit"
} else {
    Log 'ingest exit file missing（可能被中途关掉，按当前库继续跑 AI）'
}

Set-Location $repo
$env:DATABASE_URL = 'postgresql://vault:vault@localhost:5433/vault'
$env:VAULT_ROOT = 'D:\photo\vault'
$env:DERIVED_ROOT = 'D:\photo\derived'

$c1 = Run 'enqueue faces' @('run', '--group', 'ml', 'python', '-m', 'ml.enqueue', '--kind', 'faces')
$c2 = Run 'worker faces'  @('run', '--group', 'ml', 'python', '-m', 'ml.worker', '--task', 'faces', '--once')
$c3 = Run 'cluster faces' @('run', '--group', 'ml', 'python', '-m', 'ml.cluster')

$summary = @(
    "ended={0}" -f (Get-Date -Format 's')
    "enqueue_faces=$c1"
    "worker_faces=$c2"
    "cluster=$c3"
) -join "`n"
Set-Content -Path (Join-Path $logDir 'ai-chain.exit') -Value $summary -Encoding utf8
Log '===== AI chain DONE ====='
Log $summary

# 全部跑完自动关机（60 秒倒计时，期间可以 shutdown /a 取消）
Log 'stop docker compose (graceful db shutdown)'
Set-Location $repo
docker compose stop 2>&1 | ForEach-Object { Log "  $_" }
Log 'shutdown in 60s (run "shutdown /a" to cancel)'
& shutdown.exe /s /t 60 /f /c "PhotoVault: 人脸识别完成，60 秒后自动关机"
Log ("shutdown requested, exit=$LASTEXITCODE")
