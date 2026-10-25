# 打出发行产物：绿色版 zip、单文件 exe、能力包、校验文件。
#
#   .\package.ps1                用现有的 dist\Blossom 打包
#   .\package.ps1 -Rebuild       先重新构建再打包
#   .\package.ps1 -Rebuild -SignThumbprint <证书指纹>
#                                构建、签名主程序、打包、再签名安装包
#
# 产物都在 dist_release\ 下。不上传、不安装任何东西。
param(
    [switch]$Rebuild,
    [string]$Version = "7.0.2",
    [string]$Python = "python",
    [string]$SignThumbprint = "",
    [string]$TimestampServer = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$appDir = Join-Path $root "dist\Blossom"
$releaseDir = Join-Path $root "dist_release"
$stageDir = Join-Path $root "build\release"
$name = "Blossom-$Version-win64"

if ($Rebuild) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "build.ps1") -NoShortcut
    if ($LASTEXITCODE -ne 0) { throw "打包失败" }
}
if (-not (Test-Path -LiteralPath (Join-Path $appDir "Blossom.exe"))) {
    throw "没有找到 $appDir\Blossom.exe，先运行 .\build.ps1"
}

if ($SignThumbprint) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "packaging\sign-file.ps1") `
        -Path (Join-Path $appDir "Blossom.exe") -Thumbprint $SignThumbprint -TimestampServer $TimestampServer
    if ($LASTEXITCODE -ne 0) { throw "主程序签名失败" }
}

if (Test-Path -LiteralPath $stageDir) { Remove-Item -LiteralPath $stageDir -Recurse -Force }
New-Item -ItemType Directory -Path $stageDir -Force | Out-Null
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

# 组装绿色版：程序 + 说明 + 许可证 + 安装/卸载脚本。
$portable = Join-Path $stageDir $name
Copy-Item -LiteralPath $appDir -Destination $portable -Recurse
# 发行包里不带 .env：那是开发机上由模板生成的一份配置，容易让人以为该改它。
# 用户自己的配置一律在 %LOCALAPPDATA%\DeskPet\.env，随包只放 .env.example 当模板。
foreach ($junk in @(".env", ".env.example.bak")) {
    $path = Join-Path $portable $junk
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
}
foreach ($extra in @("LICENSE", "THIRD-PARTY-NOTICES.md", "README.md")) {
    $source = Join-Path $root $extra
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $portable }
}
$guide = Join-Path $root "docs\install-guide.md"
if (Test-Path -LiteralPath $guide) {
    Copy-Item -LiteralPath $guide -Destination (Join-Path $portable "安装与使用.md")
}
# 安装/卸载脚本要整组带走：.cmd 只是纯 ASCII 的薄壳，真正干活的是同名的 .ps1。
# 少带 .ps1，用户双击安装就只剩一条"找不到文件"的报错。
foreach ($script in @("install.cmd", "install.ps1", "uninstall.cmd", "uninstall.ps1")) {
    Copy-Item -LiteralPath (Join-Path $root "packaging\$script") -Destination $portable
}

# 绿色版 zip
$zipPath = Join-Path $releaseDir "$name.zip"
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
Compress-Archive -Path $portable -DestinationPath $zipPath -CompressionLevel Optimal
Write-Host ("绿色版：{0}（{1:N1} MB）" -f $zipPath, ((Get-Item $zipPath).Length / 1MB))

# 能力包：已经在 dist_packs 里的直接搬过来。
$packsDir = Join-Path $root "dist_packs"
if (Test-Path -LiteralPath $packsDir) {
    $incomingPacks = @(Get-ChildItem -LiteralPath $packsDir -Filter "*.zip")
    # 同名能力包只留当前版本，避免 voice-1.0.0 和 voice-1.1.0 一起躺在发行目录里。
    $packGroups = @($incomingPacks | Group-Object { $_.BaseName -replace '-\d+(?:\.\d+)+$', '' })
    foreach ($group in $packGroups) {
        $family = $group.Name
        $latest = $group.Group | Sort-Object {
            $versionText = $_.BaseName -replace ("^" + [regex]::Escape($family) + "-"), ""
            try { [version]$versionText } catch { [version]"0.0" }
        } -Descending | Select-Object -First 1
        Get-ChildItem -LiteralPath $releaseDir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "${family}-*.zip" -or $_.Name -like "${family}-*.zip.sha256" } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
        Copy-Item -LiteralPath $latest.FullName -Destination $releaseDir -Force
        $hash = (Get-FileHash -LiteralPath $latest.FullName -Algorithm SHA256).Hash.ToLower()
        Set-Content -LiteralPath (Join-Path $releaseDir ($latest.Name + ".sha256")) -Value $hash -Encoding ASCII
        Write-Host ("能力包：{0}（{1:N1} MB）" -f $latest.Name, ($latest.Length / 1MB))
    }
}

# 单文件 exe：用系统自带的 IExpress 把绿色版 zip 包一层自解压壳，
# 解开后自动跑 install.cmd。不需要第三方打包工具。
#
# 注意：IExpress 读写的是 ANSI 路径，工程目录里带中文时它会直接失败（试过的）。
# 所以这里全程在 %TEMP% 下的纯英文目录里干活，做完再把 exe 搬回来。
$iexpressRoot = Join-Path $env:TEMP "BlossomPack"
if (Test-Path -LiteralPath $iexpressRoot) { Remove-Item -LiteralPath $iexpressRoot -Recurse -Force }
$sedPath = Join-Path $iexpressRoot "blossom.sed"
$payloadDir = Join-Path $iexpressRoot "payload"
New-Item -ItemType Directory -Path $payloadDir -Force | Out-Null
Copy-Item -LiteralPath $zipPath -Destination (Join-Path $payloadDir "app.zip")
Copy-Item -LiteralPath (Join-Path $root "packaging\setup-bootstrap.cmd") -Destination $payloadDir
$setupPath = Join-Path $iexpressRoot "Blossom-setup.exe"

$sed = @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=0
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=
DisplayLicense=
FinishMessage=
TargetName=$setupPath
FriendlyName=Blossom
AppLaunched=setup-bootstrap.cmd
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
SourceFiles=SourceFiles
[Strings]
FILE0="app.zip"
FILE1="setup-bootstrap.cmd"
[SourceFiles]
SourceFiles0=$payloadDir\
[SourceFiles0]
%FILE0%=
%FILE1%=
"@
Set-Content -LiteralPath $sedPath -Value $sed -Encoding ASCII

$iexpress = Join-Path $env:WINDIR "System32\iexpress.exe"
if (Test-Path -LiteralPath $iexpress) {
    & $iexpress /N /Q $sedPath | Out-Null
    Start-Sleep -Seconds 3
    if (Test-Path -LiteralPath $setupPath) {
        $finalSetup = Join-Path $releaseDir "$name-setup.exe"
        Move-Item -LiteralPath $setupPath -Destination $finalSetup -Force
        & $Python (Join-Path $root "packaging\apply_version_info.py") $finalSetup (Join-Path $root "resources\version_info_setup.txt")
        if ($LASTEXITCODE -ne 0) { throw "安装包版本信息写入失败" }
        if ($SignThumbprint) {
            & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "packaging\sign-file.ps1") `
                -Path $finalSetup -Thumbprint $SignThumbprint -TimestampServer $TimestampServer
            if ($LASTEXITCODE -ne 0) { throw "安装包签名失败" }
        }
        Write-Host ("单文件安装包：{0}（{1:N1} MB）" -f $finalSetup, ((Get-Item $finalSetup).Length / 1MB))
    } else {
        Write-Warning "IExpress 没有产出安装包（退出码 $LASTEXITCODE），绿色版 zip 仍然可用。"
    }
} else {
    Write-Warning "这台机器上没有 iexpress.exe，跳过单文件安装包。"
}
Remove-Item -LiteralPath $iexpressRoot -Recurse -Force -ErrorAction SilentlyContinue

if (-not $SignThumbprint) {
    Write-Host "提示：本次没有签名。配置正式代码签名证书后，加 -SignThumbprint 重新打包。"
}

# 校验文件：所有发行产物都给一份 sha256。
Get-ChildItem -LiteralPath $releaseDir -File | Where-Object { $_.Extension -in @(".zip", ".exe") } | ForEach-Object {
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower()
    Set-Content -LiteralPath (Join-Path $releaseDir ($_.Name + ".sha256")) -Value $hash -Encoding ASCII
}

Write-Host ""
Write-Host "发行产物都在：$releaseDir"
Get-ChildItem -LiteralPath $releaseDir | Select-Object Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } } | Format-Table -AutoSize
