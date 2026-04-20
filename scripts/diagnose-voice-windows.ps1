# ======================================================================
# Sovyx — Voice Capture Forensic Diagnostic (Windows only)
#
# Collects every piece of evidence required to determine WHY a microphone
# passes audio (healthy RMS) but SileroVAD sees silence (probability
# stuck near zero). The tool is read-only: no registry writes, no
# service changes. Safe to run as a normal user — HKLM:\SOFTWARE\...
# MMDevices is world-readable.
#
# OUTPUT (all written under <repo>\tmp\voice-diag\ by default)
#   - sovyx-voice-diagnostic.json    Main report (metadata + enumerations)
#   - sovyx-voice-diagnostic.log     Full text transcript of this run
#   - raw\sovyx.log                  Daemon log (if Sovyx was running)
#   - raw\capture_combos.json        Cascade state (copy of user's data)
#   - raw\capture_overrides.json     Per-device overrides
#   - raw\endpoint_quarantine.json   Quarantined endpoints
#   - raw\system.yaml                Sovyx system config
#   - raw\capture-diagnostics-response.json   Live /api endpoint dump
#
# USAGE (PowerShell, NO admin required)
#   cd E:\sovyx
#   powershell -ExecutionPolicy Bypass -File .\scripts\diagnose-voice-windows.ps1
#
# The assistant can read the entire output dir directly from the repo.
# ======================================================================

[CmdletBinding()]
param(
    # Default output dir is <repo>\tmp\voice-diag so the assistant can
    # read the artifacts directly from the project tree.
    [string]$OutDir  = '',
    [string]$OutJson = '',
    [string]$OutLog  = ''
)

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

# Resolve default paths. $PSScriptRoot = <repo>\scripts, so parent = <repo>.
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$repoRoot  = Split-Path -Parent $scriptDir
if (-not $OutDir)  { $OutDir  = Join-Path $repoRoot 'tmp\voice-diag' }
if (-not $OutJson) { $OutJson = Join-Path $OutDir   'sovyx-voice-diagnostic.json' }
if (-not $OutLog)  { $OutLog  = Join-Path $OutDir   'sovyx-voice-diagnostic.log' }

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$RawDir = Join-Path $OutDir 'raw'
if (-not (Test-Path $RawDir)) { New-Item -ItemType Directory -Path $RawDir -Force | Out-Null }

# Transcript captures every Write-Host/Warning into the .log file.
try { Stop-Transcript | Out-Null } catch { }
Start-Transcript -Path $OutLog -Force | Out-Null
Write-Host ("Output dir: {0}" -f $OutDir)

function Write-Section([string]$title) {
    Write-Host ''
    Write-Host ('=' * 72)
    Write-Host (" {0}" -f $title)
    Write-Host ('=' * 72)
}

$Report = [ordered]@{
    schema_version       = 1
    collected_at_utc     = (Get-Date).ToUniversalTime().ToString('o')
    tool_version         = '2026-04-20'
    windows              = $null
    powershell           = $null
    audio_endpoints      = @()
    known_apo_catalog    = $null
    third_party_dsp      = @()
    exclusive_mode       = @()
    portaudio_enum       = $null
    sovyx_doctor         = $null
    sovyx_capture_diag   = $null
    sovyx_config         = $null
    sovyx_data_dir       = $null
    errors               = @()
}

# ----------------------------------------------------------------------
# 1. Windows + PowerShell fingerprint
# ----------------------------------------------------------------------
Write-Section 'Windows + PowerShell'

try {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    $cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop
    $Report.windows = [ordered]@{
        caption                    = $os.Caption
        version                    = $os.Version
        build                      = $os.BuildNumber
        ubr                        = $cv.UBR
        display_version            = $cv.DisplayVersion
        product_name               = $cv.ProductName
        edition                    = $cv.EditionID
        install_date_utc           = (Get-Date $os.InstallDate).ToUniversalTime().ToString('o')
        system_locale              = (Get-Culture).Name
        ui_locale                  = (Get-UICulture).Name
        architecture               = $env:PROCESSOR_ARCHITECTURE
    }
    Write-Host ("OS     : {0}  {1}  (build {2}.{3})" -f $os.Caption, $cv.DisplayVersion, $os.BuildNumber, $cv.UBR)
    Write-Host ("Locale : {0} / {1}" -f (Get-Culture).Name, (Get-UICulture).Name)
} catch {
    $Report.errors += "windows_fingerprint_failed: $_"
    Write-Warning $_
}

