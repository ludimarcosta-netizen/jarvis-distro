param([ValidateSet('inventory','inventory-online','sign')][string]$Mode = 'inventory')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    $records = @()
    foreach ($certificate in (Get-ChildItem Cert:\CurrentUser\My,Cert:\LocalMachine\My)) {
        $alias = if ($certificate.Subject -match '(?i)__JARVIS_USER_UPPER__') {'__JARVIS_USER_LOWER__'} elseif ($certificate.Subject -match '(?i)BELTRANO') {'beltrano'} else {$null}
        if (-not $alias) { continue }
        $digest = [System.Security.Cryptography.SHA256]::Create()
        try { $fingerprint = ([BitConverter]::ToString($digest.ComputeHash($certificate.RawData))).Replace('-','').ToLowerInvariant() }
        finally { $digest.Dispose() }
        $records += @{ Alias=$alias; Certificate=$certificate; Fingerprint=$fingerprint }
    }
    if ($Mode -in @('inventory','inventory-online')) {
        $result = @()
        foreach ($record in $records) {
            $certificate = $record.Certificate
            $chain = [System.Security.Cryptography.X509Certificates.X509Chain]::new()
            try {
                $chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::Offline
                if ($Mode -eq 'inventory-online') {
                    $chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::Online
                }
                $chain.ChainPolicy.UrlRetrievalTimeout = [TimeSpan]::FromSeconds(10)
                $chain.ChainPolicy.RevocationFlag = [System.Security.Cryptography.X509Certificates.X509RevocationFlag]::EntireChain
                if ($chain.ChainPolicy.PSObject.Properties.Name -contains 'DisableCertificateDownloads') { $chain.ChainPolicy.DisableCertificateDownloads = ($Mode -eq 'inventory') }
                $chainOk = $chain.Build($certificate)
                $publicChain = @($chain.ChainElements | ForEach-Object { [Convert]::ToBase64String($_.Certificate.RawData) })
                $result += @{alias=$record.Alias; fingerprint=$record.Fingerprint; has_private_key=$certificate.HasPrivateKey;
                    valid_now=($certificate.NotBefore -le (Get-Date) -and $certificate.NotAfter -gt (Get-Date));
                    certificate=[Convert]::ToBase64String($certificate.RawData); chain=$publicChain; chain_ok=$chainOk;
                    chain_status=@($chain.ChainStatus | ForEach-Object { $_.Status.ToString() })}
            } finally { $chain.Dispose() }
        }
        ConvertTo-Json -InputObject @($result) -Depth 5 -Compress
        exit 0
    }
    # No password, container name or private key material enters stdin/stdout.
    $inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($inputData.digest -ne 'sha256' -or $inputData.fingerprint -notmatch '^[a-f0-9]{64}$') { throw 'Invalid signing request' }
    $matches = @($records | Where-Object { $_.Alias -eq $inputData.alias -and $_.Fingerprint -eq $inputData.fingerprint })
    if ($matches.Count -ne 1) { throw 'Ambiguous certificate' }
    $certificate = $matches[0].Certificate
    if (-not $certificate.HasPrivateKey -or $certificate.NotBefore -gt (Get-Date) -or $certificate.NotAfter -le (Get-Date)) { throw 'Unavailable certificate' }
    $data = [Convert]::FromBase64String($inputData.data)
    if ($data.Length -gt 100000) { throw 'Oversized signing request' }
    $private = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($certificate)
    if ($null -eq $private) { throw 'RSA provider unavailable' }
    try {
        $signature = $private.SignData($data, [System.Security.Cryptography.HashAlgorithmName]::SHA256, [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
        @{signature=[Convert]::ToBase64String($signature)} | ConvertTo-Json -Compress
    } finally { $private.Dispose() }
} catch {
    [Console]::Error.WriteLine('Certificate operation blocked; inspect provider and approval locally.')
    exit 2
}
