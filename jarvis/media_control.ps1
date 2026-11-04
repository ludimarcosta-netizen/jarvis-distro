param(
    [ValidateSet('status','play','pause','next','previous','volume')][string]$Action = 'status',
    [string]$AppId = '',
    [ValidateRange(-1, 100)][int]$Level = -1
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
if ($Action -eq 'volume') {
    # Volume do aplicativo no mixer do Windows (Core Audio), nunca o volume geral nem teclas globais.
    try {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
namespace JarvisAudio {
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumerator {}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int EnumAudioEndpoints(); [PreserveSig] int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice device); }
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { [PreserveSig] int Activate(ref Guid iid, int ctx, IntPtr p, [MarshalAs(UnmanagedType.IUnknown)] out object o); }
[Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioSessionManager2 { int GetAudioSessionControl(); int GetSimpleAudioVolume(); [PreserveSig] int GetSessionEnumerator(out IAudioSessionEnumerator e); }
[Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioSessionEnumerator { [PreserveSig] int GetCount(out int n); [PreserveSig] int GetSession(int i, out IAudioSessionControl2 s); }
[Guid("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioSessionControl2 { int A(); int B(); int C(); int D(); int E(); int F(); int G(); int H(); int I(); int J(); int K(); [PreserveSig] int GetProcessId(out int pid); }
[Guid("87CE5498-68D6-44E5-9215-6DA47EF883D8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface ISimpleAudioVolume { [PreserveSig] int SetMasterVolume(float level, ref Guid ctx); [PreserveSig] int GetMasterVolume(out float level); }
public static class Mixer {
    public static float[] Apply(string process, int level) {
        var devices = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        IMMDevice device; Marshal.ThrowExceptionForHR(devices.GetDefaultAudioEndpoint(0, 1, out device));
        Guid iid = typeof(IAudioSessionManager2).GUID; object o;
        Marshal.ThrowExceptionForHR(device.Activate(ref iid, 23, IntPtr.Zero, out o));
        IAudioSessionEnumerator sessions; Marshal.ThrowExceptionForHR(((IAudioSessionManager2)o).GetSessionEnumerator(out sessions));
        int count; sessions.GetCount(out count);
        var levels = new List<float>();
        for (int i = 0; i < count; i++) {
            IAudioSessionControl2 control; if (sessions.GetSession(i, out control) != 0) continue;
            int pid; if (control.GetProcessId(out pid) != 0 || pid == 0) continue;
            string name; try { name = Process.GetProcessById(pid).ProcessName; } catch { continue; }
            if (!string.Equals(name, process, StringComparison.OrdinalIgnoreCase)) continue;
            var volume = (ISimpleAudioVolume)control; Guid none = Guid.Empty;
            if (level >= 0) Marshal.ThrowExceptionForHR(volume.SetMasterVolume(level / 100f, ref none));
            float now; Marshal.ThrowExceptionForHR(volume.GetMasterVolume(out now));
            levels.Add(now);
        }
        return levels.ToArray();
    }
}
}
'@
        $process = if ($AppId.Contains('!')) { $AppId.Split('!')[-1] } elseif ($AppId -match '\.exe$') { $AppId -replace '\.exe$', '' } else { $AppId.Split('.')[0] }
        if (-not $process -or $process -notmatch '^[A-Za-z0-9_ -]{1,60}$') { throw 'Invalid app' }
        $levels = @([JarvisAudio.Mixer]::Apply($process, $Level) | ForEach-Object { [int][Math]::Round($_ * 100) })
        if ($levels.Count -eq 0) {
            @{ ok = $false; error = 'O aplicativo não tem áudio ativo no mixer do Windows. Inicie a reprodução e tente de novo.' } | ConvertTo-Json -Compress
        } else {
            $confirmed = $Level -lt 0 -or -not ($levels | Where-Object { [Math]::Abs($_ - $Level) -gt 1 })
            @{ ok = [bool]$confirmed; volume = $levels[0]; error = $(if (-not $confirmed) { 'O mixer não confirmou o novo volume.' } else { '' }) } | ConvertTo-Json -Compress
        }
    } catch {
        @{ ok = $false; error = 'Volume do aplicativo indisponível. Não houve nova tentativa.' } | ConvertTo-Json -Compress
    }
    exit 0
}
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime]
function Await-WinRT($Operation, [Type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (-not $task.Wait(2500)) { throw 'Media operation timed out' }
    return $task.Result
}
try {
    $manager = Await-WinRT ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
    $sessions = @($manager.GetSessions())
    if ($Action -eq 'status') {
        $rows = @($sessions | ForEach-Object {
            @{ app = $_.SourceAppUserModelId; status = $_.GetPlaybackInfo().PlaybackStatus.ToString() }
        })
        @{ ok = $true; sessions = $rows } | ConvertTo-Json -Depth 4 -Compress
    } else {
        $matches = @($sessions | Where-Object { $_.SourceAppUserModelId -ceq $AppId })
        if ($matches.Count -ne 1) { throw 'Choose one identifiable media session' }
        $session = $matches[0]
        $operation = switch ($Action) {
            play { $session.TryPlayAsync() }
            pause { $session.TryPauseAsync() }
            next { $session.TrySkipNextAsync() }
            previous { $session.TrySkipPreviousAsync() }
        }
        $accepted = Await-WinRT $operation ([bool])
        $status = $session.GetPlaybackInfo().PlaybackStatus.ToString()
        if ($accepted -and $Action -in @('play','pause')) {
            $expected = if ($Action -eq 'play') { 'Playing' } else { 'Paused' }
            for ($attempt = 0; $attempt -lt 5 -and $status -ne $expected; $attempt++) {
                Start-Sleep -Milliseconds 100
                $status = $session.GetPlaybackInfo().PlaybackStatus.ToString()
            }
            $accepted = $status -eq $expected
        }
        @{ ok = [bool]$accepted; status = $status; error = $(if (-not $accepted) { 'O aplicativo não confirmou o comando. Não houve nova tentativa.' } else { '' }) } | ConvertTo-Json -Compress
    }
} catch {
    @{ ok = $false; error = 'Controle de mídia indisponível ou sessão ambígua. Atualize as sessões e tente novamente.' } | ConvertTo-Json -Compress
}