$Report.powershell = [ordered]@{
    edition = $PSVersionTable.PSEdition
    version = $PSVersionTable.PSVersion.ToString()
}

# ----------------------------------------------------------------------
# 2. MMDevices registry dump  (ground truth for the APO detector)
# ----------------------------------------------------------------------
Write-Section 'Capture endpoints + APO chain (HKLM MMDevices)'

# Catalog of CLSIDs the Sovyx APO detector knows about.
# Kept in sync with src\sovyx\voice\_apo_detector.py — if this list
# disagrees with that module in future versions, update both.
$KnownClsids = @{
    '{62DC1A93-AE24-464C-A43E-452F824C4250}' = 'MS Default Stream FX'
    '{47620F45-DBE4-47F3-B308-09F9120CFB05}' = 'MS Communications Mode APO'
    '{112F45E0-A531-42A8-9DE5-57F0EB73C6DE}' = 'MS Voice Capture DMO'
    '{CF1DDA2C-3B93-4EFE-8AA9-DEB6F8D4FDF1}' = 'MS Acoustic Echo Cancellation'
    '{9CF81848-DE9F-4BDF-B177-A9D8B16A7AAB}' = 'MS Automatic Gain Control'
    '{1B20CB5B-6E1B-4BFE-A2F6-7E3E81C7E0F4}' = 'MS Voice Isolation'
    '{7A8B0F43-6C2E-4C85-A1A6-C9F1F7D50E9D}' = 'MS Voice Focus'
    '{96BEDF2C-18CB-4A15-B821-5E95ED0FEA61}' = 'Windows APO container'
}
$PackagePatterns = @(
    @{ needle = 'vocaeffectpack';  label = 'Windows Voice Clarity' },
    @{ needle = 'voiceclarityep';  label = 'Windows Voice Clarity' },
    @{ needle = 'voiceclarity';    label = 'Windows Voice Clarity' },
    @{ needle = 'voiceisolation';  label = 'MS Voice Isolation' },
    @{ needle = 'voicefocus';      label = 'MS Voice Focus' }
    @{ needle = 'nvidia broadcast';label = 'NVIDIA Broadcast Mic' },
    @{ needle = 'nvbroadcast';     label = 'NVIDIA Broadcast Mic' },
    @{ needle = 'krisp';           label = 'Krisp noise suppression' },
    @{ needle = 'rzsynapse';       label = 'Razer Synapse DSP' },
    @{ needle = 'razer';           label = 'Razer audio DSP (generic)' }
)

$Report.known_apo_catalog = $KnownClsids

$CaptureRoot = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture'
$CaptureRootRaw = 'SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture'

# PKEY_* slots we want to read explicitly
$PKEY_FriendlyName    = '{a45c254e-df1c-4efd-8020-67d146a850e0},14'
$PKEY_DeviceDesc      = '{a45c254e-df1c-4efd-8020-67d146a850e0},2'
$PKEY_Enumerator      = '{a45c254e-df1c-4efd-8020-67d146a850e0},24'
$PKEY_InterfaceName   = '{b3f8fa53-0004-438e-9003-51a46e139bfc},6'

