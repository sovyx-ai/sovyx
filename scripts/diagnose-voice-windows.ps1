# ======================================================================
# Sovyx -- Voice Capture Forensic Diagnostic (Windows only)
#
# Collects every piece of evidence required to determine WHY a microphone
# passes audio (healthy RMS) but SileroVAD sees silence (probability
# stuck near zero). The tool is read-only: no registry writes, no
# service changes. Safe to run as a normal user -- HKLM:\SOFTWARE\...
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
    [string]$OutLog  = '',
    # V2: opt-in 30s ETW capture (heavy: ~50-200 MB .etl). Requires
    # admin (wpr.exe). When -CaptureEtwProfile points to an .wprp file
    # (e.g. microsoft/audio audio.wprp), uses that profile; otherwise
    # uses GeneralProfile + CPU built-in profiles.
    [switch]$CaptureEtw,
    [string]$CaptureEtwProfile = '',
    [int]$CaptureEtwSeconds = 30
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
    tool_version         = '2026-04-24-v2'
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
    # V2 (2026-04-24): camadas adicionais para paridade com Linux toolkit.
    hardware             = $null
    pnp_audio_devices    = @()
    hotfixes_recent      = @()
    audio_services       = @()
    audio_event_log      = @()
    apo_dll_resolution   = @()
    consent_store        = $null
    defender_exclusions  = $null
    network_llm          = @()
    live_captures        = @()
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
# Kept in sync with src\sovyx\voice\_apo_detector.py -- if this list
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
                    # Truncate absurd blobs -- we only need the CLSIDs / package names
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
# GUID {1da5d803-d492-4edd-8c23-e0c0ffee7f0e},<pid>  -- already read above
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
# 5. PortAudio-level device enumeration -- requires Python + sounddevice.
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

# ======================================================================
# V2 EXTENSIONS (2026-04-24) -- sections 10-15
#   10. Hardware fingerprint (BIOS/Board/PnP audio devices + DEVPKEYs)
#   11. Recent KBs + audio services + System event log
#   12. APO CLSID -> InprocServer32 DLL -> AuthenticodeSignature -> KB
#   13. ConsentStore (microphone) + Defender exclusions + AppLocker
#   14. WASAPI exclusive vs shared comparator probe (sounddevice + PaWPatch)
#   15. Network reachability to LLM endpoints
# ======================================================================

# ----------------------------------------------------------------------
# 10. Hardware fingerprint + PnP audio devices
# ----------------------------------------------------------------------
Write-Section 'Hardware (BIOS/Board/PnP audio devices)'

try {
    $bios   = Get-CimInstance Win32_BIOS -ErrorAction Stop
    $board  = Get-CimInstance Win32_BaseBoard -ErrorAction Stop
    $sys    = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
    $Report.hardware = [ordered]@{
        bios_vendor       = $bios.Manufacturer
        bios_version      = $bios.SMBIOSBIOSVersion
        bios_release_date = if ($bios.ReleaseDate) { $bios.ReleaseDate.ToString('o') } else { $null }
        board_manufacturer= $board.Manufacturer
        board_product     = $board.Product
        system_manufacturer = $sys.Manufacturer
        system_model      = $sys.Model
        system_total_ram_gb = [math]::Round($sys.TotalPhysicalMemory / 1GB, 2)
    }
    Write-Host ("BIOS  : {0} {1}" -f $bios.Manufacturer, $bios.SMBIOSBIOSVersion)
    Write-Host ("System: {0} / {1}" -f $sys.Manufacturer, $sys.Model)
} catch {
    $Report.errors += "hardware_fingerprint_failed: $_"
    Write-Warning $_
}

