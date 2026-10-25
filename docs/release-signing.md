# Blossom 代码签名

当前 7.0.1 安装包没有正式代码签名。Windows SmartScreen 可能提示未知发布者。

## 证书要求

准备一张支持代码签名的 Authenticode 证书，证书用途包含：

`1.3.6.1.5.5.7.3.3` Code Signing

证书和私钥放在当前用户的个人证书存储 `Cert:\CurrentUser\My`。测试证书和自签名证书只能用于本机验证，不能解决公开分发时的 SmartScreen 提示。

## 导入 PFX

在 PowerShell 里执行，密码用交互输入，不写进命令历史：

```powershell
$pwd = Read-Host -AsSecureString "PFX 密码"
Import-PfxCertificate -FilePath .\blossom-codesign.pfx -CertStoreLocation Cert:\CurrentUser\My -Password $pwd
```

查看可用证书：

```powershell
Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
  Format-List Subject, Thumbprint, NotAfter
```

## 构建签名发行包

在项目根目录执行：

```powershell
.\package.ps1 -Rebuild -SignThumbprint <证书指纹>
```

脚本按以下顺序执行：

1. 重新构建 `dist\Blossom\Blossom.exe`。
2. 给主程序写入 Authenticode 签名。
3. 生成绿色版 ZIP 和单文件安装包。
4. 给单文件安装包写入版本信息和 Authenticode 签名。
5. 重新生成 `.sha256`。

时间戳服务器默认是 `http://timestamp.digicert.com`。需要换服务器时加 `-TimestampServer`。

## 验证

```powershell
Get-AuthenticodeSignature .\dist\Blossom\Blossom.exe
Get-AuthenticodeSignature .\dist_release\Blossom-7.0.1-win64-setup.exe
```

正式证书的签名状态应为 `Valid`。自签名证书可能显示 `NotTrusted` 或 `UnknownError`，这表示签名存在但证书链没有被系统信任。

## 发布前检查

- 确认证书主体与发布者名称一致。
- 确认时间戳有效，签名在证书过期后仍可验证。
- 在干净 Windows 虚拟机里双击安装包，检查 SmartScreen 提示和首次启动向导。
- 重新计算并分发 ZIP、EXE 的 SHA256。
- 不要把 PFX、密码或证书私钥放进发行包。
