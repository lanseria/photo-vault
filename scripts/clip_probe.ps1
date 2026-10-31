#Requires -Version 7
<#
.SYNOPSIS
    剪辑交付链路探针：硬链接出稿目录 + 剪贴板文件列表（CF_HDROP）+ 资源管理器定位。

.DESCRIPTION
    整个"搜索 → 拖进剪辑软件"方案压在三个假设上，这个脚本一次性验掉它们：

      假设 1  硬链接出来的文件，剪映/Explorer 当成普通文件（不是快捷方式）
      假设 2  剪贴板里放 CF_HDROP（多文件），剪映素材库按 Ctrl+V 能导入
      假设 3  Explorer 定位到出稿目录后，拖进剪映是"引用"而不是"拷贝"

    默认从 E:\photo\vault 里挑两个 <25MB 的视频做样本（不碰原库，只读）。
    出稿目录默认 E:\photo\derived\_clip_probe，删掉不影响任何东西。

.EXAMPLE
    .\scripts\clip_probe.ps1                      # 自动挑 2 个样片，跑完剪贴板就有东西了
    .\scripts\clip_probe.ps1 -Explorer            # 顺带打开资源管理器选中这些文件
    .\scripts\clip_probe.ps1 -Files 'D:\a.mp4','D:\b.mov'   # 用自己的文件测
    .\scripts\clip_probe.ps1 -ClipboardOnly       # 只写剪贴板，不建目录
#>
param(
    [string[]]$Files,
    [string]$Vault = 'E:\photo\vault',
    [string]$Out = 'E:\photo\derived\_clip_probe',
    [int]$MaxMB = 25,
    [switch]$ClipboardOnly,
    [switch]$Explorer
)

$ErrorActionPreference = 'Stop'

Add-Type -Namespace Win32 -Name Clip -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError=true)]
public static extern bool OpenClipboard(System.IntPtr hWndNewOwner);
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError=true)]
public static extern bool CloseClipboard();
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError=true)]
public static extern bool EmptyClipboard();
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError=true)]
public static extern System.IntPtr SetClipboardData(uint uFormat, System.IntPtr hMem);
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError=true)]
public static extern System.IntPtr GlobalAlloc(uint uFlags, System.UIntPtr dwBytes);
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError=true)]
public static extern System.IntPtr GlobalLock(System.IntPtr hMem);
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError=true)]
public static extern bool GlobalUnlock(System.IntPtr hMem);
'@

function Set-ClipboardFileDrop {
    <# 把一组真实文件路径放进剪贴板，格式 = CF_HDROP（Windows 资源管理器复制文件用的就是它）。
       结构：20 字节 DROPFILES 头 + UTF-16 双 \0 结尾的路径列表。 #>
    param([Parameter(Mandatory)][string[]]$Paths)

    $existing = $Paths | Where-Object { Test-Path -LiteralPath $_ }
    if (-not $existing) { throw '没有可放进剪贴板的文件' }

    # DROPFILES: pFiles=20 | pt=(0,0) | fNC=0 | fWide=1
    $header = [byte[]]::new(20)
    [BitConverter]::GetBytes([int]20).CopyTo($header, 0)
    [BitConverter]::GetBytes([int]1).CopyTo($header, 16)   # fWide = TRUE

    $list = ($existing -join "`0") + "`0`0"
    $body = [System.Text.Encoding]::Unicode.GetBytes($list)
    $size = $header.Length + $body.Length

    $hMem = [Win32.Clip]::GlobalAlloc(0x0002, [UIntPtr]::new([int64]$size))   # GMEM_MOVEABLE
    if ($hMem -eq [IntPtr]::Zero) { throw "GlobalAlloc 失败 (size=$size)" }

    $p = [Win32.Clip]::GlobalLock($hMem)
    if ($p -eq [IntPtr]::Zero) { throw 'GlobalLock 失败' }
    [System.Runtime.InteropServices.Marshal]::Copy($header, 0, $p, $header.Length)
    [System.Runtime.InteropServices.Marshal]::Copy($body, 0, [IntPtr]::Add($p, $header.Length), $body.Length)
    [void][Win32.Clip]::GlobalUnlock($hMem)

    if (-not [Win32.Clip]::OpenClipboard([IntPtr]::Zero)) { throw 'OpenClipboard 失败（别的程序正占着剪贴板）' }
    try {
        [void][Win32.Clip]::EmptyClipboard()
        # 成功后所有权交给系统，不能再 GlobalFree
        if ([Win32.Clip]::SetClipboardData(15, $hMem) -eq [IntPtr]::Zero) {
            throw "SetClipboardData 失败 (err=$([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
        }
    } finally {
        [void][Win32.Clip]::CloseClipboard()
    }
}

