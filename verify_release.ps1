# 发行产物自检：哈希、包完整性、能力包边界。
#
#   .\verify_release.ps1
#   .\verify_release.ps1 -ReleaseDir D:\somewhere -Version 7.0.1
#
# 只读发行目录，不启动程序，不碰真实用户数据。
param(
    [string]$ReleaseDir = "",
    [string]$Version = "7.0.2"
)

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ReleaseDir) { $ReleaseDir = Join-Path $root "dist_release" }
$ReleaseDir = (Resolve-Path -LiteralPath $ReleaseDir).Path

$errors = New-Object System.Collections.Generic.List[string]
function Pass([string]$message) { Write-Output ("[OK]   " + $message) }
function Fail([string]$message) {
    $errors.Add($message)
    Write-Output ("[FAIL] " + $message)
}

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-Sha256([string]$path) {
    $stream = [System.IO.File]::OpenRead($path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $bytes = $sha.ComputeHash($stream)
            return ([System.BitConverter]::ToString($bytes) -replace "-", "").ToLower()
        }
        finally { $sha.Dispose() }
    }
    finally { $stream.Dispose() }
}

function Get-ZipEntries([string]$path) {
    $zip = [System.IO.Compression.ZipFile]::OpenRead($path)
    try { return @($zip.Entries | ForEach-Object { $_.FullName }) }
    finally { $zip.Dispose() }
}

function Test-Hashes([string]$dir) {
    $artifacts = @(Get-ChildItem -LiteralPath $dir -File | Where-Object { $_.Extension -in @(".zip", ".exe") })
    if (-not $artifacts.Count) { Fail "发行目录里没有 zip 或 exe"; return }
    foreach ($file in $artifacts) {
        $sidecar = $file.FullName + ".sha256"
        if (-not (Test-Path -LiteralPath $sidecar)) { Fail ("缺少校验文件：" + $file.Name); continue }
        $expected = (Get-Content -LiteralPath $sidecar -Raw).Trim().ToLower()
        $actual = Get-Sha256 $file.FullName
        if ($expected -ne $actual) { Fail ("哈希不一致：" + $file.Name) }
        else { Pass ("哈希一致：" + $file.Name) }
    }
}

function Test-ZipReadable([string]$path) {
    try { $entries = @(Get-ZipEntries $path); Pass ("包可读：" + (Split-Path -Leaf $path) + "（" + $entries.Count + " 项）") }
    catch { Fail ("包损坏：" + (Split-Path -Leaf $path) + "：" + $_.Exception.Message) }
}

$baseZip = Join-Path $ReleaseDir ("Blossom-" + $Version + "-win64.zip")
$setupExe = Join-Path $ReleaseDir ("Blossom-" + $Version + "-win64-setup.exe")
foreach ($required in @($baseZip, $setupExe)) {
    if (-not (Test-Path -LiteralPath $required)) { Fail ("缺少产物：" + (Split-Path -Leaf $required)) }
}

$voicePacks = @(Get-ChildItem -LiteralPath $ReleaseDir -Filter "voice-*.zip" -File)
$docPacks = @(Get-ChildItem -LiteralPath $ReleaseDir -Filter "docs-*.zip" -File)
if ($voicePacks.Count -ne 1) { Fail ("voice 能力包应只有 1 个，实际 " + $voicePacks.Count + " 个") }
if ($docPacks.Count -ne 1) { Fail ("docs 能力包应只有 1 个，实际 " + $docPacks.Count + " 个") }

Test-Hashes $ReleaseDir
foreach ($zip in @(Get-ChildItem -LiteralPath $ReleaseDir -Filter "*.zip" -File)) { Test-ZipReadable $zip.FullName }

if (Test-Path -LiteralPath $baseZip) {
    $names = @(Get-ZipEntries $baseZip)
    $forbidden = @(
        @{ Pattern = '(^|/)\.env$'; Label = ".env" },
        @{ Pattern = '(^|/)soundcard/'; Label = "soundcard 运行库" },
        @{ Pattern = '(^|/)sherpa_onnx/'; Label = "sherpa_onnx 运行库" },
        @{ Pattern = '(^|/)logs/'; Label = "日志目录" },
        @{ Pattern = 'deskpet\.sqlite3$'; Label = "用户数据库" }
    )
    foreach ($rule in $forbidden) {
        if ($names | Where-Object { $_ -match $rule.Pattern }) { Fail ("基础包混入了" + $rule.Label) }
        else { Pass ("基础包不含" + $rule.Label) }
    }
}

foreach ($pack in $voicePacks) {
    $names = @(Get-ZipEntries $pack.FullName)
    if (-not ($names | Where-Object { $_ -match '(^|/)soundcard/' })) { Fail ("语音包缺少 soundcard：" + $pack.Name) }
    if (-not ($names | Where-Object { $_ -match '(^|/)sherpa_onnx/' })) { Fail ("语音包缺少 sherpa_onnx：" + $pack.Name) }
    if ($names | Where-Object { $_ -match '(^|/)models/' -or $_ -match '\.onnx$' }) { Fail ("语音包混入了模型权重：" + $pack.Name) }
    else { Pass ("语音包不含模型权重：" + $pack.Name) }
}

Write-Output ""
if ($errors.Count) {
    Write-Output ("自检失败：" + $errors.Count + " 项")
    exit 1
}
Write-Output "发行自检全部通过。"
exit 0
