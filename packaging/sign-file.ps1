param(
    [Parameter(Mandatory = $true)][string[]]$Path,
    [Parameter(Mandatory = $true)][string]$Thumbprint,
    [string]$TimestampServer = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

$wanted = ($Thumbprint -replace "[^0-9A-Fa-f]", "").ToUpperInvariant()
if (-not $wanted) {
    Write-Error "A code signing certificate thumbprint is required."
    exit 2
}

$cert = $null
foreach ($store in @("Cert:\CurrentUser\My", "Cert:\LocalMachine\My")) {
    $cert = Get-ChildItem $store -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object { $_.Thumbprint -eq $wanted } |
        Select-Object -First 1
    if ($cert) { break }
}
if (-not $cert) {
    Write-Error "Code signing certificate not found: $wanted"
    exit 3
}
if (-not $cert.HasPrivateKey) {
    Write-Error "The certificate has no private key: $wanted"
    exit 4
}
$ekuExtension = $cert.Extensions |
    Where-Object { $_.Oid.Value -eq "2.5.29.37" } |
    Select-Object -First 1
if (-not $ekuExtension) {
    Write-Error "The certificate has no enhanced key usage: $wanted"
    exit 5
}
$eku = New-Object System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension(
    $ekuExtension,
    $false
)
$usage = @($eku.EnhancedKeyUsages | ForEach-Object { $_.Value })
if ($usage -notcontains "1.3.6.1.5.5.7.3.3") {
    Write-Error "The certificate is not valid for code signing: $wanted"
    exit 6
}

foreach ($item in $Path) {
    $file = (Resolve-Path -LiteralPath $item -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        Write-Error "Not a file: $file"
        exit 7
    }
    $signArgs = @{
        FilePath = $file
        Certificate = $cert
        HashAlgorithm = "SHA256"
    }
    if ($TimestampServer) {
        $signArgs.TimestampServer = $TimestampServer
    }
    $result = Set-AuthenticodeSignature @signArgs
    if (-not $result.SignerCertificate -or $result.SignerCertificate.Thumbprint -ne $cert.Thumbprint) {
        Write-Error "Signing failed: $file"
        exit 8
    }
    Write-Host ("signed: {0} [{1}]" -f $file, $result.Status)
}

exit 0
