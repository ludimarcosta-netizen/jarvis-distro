$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class PdfCertificateMetadata {
    [DllImport("crypt32.dll", SetLastError=true)]
    public static extern bool CertGetCertificateContextProperty(IntPtr cert, uint property, IntPtr data, ref uint size);
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct ProviderInfo {
        public IntPtr Container;
        public IntPtr Provider;
        public uint ProviderType;
        public uint Flags;
        public uint ParameterCount;
        public IntPtr Parameters;
        public uint KeySpec;
    }
}
'@
$result = @()
foreach ($certificate in (Get-ChildItem Cert:\CurrentUser\My,Cert:\LocalMachine\My)) {
    $alias = if ($certificate.Subject -match '(?i)__JARVIS_USER_UPPER__') {'__JARVIS_USER_LOWER__'} elseif ($certificate.Subject -match '(?i)BELTRANO') {'beltrano'} else {$null}
    if (-not $alias) { continue }
    [uint32]$size = 0
    $provider = 'unknown'
    $kind = 'unknown'
    if ([PdfCertificateMetadata]::CertGetCertificateContextProperty($certificate.Handle, 2, [IntPtr]::Zero, [ref]$size)) {
        $buffer = [Runtime.InteropServices.Marshal]::AllocHGlobal([int]$size)
        try {
            if ([PdfCertificateMetadata]::CertGetCertificateContextProperty($certificate.Handle, 2, $buffer, [ref]$size)) {
                $info = [Runtime.InteropServices.Marshal]::PtrToStructure($buffer, [type][PdfCertificateMetadata+ProviderInfo])
                $provider = [Runtime.InteropServices.Marshal]::PtrToStringUni($info.Provider)
                $kind = if ($info.ProviderType -eq 0) {'CNG'} else {'CSP'}
            }
        } finally {
            [Runtime.InteropServices.Marshal]::FreeHGlobal($buffer)
        }
    }
    $result += [PSCustomObject]@{Alias=$alias;HasPrivateKey=$certificate.HasPrivateKey;Provider=$provider;Kind=$kind;ValidNow=($certificate.NotBefore -le (Get-Date) -and $certificate.NotAfter -gt (Get-Date))}
}
ConvertTo-Json -InputObject @($result)
