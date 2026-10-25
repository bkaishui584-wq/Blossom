# Blossom 安装脚本：把程序放到当前用户目录，并建桌面快捷方式。
#
# 为什么用 PowerShell 而不是批处理：建快捷方式、算路径、报错误都在这里更稳，
# 批处理只留一层纯 ASCII 的薄壳去调它（批处理带中文容易被编码绊倒）。
#
#   -Source   程序所在目录，默认就是本脚本所在目录
#   -Target   安装到哪，默认 %LOCALAPPDATA%\Programs\Blossom
#   -NoShortcut  只装，不建快捷方式
#   -Quiet    少说话（给自解压安装包用）
param(
    [string]$Source = "",
    [string]$Target = "",
    [switch]$NoShortcut,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

function Say([string]$text) {
    if (-not $Quiet) { Write-Host $text }
}

if (-not $Source) { $Source = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Target) { $Target = Join-Path $env:LOCALAPPDATA "Programs\Blossom" }

$Source = (Resolve-Path -LiteralPath $Source).Path
if (-not (Test-Path -LiteralPath (Join-Path $Source "Blossom.exe"))) {
    Write-Warning "这个目录里没有 Blossom.exe：$Source"
    exit 2
}
if ($Source -eq $Target) {
    Say "已经就在安装目录里，不用再复制：$Target"
} else {
    Say "正在安装到 $Target"
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    # robocopy 的退出码 0-7 都算成功，8 以上才是失败。
    $null = robocopy $Source $Target /E /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) {
        Write-Warning "复制文件失败（robocopy 退出码 $LASTEXITCODE）。检查磁盘空间和权限后重试。"
        exit 3
    }
}

$exe = Join-Path $Target "Blossom.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    Write-Warning "装完之后没找到 $exe"
    exit 4
}

if (-not $NoShortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    if (-not $desktop -or -not (Test-Path -LiteralPath $desktop)) {
        Write-Warning "找不到桌面目录，快捷方式没建。"
        exit 5
    }
    $link = Join-Path $desktop "Blossom.lnk"
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($link)
        $shortcut.TargetPath = $exe
        $shortcut.WorkingDirectory = $Target
        $shortcut.IconLocation = "$exe,0"
        $shortcut.Description = "Blossom 桌宠"
        $shortcut.Save()
    } catch {
        Write-Warning "快捷方式没建成：$($_.Exception.Message)"
        Write-Warning "程序已经装好了，可以直接双击 $exe 使用。"
        exit 6
    }
    if (Test-Path -LiteralPath $link) {
        Say "桌面快捷方式：$link"
    } else {
        Write-Warning "快捷方式写完之后没找到，可能被安全软件拦了。"
        exit 7
    }
}

Say ""
Say "安装完成。桌面上的 Blossom 图标双击就能用。"
Say "第一次启动有引导页，说明哪些能力本机可用、哪些要联网或配密钥。"
Say "用户数据在 $env:LOCALAPPDATA\DeskPet，安装和升级都不会动它。"
Say "卸载：运行程序目录里的 uninstall.cmd。"
exit 0