# PnP audio devices (MEDIA + AudioEndpoint + AudioProcessingObject classes).
# Cross-reference DEVPKEY for driver version + INF + signing date.
# CRITICAL: anti-pattern #21 (Voice Clarity APO) ships via UBR servicing --
# the AudioProcessingObject driver date is the smoking gun.
Write-Host ''
Write-Host 'PnP audio devices (MEDIA + AudioEndpoint + AudioProcessingObject):'
try {
    $pnpDevices = Get-PnpDevice -PresentOnly -Class MEDIA, AudioEndpoint, AudioProcessingObject -ErrorAction Stop
    foreach ($dev in $pnpDevices) {
        $row = [ordered]@{
            instance_id   = $dev.InstanceId
            class         = $dev.Class
            friendly_name = $dev.FriendlyName
            status        = $dev.Status
            driver_version= $null
            driver_date   = $null
            driver_provider = $null
            driver_inf    = $null
            location_info = $null
            bus_reported  = $null
        }
        # DEVPKEY queries -- each is one cmdlet call. Slow O(devices*5).
        try {
            $props = Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName `
                'DEVPKEY_Device_DriverVersion','DEVPKEY_Device_DriverDate',`
                'DEVPKEY_Device_DriverProvider','DEVPKEY_Device_DriverInfPath',`
                'DEVPKEY_Device_LocationInfo','DEVPKEY_Device_BusReportedDeviceDesc' `
                -ErrorAction SilentlyContinue
            foreach ($p in $props) {
                switch ($p.KeyName) {
                    'DEVPKEY_Device_DriverVersion'         { $row.driver_version  = $p.Data }
                    'DEVPKEY_Device_DriverDate'            { $row.driver_date     = if ($p.Data) { $p.Data.ToString('o') } else { $null } }
                    'DEVPKEY_Device_DriverProvider'        { $row.driver_provider = $p.Data }
                    'DEVPKEY_Device_DriverInfPath'         { $row.driver_inf      = $p.Data }
                    'DEVPKEY_Device_LocationInfo'          { $row.location_info   = $p.Data }
                    'DEVPKEY_Device_BusReportedDeviceDesc' { $row.bus_reported    = $p.Data }
                }
            }
        } catch { }
        $Report.pnp_audio_devices += $row
        if ($dev.Class -eq 'AudioProcessingObject') {
            Write-Host ("  [APO] {0,-50}  {1}  ({2})" -f $dev.FriendlyName, $row.driver_version, $row.driver_date) -ForegroundColor Yellow
        } else {
            Write-Host ("  [{0,-12}] {1,-50}  {2}" -f $dev.Class, $dev.FriendlyName, $row.driver_version)
        }
    }
} catch {
    $Report.errors += "pnp_enum_failed: $_"
    Write-Warning $_
}

# ----------------------------------------------------------------------
# 11. Recent HotFixes + Audio services + System event log audio errors
# ----------------------------------------------------------------------
Write-Section 'Windows Updates + Audio services + Event log'

# HotFix history (last 30 by InstalledOn DESC).
# CRITICAL: Voice Clarity APO arrived as UBR servicing -- the most recent
# KBs are top suspects for "broke after update" cases.
try {
    $hotfixes = Get-HotFix -ErrorAction Stop | Sort-Object InstalledOn -Descending | Select-Object -First 30
    foreach ($hf in $hotfixes) {
        $Report.hotfixes_recent += [ordered]@{
            hotfix_id     = $hf.HotFixID
            description   = $hf.Description
            installed_on  = if ($hf.InstalledOn) { $hf.InstalledOn.ToString('o') } else { $null }
            installed_by  = $hf.InstalledBy
        }
    }
    if ($Report.hotfixes_recent.Count -gt 0) {
        Write-Host ("Last {0} hotfixes:" -f $Report.hotfixes_recent.Count)
        foreach ($h in $Report.hotfixes_recent | Select-Object -First 10) {
            Write-Host ("  {0,-10}  {1,-12}  {2}" -f $h.hotfix_id, $h.description, $h.installed_on)
        }
    }
} catch {
    $Report.errors += "hotfix_enum_failed: $_"
    Write-Warning $_
}

# Audio service state.
$audioSvcs = @('Audiosrv','AudioEndpointBuilder','MMCSS','RpcSs')
foreach ($svcName in $audioSvcs) {
    try {
        $svc = Get-CimInstance Win32_Service -Filter "Name='$svcName'" -ErrorAction Stop
        if ($svc) {
            $Report.audio_services += [ordered]@{
                name       = $svc.Name
                state      = $svc.State
                start_mode = $svc.StartMode
                pid        = $svc.ProcessId
                exit_code  = $svc.ExitCode
                status     = $svc.Status
            }
            Write-Host ("  {0,-22}  state={1,-10}  pid={2}" -f $svc.Name, $svc.State, $svc.ProcessId)
        }
    } catch { }
}