# ---------------------------------------------------------------- 1. 选样本
if (-not $Files) {
    if (-not (Test-Path -LiteralPath $Vault)) { throw "vault 不存在: $Vault" }
    Write-Host "扫描 $Vault 找 <${MaxMB}MB 的视频样本…" -ForegroundColor DarkGray
    $pick = Get-ChildItem -LiteralPath $Vault -Recurse -File -Include *.mp4, *.mov, *.MP4, *.MOV |
            Where-Object { $_.Length -lt ($MaxMB * 1MB) -and $_.Length -gt 1MB } |
            Sort-Object Length |
            Select-Object -First 2
    if (-not $pick) { throw "库里没有 <${MaxMB}MB 的视频，改用 -Files 指定" }
    $Files = $pick.FullName
}

$Files = $Files | ForEach-Object { (Resolve-Path -LiteralPath $_).Path }
Write-Host "`n样本 $($Files.Count) 个：" -ForegroundColor Cyan
$Files | ForEach-Object { Write-Host ("  {0,-14} {1}" -f ('{0:N1} MB' -f ((Get-Item -LiteralPath $_).Length / 1MB)), $_) }

# ---------------------------------------------------------------- 2. 硬链接出稿
$deliver = $Files
if (-not $ClipboardOnly) {
    if (Test-Path -LiteralPath $Out) { Remove-Item -LiteralPath $Out -Recurse -Force }
    New-Item -ItemType Directory -Path $Out -Force | Out-Null

    $i = 0
    $deliver = foreach ($f in $Files) {
        $i++
        $src = Get-Item -LiteralPath $f
        # 出稿名：序号 + 时间 + 原名，保证在剪辑软件素材库里按时间顺序排
        $name = '{0:D3}_{1}_{2}' -f $i, $src.LastWriteTime.ToString('yyyyMMdd_HHmmss'), $src.Name
        $dst = Join-Path $Out $name
        New-Item -ItemType HardLink -Path $dst -Target $src.FullName | Out-Null
        $dst
    }
    $total = ($Files | ForEach-Object { (Get-Item -LiteralPath $_).Length } | Measure-Object -Sum).Sum
    Write-Host "`n出稿目录 $Out  （硬链接，额外占用 0 字节；总量 $('{0:N1} MB' -f ($total/1MB))）" -ForegroundColor Green
    $deliver | ForEach-Object { Write-Host "  $($_ | Split-Path -Leaf)" }
}

# ---------------------------------------------------------------- 3. 剪贴板
try {
    Set-ClipboardFileDrop -Paths @($deliver)
    Write-Host "`n剪贴板已放入 $($deliver.Count) 个文件（CF_HDROP，多选格式）" -ForegroundColor Green
} catch {
    Write-Warning "写剪贴板失败：$($_.Exception.Message)"
}

# ---------------------------------------------------------------- 4. 资源管理器
if ($Explorer) {
    $first = @($deliver)[0]
    Start-Process explorer.exe -ArgumentList "/select,`"$first`""
    Write-Host "已打开资源管理器并选中第一个文件" -ForegroundColor Green
}

Write-Host @"

—— 接下来手动验三件事（各 10 秒）——
  ① 剪映 → 素材库 → Ctrl+V        : 文件进来没有？(假设 2：剪贴板导入)
  ② 资源管理器里把文件拖进素材库    : 进来没有？(假设 1：硬链接被当普通文件)
  ③ 剪映素材库看文件名/时长        : 排序对不对？(命名策略)
  若 ① 不行、② 行 → 走"出稿目录 + 拖拽"，不做剪贴板。
  若都行 → 剪贴板是最省事的交付通道，一条 /api/clip/copy 就够。
"@ -ForegroundColor Cyan
