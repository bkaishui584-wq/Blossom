# 打包 Blossom.exe，并可选地创建桌面快捷方式。
#
#   .\build.ps1                 打包 + 创建桌面快捷方式
#   .\build.ps1 -NoShortcut     只打包
#   .\build.ps1 -ShortcutOnly   只重建桌面快捷方式
#   .\build.ps1 -StartMenu      顺带放进开始菜单（默认不放，跟着用户自己的选择走）
param(
    [switch]$NoShortcut,
    [switch]$ShortcutOnly,
    [switch]$StartMenu,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$exePath = Join-Path $root "dist\Blossom\Blossom.exe"

if (-not $ShortcutOnly) {
    & $Python -m PyInstaller --noconfirm --clean Blossom.spec
    if ($LASTEXITCODE -ne 0) { throw "打包失败" }
    if (-not (Test-Path -LiteralPath $exePath)) { throw "没有找到 $exePath" }

    # 首次打包时给一份空的配置模板，用户填完密钥就能用；里面没有真实密钥。
    $target = Join-Path $root "dist\Blossom"
    Copy-Item -LiteralPath (Join-Path $root ".env.example") -Destination (Join-Path $target ".env.example") -Force
    if (-not (Test-Path -LiteralPath (Join-Path $target ".env"))) {
        Copy-Item -LiteralPath (Join-Path $root ".env.example") -Destination (Join-Path $target ".env")
    }
    Write-Host "打包完成：$exePath"
}

if (-not (Test-Path -LiteralPath $exePath)) { throw "没有找到 $exePath，先运行一次打包" }

function New-Shortcut([string]$lnk) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($lnk)
    $shortcut.TargetPath = $exePath
    $shortcut.WorkingDirectory = Split-Path -Parent $exePath
    $shortcut.IconLocation = "$exePath,0"
    $shortcut.Description = "Blossom 桌宠"
    $shortcut.Save()
    Write-Host "快捷方式：$lnk"
}

if (-not $NoShortcut) {
    New-Shortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "Blossom.lnk")
    # 改名后旧快捷方式要收掉，免得桌面上两个图标指向两个不同的包。
    $stale = Join-Path ([Environment]::GetFolderPath("Desktop")) "DeskPet.lnk"
    if (Test-Path -LiteralPath $stale) { Remove-Item -LiteralPath $stale -Force }
}

if ($StartMenu) {
    $programs = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
    New-Shortcut (Join-Path $programs "Blossom.lnk")
}