function Read-RegValue([Microsoft.Win32.RegistryKey]$Key, [string]$Name) {
    if ($null -eq $Key) { return $null }
    try {
        $v = $Key.GetValue($Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        return $v
    } catch {
        return $null
    }
}

function Decode-Value($value) {
    if ($null -eq $value) { return '' }
    if ($value -is [string]) { return $value }
    if ($value -is [byte[]]) {
        try {
            return [System.Text.Encoding]::Unicode.GetString($value).TrimEnd([char]0)
        } catch { return '' }
    }
    if ($value -is [int] -or $value -is [uint32]) {
        return ('{0}' -f $value)
    }
    return ($value.ToString())
}

$ClsidRegex = '\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}'

try {
    $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::LocalMachine,
        [Microsoft.Win32.RegistryView]::Registry64
    )
    $captureKey = $baseKey.OpenSubKey($CaptureRootRaw)
    if ($null -eq $captureKey) {
        throw "MMDevices\Audio\Capture subkey not found"
    }

    foreach ($endpointId in $captureKey.GetSubKeyNames()) {
        $epKey = $captureKey.OpenSubKey($endpointId)
        if ($null -eq $epKey) { continue }
        try {
            $state = Read-RegValue $epKey 'DeviceState'
            $isActive = ($state -eq 1)

            $propsKey = $epKey.OpenSubKey('Properties')
            $fxKey    = $epKey.OpenSubKey('FxProperties')

            $friendly       = if ($propsKey) { Decode-Value (Read-RegValue $propsKey $PKEY_FriendlyName) } else { '' }
            if (-not $friendly -and $propsKey) {
                $friendly   = Decode-Value (Read-RegValue $propsKey $PKEY_DeviceDesc)
            }
            $enumerator     = if ($propsKey) { Decode-Value (Read-RegValue $propsKey $PKEY_Enumerator) }    else { '' }
            $deviceIface    = if ($propsKey) { Decode-Value (Read-RegValue $propsKey $PKEY_InterfaceName) } else { '' }

            $fxValues = @()
            if ($fxKey) {
                foreach ($valName in $fxKey.GetValueNames()) {
                    $raw = $fxKey.GetValue($valName, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                    $decoded = Decode-Value $raw
                    $kind = $fxKey.GetValueKind($valName).ToString()
                    $decodedLen = if ($decoded) { $decoded.Length } else { 0 }
                    # Truncate absurd blobs — we only need the CLSIDs / package names
                    # embedded in the string; anything beyond 4000 chars is raw PCM/DSP
                    # preset data that would bloat the JSON and choke ConvertTo-Json.
                    if ($decodedLen -gt 4000) {
                        $decoded = $decoded.Substring(0, 4000) + '...<TRUNCATED>'
                    }
                    $fxValues += [ordered]@{
                        name           = $valName
                        kind           = $kind
                        decoded        = $decoded
                        decoded_length = $decodedLen
                    }
                }
            }

            $clsids = @()
            $known  = @()
            $knownSeen = @{}
            $voiceClarityActive = $false
            foreach ($v in $fxValues) {
                $text = $v.decoded
                $matches = [regex]::Matches($text, $ClsidRegex)
                foreach ($m in $matches) {
                    $up = $m.Value.ToUpper()
                    if ($clsids -notcontains $up) { $clsids += $up }
                    if ($KnownClsids.ContainsKey($up)) {
                        $label = $KnownClsids[$up]
                        if (-not $knownSeen.ContainsKey($label)) {
                            $knownSeen[$label] = $true
                            $known += $label
                        }
                    }
                }
                $lowered = $text.ToLower()
                foreach ($p in $PackagePatterns) {
                    if ($lowered.Contains($p.needle)) {
                        if (-not $knownSeen.ContainsKey($p.label)) {
                            $knownSeen[$p.label] = $true
                            $known += $p.label
                        }
                        if ($p.label -eq 'Windows Voice Clarity') {
                            $voiceClarityActive = $true
                        }
                    }
                }
            }

            # Additional policy knobs that influence exclusive-mode + enhancements.
            # These live under the Properties subkey as PKEY_AudioEndpoint_* pids.
            $enableSysFx       = $null
            $disableSysFx      = $null
            $enableExclusive   = $null
            $policyFmtid       = '{1da5d803-d492-4edd-8c23-e0c0ffee7f0e}'
            if ($propsKey) {
                $enableSysFx     = Read-RegValue $propsKey "$policyFmtid,5"   # PKEY_AudioEndpoint_Disable_SysFx semantics vary by OEM
                $disableSysFx    = Read-RegValue $propsKey "$policyFmtid,0"
                $enableExclusive = Read-RegValue $propsKey "$policyFmtid,7"
            }

            $endpoint = [ordered]@{
                endpoint_id                = $endpointId
                device_state               = $state
                is_active                  = $isActive
                friendly_name              = $friendly
                device_interface_name      = $deviceIface
                enumerator                 = $enumerator
                fx_values_count            = $fxValues.Count
                fx_values                  = $fxValues
                all_clsids                 = $clsids
                known_apos                 = $known
                voice_clarity_active       = $voiceClarityActive
                pkey_disable_sysfx         = $disableSysFx
                pkey_enable_sysfx          = $enableSysFx
                pkey_enable_exclusive_mode = $enableExclusive
            }
            $Report.audio_endpoints += $endpoint

            if ($isActive) {
                Write-Host ''
                Write-Host ("[ACTIVE] {0}" -f $friendly)
                Write-Host ("  endpoint_id : {0}" -f $endpointId)
                Write-Host ("  interface   : {0}" -f $deviceIface)
                Write-Host ("  enumerator  : {0}" -f $enumerator)
                Write-Host ("  fx_values   : {0}" -f $fxValues.Count)
                if ($known.Count -gt 0) {
                    Write-Host ("  known APOs  : {0}" -f ($known -join ', ')) -ForegroundColor Yellow
                } else {
                    Write-Host  "  known APOs  : (none recognised from Sovyx catalog)"
                }
                if ($voiceClarityActive) {
                    Write-Host '  >>> Windows Voice Clarity ACTIVE on this endpoint <<<' -ForegroundColor Red
                }
                if ($clsids.Count -gt 0) {
                    Write-Host "  raw CLSIDs  :"
                    foreach ($c in $clsids) {
                        $label = if ($KnownClsids.ContainsKey($c)) { " ($($KnownClsids[$c]))" } else { '' }
                        Write-Host ("    - {0}{1}" -f $c, $label)
                    }
                }
            }
        } finally {
            if ($propsKey) { $propsKey.Close() }
            if ($fxKey)    { $fxKey.Close()    }
            $epKey.Close()
        }
    }
    $captureKey.Close()
    $baseKey.Close()
} catch {
    $Report.errors += "mmdevices_enum_failed: $_"
    Write-Warning $_
}

# ----------------------------------------------------------------------
# 3. Per-device mic-enhancement checkboxes (Enhancements tab equivalent)
#    Windows exposes these in the UI as "Enable Audio Enhancements" but
#    the underlying storage is per-effect in FxProperties plus a few
#    endpoint-level properties. We already captured FxProperties above;
#    here we expose the per-endpoint SpatialAudio + VoiceProcessing flags.
# ----------------------------------------------------------------------
Write-Section 'Capture-side policy flags'
# Values of interest are inside each endpoint's "Properties" subkey under
# GUID {1da5d803-d492-4edd-8c23-e0c0ffee7f0e},<pid>  — already read above
# as pkey_enable_exclusive_mode / disable_sysfx / enable_sysfx. Expose
# them in a flat per-device table so the assistant can eyeball values.
foreach ($e in $Report.audio_endpoints) {
    if (-not $e.is_active) { continue }
    $row = [ordered]@{
        friendly_name         = $e.friendly_name
        endpoint_id           = $e.endpoint_id
        pkey_disable_sysfx    = $e.pkey_disable_sysfx
        pkey_enable_sysfx     = $e.pkey_enable_sysfx
        pkey_enable_exclusive = $e.pkey_enable_exclusive_mode
    }
    $Report.exclusive_mode += $row
    Write-Host ("{0}: disable_sysfx={1}  enable_sysfx={2}  exclusive={3}" -f $e.friendly_name, $e.pkey_disable_sysfx, $e.pkey_enable_sysfx, $e.pkey_enable_exclusive_mode)
}

# ----------------------------------------------------------------------
# 4. Third-party DSP / noise-suppression processes + installed packages
# ----------------------------------------------------------------------
Write-Section 'Third-party DSP software'

$DspIndicators = @(
    # Razer
    @{ proc = 'RzSynapse';      name = 'Razer Synapse' },
    @{ proc = 'Razer Synapse';  name = 'Razer Synapse' },
    @{ proc = 'RazerCortex';    name = 'Razer Cortex' },
    @{ proc = 'RzAudioAppSvc';  name = 'Razer Audio App Service' },
    # NVIDIA
    @{ proc = 'NVIDIA Broadcast'; name = 'NVIDIA Broadcast' },
    @{ proc = 'NvBroadcast';      name = 'NVIDIA Broadcast (service)' },
    @{ proc = 'NVIDIA Share';     name = 'NVIDIA Share (GFE)' },
    @{ proc = 'NvContainer';      name = 'NVIDIA Container' },
    # Krisp
    @{ proc = 'Krisp';          name = 'Krisp' },
    @{ proc = 'krisp-helper';   name = 'Krisp helper' },
    # Discord / Zoom / Teams (may inject RNNoise variant)
    @{ proc = 'Discord';        name = 'Discord' },
    @{ proc = 'Zoom';           name = 'Zoom' },
    @{ proc = 'Teams';          name = 'Microsoft Teams' },
    # Voice changers / virtual cables
    @{ proc = 'VoiceMeeter';    name = 'VoiceMeeter' },
    @{ proc = 'VoicemodDesktop';name = 'Voicemod' },
    @{ proc = 'vcvad';          name = 'VB-Cable Virtual' },
    # SteelSeries / Logitech DSP suites
    @{ proc = 'SteelSeriesGG';  name = 'SteelSeries GG' },
    @{ proc = 'LGHUB';          name = 'Logitech G HUB' }
)

foreach ($entry in $DspIndicators) {
    $procs = Get-Process -Name $entry.proc -ErrorAction SilentlyContinue
    if ($procs) {
        foreach ($p in $procs) {
            $startTime = $null
            try { $startTime = $p.StartTime.ToString('o') } catch { }
            $procPath = $null
            try { $procPath = $p.Path } catch { }
            $row = [ordered]@{
                software   = $entry.name
                process    = $p.Name
                pid        = $p.Id
                start_time = $startTime
                path       = $procPath
            }
            $Report.third_party_dsp += $row
            Write-Host ("{0,-30}  {1}  (pid={2})" -f $entry.name, $p.Name, $p.Id) -ForegroundColor Yellow
        }
    }
}

# Scan Uninstall registry for installed audio-processing packages.
$uninstallRoots = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$installedDspHits = @()
foreach ($r in $uninstallRoots) {
    try {
        Get-ItemProperty -Path $r -ErrorAction SilentlyContinue |
            Where-Object {
                $_.DisplayName -and (
                    $_.DisplayName -match 'Razer|Nahimic|SmartAudio|Waves|NVIDIA Broadcast|Krisp|Realtek|Synapse|SteelSeries|SonicSuite|Sonic Studio|Dolby|DTS|MaxxAudio|Voice Clarity|Voice Isolation|VB-Audio|Voicemod|VoiceMeeter'
                )
            } |
            ForEach-Object {
                $installedDspHits += [ordered]@{
                    display_name    = $_.DisplayName
                    display_version = $_.DisplayVersion
                    publisher       = $_.Publisher
                    install_date    = $_.InstallDate
                }
            }
    } catch { }
}
$Report.installed_audio_software = $installedDspHits
if ($installedDspHits.Count -gt 0) {
    Write-Host ''
    Write-Host 'Installed audio-processing packages:'
    foreach ($h in $installedDspHits) {
        Write-Host ("  - {0}  v{1}  ({2})" -f $h.display_name, $h.display_version, $h.publisher)
    }
}

# ----------------------------------------------------------------------
# 5. PortAudio-level device enumeration — requires Python + sounddevice.
#    Uses sovyx's own environment when available so the enumeration
#    matches what the daemon sees.
# ----------------------------------------------------------------------
Write-Section 'PortAudio enumeration (via Sovyx Python environment)'

$paSnippet = @'
import json, sys
try:
    import sounddevice as sd
    apis = [dict(name=a["name"], index=i) for i,a in enumerate(sd.query_hostapis())]
    devs = []
    for i,d in enumerate(sd.query_devices()):
        devs.append({
            "index": i,
            "name": d.get("name"),
            "host_api_index": d.get("hostapi"),
            "host_api_name": sd.query_hostapis(d.get("hostapi"))["name"],
            "max_input_channels": d.get("max_input_channels"),
            "max_output_channels": d.get("max_output_channels"),
            "default_samplerate": d.get("default_samplerate"),
        })
    default_in = sd.default.device[0] if sd.default.device else None
    print(json.dumps({"ok": True, "host_apis": apis, "devices": devs, "default_input": default_in}))
except Exception as e:
    print(json.dumps({"ok": False, "error": repr(e), "error_type": type(e).__name__}))
'@

$pythonCandidates = @('python', 'python3', 'py')
$paJson = $null
foreach ($cmd in $pythonCandidates) {
    try {
        $paJson = & $cmd -c $paSnippet 2>$null
        if ($LASTEXITCODE -eq 0 -and $paJson) { break }
    } catch { }
}
if ($paJson) {
    try {
        $Report.portaudio_enum = $paJson | ConvertFrom-Json -ErrorAction Stop
        if ($Report.portaudio_enum.ok) {
            Write-Host ("{0,3}  {1,-24}  {2,-32}  in={3}  out={4}  rate={5}" -f 'idx','host_api','name','in','out','rate')
            foreach ($d in $Report.portaudio_enum.devices) {
                Write-Host ("{0,3}  {1,-24}  {2,-32}  in={3}  out={4}  rate={5}" -f `
                    $d.index, $d.host_api_name, $d.name.Substring(0, [Math]::Min($d.name.Length, 32)), $d.max_input_channels, $d.max_output_channels, [int]$d.default_samplerate)
            }
            Write-Host ("default_input_index : {0}" -f $Report.portaudio_enum.default_input)
        } else {
            Write-Warning ("PortAudio enum error: {0}" -f $Report.portaudio_enum.error)
        }
    } catch {
        $Report.errors += "portaudio_enum_parse_failed: $_"
        Write-Warning $_
    }
} else {
    $Report.errors += 'python_or_sounddevice_unavailable'
    Write-Warning 'Python + sounddevice not reachable - skipped PortAudio enumeration'
}

# ----------------------------------------------------------------------
# 6. sovyx doctor (voice-specific checks)
# ----------------------------------------------------------------------
Write-Section 'sovyx doctor (voice_capture_apo + voice_capture_kernel_invalidated)'

$doctorChecks = @('voice_capture_apo', 'voice_capture_kernel_invalidated')
$doctorResults = [ordered]@{}
foreach ($chk in $doctorChecks) {
    try {
        $raw = & sovyx doctor $chk --json 2>$null
        if ($LASTEXITCODE -eq 0 -and $raw) {
            $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
            $doctorResults[$chk] = $parsed
            $msg = if ($parsed.message) { $parsed.message } else { $parsed.status }
            Write-Host ("[{0}] {1}" -f $chk, $msg)
        } else {
            $doctorResults[$chk] = @{ error = 'no_output_or_nonzero_exit'; raw = $raw }
        }
    } catch {
        $doctorResults[$chk] = @{ error = "$_" }
    }
}
$Report.sovyx_doctor = $doctorResults

# ----------------------------------------------------------------------
# 7. Sovyx data dir + config file sidecars
# ----------------------------------------------------------------------
# IMPORTANT: we DO NOT inline file contents into the JSON report.
# PowerShell 5.1's ConvertTo-Json hangs for minutes on medium-sized
# strings with unicode content (known bug). Instead we COPY each
# config file into $RawDir as a sidecar and only record metadata
# (size, sha256, timestamps) in the JSON. The assistant can read
# the sidecars directly for full content.
Write-Section 'Sovyx data directory snapshot'

$sovyxDataDir = Join-Path $env:USERPROFILE '.sovyx'
$Report.sovyx_data_dir = $sovyxDataDir

$configFiles = [ordered]@{
    'capture_combos.json'      = Join-Path $sovyxDataDir 'voice\capture_combos.json'
    'capture_overrides.json'   = Join-Path $sovyxDataDir 'voice\capture_overrides.json'
    'endpoint_quarantine.json' = Join-Path $sovyxDataDir 'voice\endpoint_quarantine.json'
    'system.yaml'              = Join-Path $sovyxDataDir 'system.yaml'
}

$configMeta = [ordered]@{}
foreach ($kv in $configFiles.GetEnumerator()) {
    $name = $kv.Key
    $path = $kv.Value
    if (Test-Path $path) {
        try {
            $info = Get-Item -Path $path -ErrorAction Stop
            $copyPath = Join-Path $RawDir $name
            Copy-Item -Path $path -Destination $copyPath -Force -ErrorAction Stop
            $sha = $null
            try { $sha = (Get-FileHash -Path $path -Algorithm SHA256).Hash } catch { }
            $configMeta[$name] = [ordered]@{
                present     = $true
                source_path = $path
                size_bytes  = [int64]$info.Length
                last_write  = $info.LastWriteTime.ToUniversalTime().ToString('o')
                sha256      = $sha
                copied_to   = $copyPath
            }
            Write-Host ("  captured {0,-28} -> raw\{0}  ({1:N0} bytes)" -f $name, $info.Length)
        } catch {
            $configMeta[$name] = [ordered]@{
                present     = $true
                source_path = $path
                error       = "$_"
            }
            Write-Warning ("copy failed for {0}: {1}" -f $name, $_)
        }
    } else {
        $configMeta[$name] = [ordered]@{
            present     = $false
            source_path = $path
        }
        Write-Host ("  (not present) {0}" -f $path)
    }
}
$Report.sovyx_config = $configMeta

# ----------------------------------------------------------------------
# 8. Ask the running daemon (if any) for /api/voice/capture-diagnostics
# ----------------------------------------------------------------------
Write-Section 'Live /api/voice/capture-diagnostics probe'

$tokenFile = Join-Path $sovyxDataDir 'token'
if (Test-Path $tokenFile) {
    try {
        $token = (Get-Content -Raw -Path $tokenFile).Trim()
        $headers = @{ Authorization = "Bearer $token" }
        # Invoke-WebRequest (not RestMethod) so we can dump the raw JSON
        # directly to a sidecar file without re-serializing. Avoids the
        # PS 5.1 ConvertTo-Json bug when the response has rich content.
        $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:7777/api/voice/capture-diagnostics' -Headers $headers -TimeoutSec 5 -ErrorAction Stop
        $diagSidecar = Join-Path $RawDir 'capture-diagnostics-response.json'
        Set-Content -Path $diagSidecar -Value $resp.Content -Encoding UTF8
        $parsed = $resp.Content | ConvertFrom-Json -ErrorAction Stop
        $epCount = 0
        if ($parsed.endpoints) { $epCount = $parsed.endpoints.Count }
        # Only keep a flat summary in the main JSON (easy to ConvertTo-Json).
        $Report.sovyx_capture_diag = [ordered]@{
            copied_to                = $diagSidecar
            size_bytes               = $resp.Content.Length
            active_device_name       = $parsed.active_device_name
            voice_clarity_active     = $parsed.voice_clarity_active
            any_voice_clarity_active = $parsed.any_voice_clarity_active
            endpoints_count          = $epCount
        }
        Write-Host ("active_device_name       : {0}" -f $parsed.active_device_name)
        Write-Host ("voice_clarity_active     : {0}" -f $parsed.voice_clarity_active)
        Write-Host ("any_voice_clarity_active : {0}" -f $parsed.any_voice_clarity_active)
        Write-Host ("endpoints reported       : {0}" -f $epCount)
        Write-Host ("full response saved to   : {0}" -f $diagSidecar)
    } catch {
        $Report.sovyx_capture_diag = @{ error = "$_" }
        Write-Warning "capture-diagnostics fetch failed: $_"
    }
} else {
    $Report.sovyx_capture_diag = @{ error = 'token_file_missing' }
    Write-Host '  (daemon not running - no token file; start `sovyx start` and re-run for live endpoint data)'
}

# ----------------------------------------------------------------------
# 8b. Copy the daemon log if it exists (most important forensic artifact)
# ----------------------------------------------------------------------
Write-Section 'Daemon log snapshot'

$daemonLog = Join-Path $sovyxDataDir 'logs\sovyx.log'
if (Test-Path $daemonLog) {
    try {
        $logInfo = Get-Item $daemonLog
        $logCopy = Join-Path $RawDir 'sovyx.log'
        Copy-Item -Path $daemonLog -Destination $logCopy -Force -ErrorAction Stop
        $logHash = $null
        try { $logHash = (Get-FileHash -Path $daemonLog -Algorithm SHA256).Hash } catch { }
        $Report.sovyx_daemon_log = [ordered]@{
            present     = $true
            source_path = $daemonLog
            size_bytes  = [int64]$logInfo.Length
            last_write  = $logInfo.LastWriteTime.ToUniversalTime().ToString('o')
            sha256      = $logHash
            copied_to   = $logCopy
        }
        Write-Host ("  daemon log captured  ({0:N0} bytes)  ->  {1}" -f $logInfo.Length, $logCopy)
    } catch {
        $Report.sovyx_daemon_log = [ordered]@{
            present     = $true
            source_path = $daemonLog
            error       = "$_"
        }
        Write-Warning ("daemon log copy failed: {0}" -f $_)
    }
} else {
    $Report.sovyx_daemon_log = [ordered]@{
        present     = $false
        source_path = $daemonLog
    }
    Write-Host ("  (not present) {0}" -f $daemonLog)
}

# ----------------------------------------------------------------------
# 9. Write JSON dump (section-by-section, with visible progress)
# ----------------------------------------------------------------------
# We do NOT use a single ConvertTo-Json call here. PowerShell 5.1's
# serializer is silent while working, so a large $Report would look hung.
# Instead we walk the top-level keys, call ConvertTo-Json on each value
# individually, and print one progress line per section. The resulting
# JSON pieces are concatenated into a single valid JSON object.
Write-Section 'Writing JSON report (per-section serialization)'

function ConvertTo-JsonSafe($value, [int]$Depth = 8) {
    if ($null -eq $value) { return 'null' }
    try {
        $out = ConvertTo-Json -InputObject $value -Depth $Depth -Compress
        if ($null -eq $out -or $out -eq '') { return 'null' }
        return $out
    } catch {
        return '"<serialize_failed>"'
    }
}

$swTotal = [System.Diagnostics.Stopwatch]::StartNew()
$topKeys = @($Report.Keys)
$jsonParts = @()
$i = 0

foreach ($k in $topKeys) {
    $i++
    $val = $Report[$k]

    if ($k -eq 'audio_endpoints' -and $val -and $val.Count -gt 0) {
        # The fat section: serialize each endpoint individually so the user
        # sees constant forward motion even if one endpoint is huge.
        Write-Host ("  [{0,2}/{1}] {2} ({3} endpoints)" -f $i, $topKeys.Count, $k, $val.Count)
        $epParts = @()
        for ($e = 0; $e -lt $val.Count; $e++) {
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            $epJson = ConvertTo-JsonSafe $val[$e] 8
            $sw.Stop()
            $epParts += $epJson
            $nm = $val[$e].friendly_name
            if (-not $nm) { $nm = '(unnamed)' }
            if ($nm.Length -gt 40) { $nm = $nm.Substring(0, 40) }
            Write-Host ("        endpoint {0,2}/{1,-2}  {2,-40}  {3,6:N2}s  {4,8:N0} bytes" -f ($e+1), $val.Count, $nm, $sw.Elapsed.TotalSeconds, $epJson.Length)
        }
        $partValue = '[' + ($epParts -join ',') + ']'
    } else {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $partValue = ConvertTo-JsonSafe $val 8
        $sw.Stop()
        Write-Host ("  [{0,2}/{1}] {2,-28}  {3,6:N2}s  {4,8:N0} bytes" -f $i, $topKeys.Count, $k, $sw.Elapsed.TotalSeconds, $partValue.Length)
    }

    # JSON-encode the key name (safe even though our keys are ASCII).
    $keyEnc = ConvertTo-Json -InputObject $k -Compress
    $jsonParts += ("{0}:{1}" -f $keyEnc, $partValue)
}

Write-Host '  Concatenating + writing to disk...'
$json = '{' + ($jsonParts -join ',') + '}'
Set-Content -Path $OutJson -Value $json -Encoding UTF8
$swTotal.Stop()

Write-Host ''
Write-Host ("  TOTAL: {0:N1}s, {1:N0} bytes" -f $swTotal.Elapsed.TotalSeconds, $json.Length)
Write-Host ("  JSON   : {0}" -f $OutJson)
Write-Host ("  Log    : {0}" -f $OutLog)
Write-Host ("  Raw dir: {0}  (file sidecars: daemon log, quarantine, combos, capture-diagnostics response)" -f $RawDir)

Stop-Transcript | Out-Null

Write-Host ''
Write-Host ("DONE. All artifacts are under: {0}" -f $OutDir) -ForegroundColor Green
Write-Host 'The assistant can read them directly from the project tree.' -ForegroundColor Green
