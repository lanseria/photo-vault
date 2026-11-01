# 把 E:\photo 整体搬到 D:\photo，校验后切换 app 与导入任务。
#
# 为什么搬：vault 和素材源都在 D: 上以后，ingest 可以用 --mode link 建硬链接，
# 新素材入库只读一遍算哈希、不再拷贝（同盘 O(1)），省一半 IO 和一份空间。
#
# 这个脚本可以重复执行：robocopy 会跳过已经完整复制过的文件。
# 它 **不删除** E:\photo —— 切换成功后再由人确认清理。
# 任何一步失败（校验/健康检查）都会自动把 app 切回 E:，不会把站点留在半挂状态。

$ErrorActionPreference = 'Stop'
$repo = 'C:\Users\zhang\Documents\Code\photo-vault'
$src = 'E:\photo'
$dst = 'D:\photo'
$log = Join-Path $env:TEMP 'migrate_to_d.log'
$envFile = Join-Path $repo '.env'

function Log([string]$m) {
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $m
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}
function Tree([string]$p) {
    $m = Get-ChildItem $p -Recurse -File -Force -ErrorAction SilentlyContinue |
        Measure-Object Length -Sum
    [pscustomobject]@{ Count = [int]$m.Count; Bytes = [long]$m.Sum }
}
function Copy-All {
    robocopy $src $dst /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /MT:1 /NP /NDL /LOG+:$log
    if ($LASTEXITCODE -ge 8) { throw "robocopy exit $LASTEXITCODE" }
}
function Set-EnvRoot([string]$root) {
    $text = Get-Content $envFile -Raw
    $text = $text -replace '(?m)^HOST_ROOT=.*$', "HOST_ROOT=$root"
    $text = $text -replace '(?m)^MEDIA_HOST_ROOT=.*$', "MEDIA_HOST_ROOT=$root\media"
    Set-Content -Path $envFile -Value $text -NoNewline -Encoding utf8
}

Log "===== migrate $src -> $dst ====="
if (-not (Test-Path $src)) { Log 'FAILED: source missing'; exit 1 }
New-Item -ItemType Directory -Force -Path $dst | Out-Null

Log 'pass 1: copy (resumable)'
Copy-All
$a = Tree "$src\vault"
$b = Tree "$dst\vault"
Log ("pass 1 done: vault src {0} files / {1:N1} GB, dst {2} files / {3:N1} GB" -f
    $a.Count, ($a.Bytes / 1GB), $b.Count, ($b.Bytes / 1GB))

Push-Location $repo
$appOnD = $false
try {
    Log 'stop app (consistent second pass)'
    docker compose stop app 2>&1 | ForEach-Object { Log "  $_" }

    Log 'pass 2: sync while app stopped'
    Copy-All

    foreach ($d in @('vault', 'derived', 'source')) {
        $a = Tree (Join-Path $src $d)
        $b = Tree (Join-Path $dst $d)
        Log ("verify {0}: src {1} files / {2} B, dst {3} files / {4} B" -f
            $d, $a.Count, $a.Bytes, $b.Count, $b.Bytes)
        if ($a.Count -ne $b.Count -or $a.Bytes -ne $b.Bytes) {
            throw "size mismatch in $d"
        }
    }

    Log 'switch .env to D:'
    Set-EnvRoot 'D:\photo'

    Log 'start app on D:'
    docker compose up -d app 2>&1 | ForEach-Object { Log "  $_" }

    $ok = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Seconds 3
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://localhost:8081/api/stats'
            if ($r.StatusCode -eq 200) {
                $ok = $true
                Log ('health ok: ' + $r.Content.Substring(0, [Math]::Min(140, $r.Content.Length)))
                break
            }
        } catch { }
    }
    if (-not $ok) { throw 'app unhealthy on D:' }
    $appOnD = $true

    # 导入包装脚本切到 D:，并改用 link 模式：同盘硬链接，不再拷贝
    $wrapper = Join-Path $env:TEMP 'run_ingest_records.ps1'
    $body = @'
$ErrorActionPreference = 'Continue'
$env:DATABASE_URL = 'postgresql://vault:vault@localhost:5433/vault'
$env:VAULT_ROOT = 'D:\photo\vault'
$env:DERIVED_ROOT = 'D:\photo\derived'
$env:LANDING_ROOT = 'D:\photo\vault\.landing'
$env:MEDIA_ROOT = 'D:\photo\media'
$env:HOST_PATH_ROOT = 'D:\photo'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$py = 'C:\Users\zhang\Documents\Code\photo-vault\.venv\Scripts\python.exe'
$logDir = 'D:\photo\derived\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = Join-Path $logDir "ingest-records-$stamp.out.log"
$err = Join-Path $logDir "ingest-records-$stamp.err.log"
$p = Start-Process -FilePath $py -ArgumentList @('-m','vault.cli','ingest','D:\视频记录','--mode','link','--library','gallery','--progress','25') -WorkingDirectory 'C:\Users\zhang\Documents\Code\photo-vault' -NoNewWindow -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
Set-Content -Path (Join-Path $logDir 'ingest-records.python.pid') -Value $p.Id
$p.WaitForExit()
Set-Content -Path (Join-Path $logDir 'ingest-records.exit') -Value ("exit={0}`nended={1}`nstdout={2}`nstderr={3}" -f $p.ExitCode, (Get-Date -Format 's'), $out, $err)
'@
    Set-Content -Path $wrapper -Value $body -Encoding utf8

    Log 'restart ingest task (D:, link mode)'
    Start-ScheduledTask -TaskName PhotoVaultIngestRecords
    Set-Content -Path (Join-Path $dst 'derived\logs\migrate.done') -Value ('done ' + (Get-Date -Format s))
    Log '===== DONE ====='
}
catch {
    Log "ERROR: $($_.Exception.Message)"
    if (-not $appOnD) {
        Log 'roll back to E:'
        try {
            Set-EnvRoot 'E:\photo'
            docker compose up -d app 2>&1 | ForEach-Object { Log "  $_" }
            Log 'app is back on E:'
        } catch {
            Log "ROLLBACK FAILED: $($_.Exception.Message)"
        }
    }
    exit 1
}
finally {
    Pop-Location
}