# System event log: audio-relevant errors/warnings, last 200.
Write-Host ''
Write-Host 'System event log (audio sources, last 200):'
try {
    $audioEvents = Get-WinEvent -FilterHashtable @{
        LogName='System'
        ProviderName=@(
            'Microsoft-Windows-Audio',
            'Microsoft-Windows-AudioCore',
            'Microsoft-Windows-AudioDeviceGraphIsolation',
            'Microsoft-Windows-Kernel-PnP'
        )
        Level=@(2,3)  # Error + Warning
    } -MaxEvents 200 -ErrorAction Stop
    foreach ($ev in $audioEvents) {
        $Report.audio_event_log += [ordered]@{
            time          = $ev.TimeCreated.ToString('o')
            level         = $ev.LevelDisplayName
            provider      = $ev.ProviderName
            event_id      = $ev.Id
            message_head  = if ($ev.Message) {
                $ev.Message.Substring(0, [Math]::Min($ev.Message.Length, 200))
            } else { '' }
        }
    }
    Write-Host ("  captured {0} audio-related System events (level=Error|Warning)" -f $Report.audio_event_log.Count)
} catch {
    if ($_.Exception.Message -notmatch 'No events were found') {
        $Report.errors += "audio_event_log_failed: $_"
        Write-Warning $_
    } else {
        Write-Host '  (no audio-relevant errors/warnings in System log)'
    }
}

# ----------------------------------------------------------------------
# 12. APO CLSID -> InprocServer32 DLL -> AuthenticodeSignature -> KB date
# ----------------------------------------------------------------------
Write-Section 'APO CLSID -> DLL -> signing -> Windows Update correlation'

# Walk every CLSID we found in section 2 (audio_endpoints[].all_clsids),
# resolve to the implementing DLL via HKCR\CLSID\<x>\InprocServer32, get
# AuthenticodeSignature + LastWriteTime, and correlate against HotFix
# install date to identify the KB that delivered the APO.
$clsidUniverse = @{}
foreach ($ep in $Report.audio_endpoints) {
    foreach ($c in $ep.all_clsids) {
        $clsidUniverse[$c] = $true
    }
}
foreach ($clsid in $clsidUniverse.Keys) {
    $regPaths = @(
        "Registry::HKEY_CLASSES_ROOT\CLSID\$clsid\InprocServer32",
        "Registry::HKEY_CLASSES_ROOT\WOW6432Node\CLSID\$clsid\InprocServer32"
    )
    $dllPath = $null
    foreach ($rp in $regPaths) {
        if (Test-Path $rp) {
            try {
                $entry = Get-ItemProperty -Path $rp -ErrorAction Stop
                $dllPath = $entry.'(default)'
                if (-not $dllPath) { $dllPath = $entry.PSObject.Properties['(Default)'].Value }
                if ($dllPath) {
                    # Expand env vars in path (e.g. %SystemRoot%\System32\foo.dll).
                    $dllPath = [System.Environment]::ExpandEnvironmentVariables($dllPath)
                    if (Test-Path $dllPath) { break }
                }
            } catch { }
        }
    }
    $row = [ordered]@{
        clsid           = $clsid
        known_label     = if ($KnownClsids.ContainsKey($clsid)) { $KnownClsids[$clsid] } else { '' }
        dll_path        = $dllPath
        dll_exists      = if ($dllPath) { (Test-Path $dllPath) } else { $false }
        file_version    = $null
        product_version = $null
        last_write      = $null
        signing_status  = $null
        signing_subject = $null
        likely_kb       = $null
    }
    if ($dllPath -and (Test-Path $dllPath)) {
        try {
            $info = Get-Item -Path $dllPath -ErrorAction Stop
            $row.last_write = $info.LastWriteTime.ToString('o')
            try {
                $verInfo = $info.VersionInfo
                $row.file_version    = $verInfo.FileVersion
                $row.product_version = $verInfo.ProductVersion
            } catch { }
            try {
                $sig = Get-AuthenticodeSignature -FilePath $dllPath -ErrorAction Stop
                $row.signing_status  = $sig.Status.ToString()
                $row.signing_subject = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { $null }
            } catch { }
            # Correlate dll LastWriteTime against HotFix InstalledOn -- same
            # day (within 1 day window) is strong evidence the KB delivered
            # this DLL.
            $dllDay = $info.LastWriteTime.Date
            foreach ($hf in $Report.hotfixes_recent) {
                if ($hf.installed_on) {
                    $hfDay = ([DateTime]$hf.installed_on).Date
                    $delta = ($dllDay - $hfDay).TotalDays
                    if ([Math]::Abs($delta) -le 1) {
                        $row.likely_kb = $hf.hotfix_id
                        break
                    }
                }
            }
        } catch { }
    }
    $Report.apo_dll_resolution += $row
    $emit = $false
    if ($row.known_label) { $emit = $true }
    if ($row.likely_kb)   { $emit = $true }
    if ($emit) {
        $dllStr = if ($row.dll_path) { $row.dll_path } else { '(unresolved)' }
        Write-Host ("  {0}  ->  {1}" -f $clsid, $dllStr)
        if ($row.known_label)     { Write-Host ("      label: {0}" -f $row.known_label) -ForegroundColor Yellow }
        if ($row.likely_kb)       { Write-Host ("      KB:    {0} (within 1d of dll mtime)" -f $row.likely_kb) -ForegroundColor Cyan }
        if ($row.signing_subject) { Write-Host ("      sig:   {0} ({1})" -f $row.signing_status, $row.signing_subject) }
        if ($row.file_version)    { Write-Host ("      ver:   {0}" -f $row.file_version) }
    }
}
Write-Host ("Resolved {0} CLSIDs (of {1} unique)" -f ($Report.apo_dll_resolution | Where-Object { $_.dll_exists }).Count, $clsidUniverse.Keys.Count)

