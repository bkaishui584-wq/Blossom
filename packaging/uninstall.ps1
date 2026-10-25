# Blossom 卸载脚本：删程序目录和快捷方式；用户数据默认保留。
#
#   -Purge   连用户数据（%LOCALAPPDATA%\DeskPet）一起删，会先二次确认
param(
    [string]$Target = "",
    [switch]$Purge
)

$ErrorActionPreference = "Stop"

function Say([string]$text) { Write-Host $text }

if (-not $Target) { $Target = Join-Path $env:LOCALAPPDATA "Programs\Blossom" }
$data = Join-Path $env:LOCALAPPDATA "DeskPet"

$running = Get-Process -Name "Blossom" -ErrorAction SilentlyContinue
if ($running) {
    Say "Blossom 正在运行，请先退出桌宠（右键角色 - 退出），再运行本脚本。"
    exit 1
}

# 兜底：别让手滑传进来的路径把不该删的目录删掉。
$resolved = [System.IO.Path]::GetFullPath($Target)
if ($resolved -eq [System.IO.Path]::GetPathRoot($resolved) -or $resolved.Length -lt 10) {
    Say "目标路径看起来不对，已停止：$resolved"
    exit 2
}

if (Test-Path -LiteralPath $resolved) {
    Say "正在删除程序目录：$resolved"
    try {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    } catch {
        Say "删除失败：$($_.Exception.Message)"
        Say "可能有文件被占用，退出所有 Blossom 窗口后重试。"
        exit 3
    }
    Say "程序目录已删除。"
} else {
    Say "没有找到程序目录，可能已经卸载过了。"
}

$link = Join-Path ([Environment]::GetFolderPath("Desktop")) "Blossom.lnk"
if (Test-Path -LiteralPath $link) {
    Remove-Item -LiteralPath $link -Force
    Say "桌面快捷方式已删除。"
}

if ($Purge) {
    Say ""
    Say "你加了 -Purge：接下来会删除用户数据 $data"
    Say "里面是记忆、会话、人格、角色包和配置，删了无法恢复。"
    $answer = Read-Host "确认删除请输入大写 YES"
    if ($answer -ceq "YES") {
        if (Test-Path -LiteralPath $data) { Remove-Item -LiteralPath $data -Recurse -Force }
        Say "用户数据已删除。"
    } else {
        Say "已取消，用户数据保留。"
    }
} else {
    Say ""
    Say "用户数据保留在 $data"
    Say "想彻底删干净，重新运行本脚本并加参数：uninstall.cmd --purge"
}

exit 0
