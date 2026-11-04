# Instalador do Jarvis.
#
#   powershell -ExecutionPolicy Bypass -File .\instalar.ps1
#
# 1. Instala o Hermes Agent (base do Jarvis) fixado na versão testada, pelo instalador oficial.
# 2. Abre o menu do Jarvis (perfil, IA e recursos opcionais), usando o Python que o Hermes instalou.
#
# Rodar de novo retoma de onde parou ou liga recursos novos.
[CmdletBinding()]
param(
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" }),
    [switch]$ReinstalarHermes,   # roda o instalador oficial mesmo com o Hermes já instalado
    [switch]$Forcar              # instala sobre uma pasta jarvis que não veio deste instalador
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Raiz = $PSScriptRoot
$HermesVersao = (Get-Content (Join-Path $Raiz 'hermes-patches\BASE.txt') -Raw).Trim()
# Commit da tag acima; fixado para o instalador oficial não trazer código que o patch do Jarvis não conhece.
$HermesCommit = '818c13be1dc4fd28987e1e881a9408224afd4535'
$Python = Join-Path $HermesHome 'hermes-agent\venv\Scripts\python.exe'

function Instalar-Hermes {
    $url = "https://raw.githubusercontent.com/NousResearch/hermes-agent/$HermesCommit/scripts/install.ps1"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "hermes-install-$HermesCommit.ps1"
    Write-Host "Baixando o instalador oficial do Hermes $HermesVersao..."
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $tmp `
            -Commit $HermesCommit -HermesHome $HermesHome -InstallDir (Join-Path $HermesHome 'hermes-agent') -NonInteractive
        if ($LASTEXITCODE) { throw "o instalador do Hermes terminou com código $LASTEXITCODE" }
    } finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
    }
}

if ($ReinstalarHermes -or -not (Test-Path $Python)) {
    Instalar-Hermes
} else {
    Write-Host "Hermes já instalado em $HermesHome (use -ReinstalarHermes para reinstalar)."
}
if (-not (Test-Path $Python)) { throw "Python do Hermes não encontrado em $Python" }

$env:PYTHONPATH = Join-Path $Raiz 'installer'
$env:PYTHONUTF8 = '1'
$argumentos = @('-m', 'instalador', '--home', $HermesHome, '--origem', $Raiz)
if ($Forcar) { $argumentos += '--forcar' }
& $Python @argumentos
exit $LASTEXITCODE