# ----------------------------------------------------------------------
# 13. ConsentStore (microphone) + Defender exclusions
# ----------------------------------------------------------------------
Write-Section 'Microphone consent + Defender + AppLocker'

# Microphone permission via ConsentStore. Per-app + global kill switch.
$consentRoots = @(
    @{ scope = 'user_global';     path = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone' },
    @{ scope = 'machine_global';  path = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone' }
)
$consentReport = [ordered]@{
    user_global_value     = $null
    machine_global_value  = $null
    nonpackaged_apps      = @()
    packaged_apps         = @()
}
foreach ($root in $consentRoots) {
    if (Test-Path $root.path) {
        try {
            $rootProps = Get-ItemProperty -Path $root.path -ErrorAction Stop
            $val = $rootProps.Value
            if ($root.scope -eq 'user_global')    { $consentReport.user_global_value = $val }
            if ($root.scope -eq 'machine_global') { $consentReport.machine_global_value = $val }
            Write-Host ("  {0}: {1}" -f $root.scope, $val)
        } catch { }
        # NonPackaged apps subkey enumerates Win32 exes that requested mic.
        $nonPkg = Join-Path $root.path 'NonPackaged'
        if (Test-Path $nonPkg) {
            try {
                Get-ChildItem -Path $nonPkg -ErrorAction Stop | ForEach-Object {
                    $appKey = $_
                    try {
                        $props = Get-ItemProperty -Path $appKey.PSPath -ErrorAction Stop
                        $consentReport.nonpackaged_apps += [ordered]@{
                            scope         = $root.scope
                            app_path_enc  = $appKey.PSChildName
                            value         = $props.Value
                            last_used_us  = $props.LastUsedTimeStart
                        }
                    } catch { }
                }
            } catch { }
        }
    }
}
$Report.consent_store = $consentReport
Write-Host ("  NonPackaged apps with mic consent records: {0}" -f $consentReport.nonpackaged_apps.Count)

# Defender preferences -- exclusions can affect onnxruntime model load
# (real-time scan stalls). Get-MpPreference is privileged on some builds.
try {
    $mp = Get-MpPreference -ErrorAction Stop
    $Report.defender_exclusions = [ordered]@{
        exclusion_path      = $mp.ExclusionPath
        exclusion_extension = $mp.ExclusionExtension
        exclusion_process   = $mp.ExclusionProcess
        rt_protection_enabled = -not $mp.DisableRealtimeMonitoring
        scan_avg_cpu_load   = $mp.ScanAvgCPULoadFactor
    }
    Write-Host ("  Defender RT protection: {0}" -f (-not $mp.DisableRealtimeMonitoring))
    Write-Host ("  Path exclusions: {0}" -f $mp.ExclusionPath.Count)
    Write-Host ("  Process exclusions: {0}" -f $mp.ExclusionProcess.Count)
} catch {
    $Report.defender_exclusions = @{ error = "$_" }
    Write-Warning ("Defender query failed: {0}" -f $_)
}

# ----------------------------------------------------------------------
# 14. WASAPI exclusive vs shared comparator probe (live capture)
# ----------------------------------------------------------------------
Write-Section 'Live capture: WASAPI shared vs exclusive (5s each, FFT + Silero)'

# This is the SMOKING GUN test for anti-pattern #21:
#   shared mode = signal goes through APOs (Voice Clarity destroys it)
#   exclusive mode = APOs bypassed
# If shared captures silence and exclusive captures voice → APO confirmed.
#
# Reuses Linux toolkit's analyze_wav.py + silero_probe.py for symmetric
# metrics. Helper script lives next to this PS1 (created in section
# below if absent).
$wasapiHelper = Join-Path $scriptDir 'voice-diag-wasapi-comparator.py'
if (-not (Test-Path $wasapiHelper)) {
    Write-Warning "wasapi comparator helper not found at $wasapiHelper -- section 14 skipped"
    $Report.live_captures += @{ error = 'helper_missing'; path = $wasapiHelper }
} else {
    $captureDir = Join-Path $RawDir 'live_captures'
    if (-not (Test-Path $captureDir)) { New-Item -ItemType Directory -Path $captureDir -Force | Out-Null }

    foreach ($cmd in $pythonCandidates) {
        try {
            $cmpJson = & $cmd $wasapiHelper --outdir $captureDir --duration 5 2>$null
            if ($LASTEXITCODE -eq 0 -and $cmpJson) {
                try {
                    $parsed = $cmpJson | ConvertFrom-Json -ErrorAction Stop
                    $Report.live_captures = $parsed
                    Write-Host 'WASAPI comparator:'
                    foreach ($mode in @('shared','exclusive')) {
                        $r = $parsed.$mode
                        if ($r) {
                            Write-Host ("  [{0,-9}] rms={1,7:N1} dBFS  vad_max={2,5:N3}  ok={3}" -f $mode, $r.rms_dbfs, $r.silero_max_prob, $r.ok)
                        }
                    }
                } catch {
                    $Report.live_captures = @{ error = "parse_failed: $_"; raw = $cmpJson }
                }
                break
            }
        } catch { }
    }
    if (-not $Report.live_captures) {
        $Report.live_captures = @{ error = 'helper_run_failed_or_python_unavailable' }
        Write-Warning 'WASAPI comparator helper did not produce JSON output'
    }
}

# ----------------------------------------------------------------------
# 15. Network reachability to LLM/voice provider endpoints
# ----------------------------------------------------------------------
Write-Section 'Network reachability (LLM/STT/TTS providers)'

$endpoints = @(
    @{ host = 'api.anthropic.com';                    port = 443 },
    @{ host = 'api.openai.com';                       port = 443 },
    @{ host = 'generativelanguage.googleapis.com';    port = 443 },
    @{ host = 'api.deepgram.com';                     port = 443 },
    @{ host = 'api.elevenlabs.io';                    port = 443 }
)
foreach ($ep in $endpoints) {
    $row = [ordered]@{
        host        = $ep.host
        port        = $ep.port
        dns_ok      = $false
        dns_ips     = @()
        tcp_ok      = $false
        rtt_ms      = $null
    }
    try {
        $dns = Resolve-DnsName -Name $ep.host -Type A -ErrorAction Stop -DnsOnly
        $row.dns_ok  = $true
        $row.dns_ips = @($dns | Where-Object { $_.IPAddress } | Select-Object -ExpandProperty IPAddress -First 5)
    } catch { }
    try {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $tcp = Test-NetConnection -ComputerName $ep.host -Port $ep.port -InformationLevel Quiet -WarningAction SilentlyContinue -ErrorAction Stop
        $sw.Stop()
        $row.tcp_ok = [bool]$tcp
        $row.rtt_ms = [int]$sw.Elapsed.TotalMilliseconds
    } catch { }
    $Report.network_llm += $row
    Write-Host ("  {0,-44}  dns={1}  tcp={2}  rtt={3}ms" -f $ep.host, $row.dns_ok, $row.tcp_ok, $row.rtt_ms)
}

# ----------------------------------------------------------------------
# 16. ETW capture (opt-in via -CaptureEtw)
# ----------------------------------------------------------------------
# Windows Performance Recorder (wpr.exe) captures kernel + audio ETW
# providers. The .etl is opaque without WPA, but it's the gold standard
# evidence for audio glitches: per-frame audio engine telemetry, USB
# transfer errors, DPC/ISR latency.
#
# Requires admin. Heavy artifact (~50-200 MB). Opt-in only.
#
# To use Microsoft's audio-specific profile:
#   1. Clone https://github.com/microsoft/audio (MIT)
#   2. Pass -CaptureEtwProfile <path>\audio.wprp
# Otherwise this section uses the built-in GeneralProfile + CPU.
$Report.etw_capture = $null
if ($CaptureEtw) {
    Write-Section ("ETW capture (wpr.exe, {0}s window)" -f $CaptureEtwSeconds)
    $isAdmin = $false
    try {
        $isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
                [Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { }
    if (-not $isAdmin) {
        $Report.etw_capture = @{ ok = $false; reason = 'requires_admin' }
        Write-Warning '  ETW capture requires admin (wpr.exe). Skipped.'
    } else {
        $etlPath = Join-Path $RawDir 'etw_audio_capture.etl'
        $startArgs = @()
        if ($CaptureEtwProfile -and (Test-Path $CaptureEtwProfile)) {
            $startArgs = @('-start', $CaptureEtwProfile)
            Write-Host ("  Using profile: {0}" -f $CaptureEtwProfile)
        } else {
            $startArgs = @('-start', 'GeneralProfile', '-start', 'CPU')
            Write-Host '  Using built-in profiles: GeneralProfile + CPU'
        }
        try {
            # Cancel any running session first.
            & wpr.exe -cancel 2>$null | Out-Null
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            $startResult = & wpr.exe @startArgs 2>&1
            if ($LASTEXITCODE -ne 0) {
                $Report.etw_capture = @{ ok = $false; reason = 'wpr_start_failed';
                                          detail = ($startResult -join "`n") }
                Write-Warning ("  wpr -start failed: {0}" -f ($startResult -join '; '))
            } else {
                Write-Host ("  capturing {0}s..." -f $CaptureEtwSeconds)
                Start-Sleep -Seconds $CaptureEtwSeconds
                $stopResult = & wpr.exe -stop $etlPath 2>&1
                $sw.Stop()
                if ($LASTEXITCODE -eq 0 -and (Test-Path $etlPath)) {
                    $info = Get-Item $etlPath
                    $Report.etw_capture = [ordered]@{
                        ok           = $true
                        profile      = if ($CaptureEtwProfile) { $CaptureEtwProfile } else { 'GeneralProfile+CPU' }
                        duration_s   = $CaptureEtwSeconds
                        elapsed_s    = [math]::Round($sw.Elapsed.TotalSeconds, 1)
                        path         = $etlPath
                        size_bytes   = [int64]$info.Length
                        size_mb      = [math]::Round($info.Length / 1MB, 1)
                    }
                    Write-Host ("  captured {0}: {1} MB" -f $etlPath, $Report.etw_capture.size_mb)
                } else {
                    $Report.etw_capture = @{ ok = $false; reason = 'wpr_stop_failed';
                                              detail = ($stopResult -join "`n") }
                    Write-Warning ("  wpr -stop failed: {0}" -f ($stopResult -join '; '))
                }
            }
        } catch {
            $Report.etw_capture = @{ ok = $false; reason = 'exception';
                                      detail = "$_" }
            try { & wpr.exe -cancel 2>$null | Out-Null } catch { }
        }
    }
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
