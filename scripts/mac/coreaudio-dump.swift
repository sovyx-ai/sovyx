#!/usr/bin/env swift
// coreaudio-dump — emite JSON com enumeração completa de devices CoreAudio
// + propriedades por device (formato, transport, latência, sample rates,
// data source, IsAlive, IsRunning).
//
// Uso (macOS 10.13+):
//   swift coreaudio-dump.swift > coreaudio_dump.json
//
// Output JSON shape (single-line per top-level key):
// {
//   "ok": true,
//   "system_default_input": { ... },
//   "system_default_output": { ... },
//   "devices": [ { ... }, ... ],
//   "tool_version": "1.0",
//   "errors": []
// }
//
// Cada device:
//   { uid, name, manufacturer, transport_type, transport_type_name,
//     is_alive, is_running, is_running_somewhere,
//     input_streams: [...], output_streams: [...],
//     available_sample_rates: [...], current_sample_rate,
//     buffer_frame_size, buffer_frame_size_range, latency_input_frames,
//     latency_output_frames, data_source_input, data_source_output }
//
// Stream:
//   { stream_id, direction, format: { sample_rate, channels, bits_per_channel,
//     bytes_per_frame, format_id, format_id_name, format_flags } }
//
// Equivalente Linux ao toolkit: pactl + pw-dump + arecord -L combinados
// num único dump. Permite ao analista cross-correlate com PortAudio enum.

import Foundation
import CoreAudio
import AudioToolbox

let TOOL_VERSION = "1.0"
var errors: [String] = []

// ===================================================================
// Helpers — wrap CoreAudio C API into Swift
// ===================================================================

func getProperty<T>(deviceID: AudioObjectID,
                    selector: AudioObjectPropertySelector,
                    scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                    element: AudioObjectPropertyElement = kAudioObjectPropertyElementMain,
                    defaultVal: T) -> T {
    var addr = AudioObjectPropertyAddress(mSelector: selector,
                                           mScope: scope,
                                           mElement: element)
    var size = UInt32(MemoryLayout<T>.size)
    var value = defaultVal
    let status = AudioObjectGetPropertyData(deviceID, &addr, 0, nil, &size, &value)
    if status != noErr {
        errors.append("getProperty selector=\(selector) status=\(status)")
    }
    return value
}

func getStringProperty(deviceID: AudioObjectID,
                       selector: AudioObjectPropertySelector,
                       scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> String? {
    var addr = AudioObjectPropertyAddress(mSelector: selector,
                                           mScope: scope,
                                           mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    let sizeStatus = AudioObjectGetPropertyDataSize(deviceID, &addr, 0, nil, &size)
    if sizeStatus != noErr || size == 0 { return nil }
    var cfStr: Unmanaged<CFString>?
    let status = AudioObjectGetPropertyData(deviceID, &addr, 0, nil, &size, &cfStr)
    if status != noErr {
        errors.append("getStringProperty selector=\(selector) status=\(status)")
        return nil
    }
    return cfStr?.takeRetainedValue() as String?
}

func getArrayProperty<T>(deviceID: AudioObjectID,
                         selector: AudioObjectPropertySelector,
                         scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                         elementType: T.Type) -> [T] {
    var addr = AudioObjectPropertyAddress(mSelector: selector,
                                           mScope: scope,
                                           mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    let sizeStatus = AudioObjectGetPropertyDataSize(deviceID, &addr, 0, nil, &size)
    if sizeStatus != noErr || size == 0 { return [] }
    let count = Int(size) / MemoryLayout<T>.size
    var buffer = [T](repeating: unsafeBitCast(0 as UInt64, to: T.self), count: count)
    let status = buffer.withUnsafeMutableBufferPointer { ptr in
        AudioObjectGetPropertyData(deviceID, &addr, 0, nil, &size, ptr.baseAddress!)
    }
    if status != noErr {
        errors.append("getArrayProperty selector=\(selector) status=\(status)")
        return []
    }
    return buffer
}

func fourCharCodeToString(_ code: UInt32) -> String {
    let bytes: [UInt8] = [
        UInt8((code >> 24) & 0xFF),
        UInt8((code >> 16) & 0xFF),
        UInt8((code >> 8) & 0xFF),
        UInt8(code & 0xFF)
    ]
    return String(bytes: bytes, encoding: .ascii) ?? "????"
}

func transportTypeName(_ type: UInt32) -> String {
    switch type {
    case kAudioDeviceTransportTypeBuiltIn:    return "BuiltIn"
    case kAudioDeviceTransportTypeAggregate:  return "Aggregate"
    case kAudioDeviceTransportTypeAutoAggregate: return "AutoAggregate"
    case kAudioDeviceTransportTypeVirtual:    return "Virtual"
    case kAudioDeviceTransportTypePCI:        return "PCI"
    case kAudioDeviceTransportTypeUSB:        return "USB"
    case kAudioDeviceTransportTypeFireWire:   return "FireWire"
    case kAudioDeviceTransportTypeBluetooth:  return "Bluetooth"
    case kAudioDeviceTransportTypeBluetoothLE:return "BluetoothLE"
    case kAudioDeviceTransportTypeHDMI:       return "HDMI"
    case kAudioDeviceTransportTypeDisplayPort:return "DisplayPort"
    case kAudioDeviceTransportTypeAirPlay:    return "AirPlay"
    case kAudioDeviceTransportTypeAVB:        return "AVB"
    case kAudioDeviceTransportTypeThunderbolt:return "Thunderbolt"
    case kAudioDeviceTransportTypeContinuityCaptureWired: return "ContinuityCaptureWired"
    case kAudioDeviceTransportTypeContinuityCaptureWireless: return "ContinuityCaptureWireless"
    case kAudioDeviceTransportTypeUnknown:    return "Unknown"
    default: return "Other(\(type))"
    }
}

// ===================================================================
// Device enumeration
// ===================================================================

func enumerateDevices() -> [AudioObjectID] {
    return getArrayProperty(deviceID: AudioObjectID(kAudioObjectSystemObject),
                            selector: kAudioHardwarePropertyDevices,
                            elementType: AudioObjectID.self)
}

func defaultInputDeviceID() -> AudioObjectID {
    return getProperty(deviceID: AudioObjectID(kAudioObjectSystemObject),
                       selector: kAudioHardwarePropertyDefaultInputDevice,
                       defaultVal: AudioObjectID(0))
}

func defaultOutputDeviceID() -> AudioObjectID {
    return getProperty(deviceID: AudioObjectID(kAudioObjectSystemObject),
                       selector: kAudioHardwarePropertyDefaultOutputDevice,
                       defaultVal: AudioObjectID(0))
}

// Inspect single device.
func inspectDevice(_ deviceID: AudioObjectID) -> [String: Any] {
    var dev: [String: Any] = ["device_id": deviceID]
    dev["uid"]          = getStringProperty(deviceID: deviceID, selector: kAudioDevicePropertyDeviceUID) ?? ""
    dev["name"]         = getStringProperty(deviceID: deviceID, selector: kAudioObjectPropertyName) ?? ""
    dev["manufacturer"] = getStringProperty(deviceID: deviceID, selector: kAudioObjectPropertyManufacturer) ?? ""
    dev["model_uid"]    = getStringProperty(deviceID: deviceID, selector: kAudioDevicePropertyModelUID) ?? ""
    dev["icon_url"]     = getStringProperty(deviceID: deviceID, selector: kAudioDevicePropertyIcon) ?? ""

    let transportType: UInt32 = getProperty(deviceID: deviceID,
                                             selector: kAudioDevicePropertyTransportType,
                                             defaultVal: UInt32(0))
    dev["transport_type"]      = transportType
    dev["transport_type_name"] = transportTypeName(transportType)

    let isAlive: UInt32 = getProperty(deviceID: deviceID,
                                       selector: kAudioDevicePropertyDeviceIsAlive,
                                       defaultVal: UInt32(0))
    dev["is_alive"] = isAlive != 0

    let isRunning: UInt32 = getProperty(deviceID: deviceID,
                                         selector: kAudioDevicePropertyDeviceIsRunning,
                                         defaultVal: UInt32(0))
    dev["is_running"] = isRunning != 0

    let isRunningSomewhere: UInt32 = getProperty(
        deviceID: deviceID,
        selector: kAudioDevicePropertyDeviceIsRunningSomewhere,
        defaultVal: UInt32(0)
    )
    dev["is_running_somewhere"] = isRunningSomewhere != 0

    // Sample rate (current + available).
    let currentRate: Float64 = getProperty(deviceID: deviceID,
                                            selector: kAudioDevicePropertyNominalSampleRate,
                                            defaultVal: Float64(0))
    dev["current_sample_rate"] = currentRate

    // Available rates come as AudioValueRange struct array.
    let rateRanges: [AudioValueRange] = getArrayProperty(
        deviceID: deviceID,
        selector: kAudioDevicePropertyAvailableNominalSampleRates,
        elementType: AudioValueRange.self
    )
    dev["available_sample_rates"] = rateRanges.map { ["min": $0.mMinimum, "max": $0.mMaximum] }

    // Buffer frame size.
    let bufFrames: UInt32 = getProperty(deviceID: deviceID,
                                         selector: kAudioDevicePropertyBufferFrameSize,
                                         defaultVal: UInt32(0))
    dev["buffer_frame_size"] = bufFrames

    let bufRange: AudioValueRange = getProperty(
        deviceID: deviceID,
        selector: kAudioDevicePropertyBufferFrameSizeRange,
        defaultVal: AudioValueRange(mMinimum: 0, mMaximum: 0)
    )
    dev["buffer_frame_size_range"] = ["min": bufRange.mMinimum, "max": bufRange.mMaximum]

    // Per-scope (input + output) properties.
    for (scope, scopeName) in [
        (kAudioObjectPropertyScopeInput, "input"),
        (kAudioObjectPropertyScopeOutput, "output")
    ] {
        // Latency in frames.
        let latency: UInt32 = getProperty(deviceID: deviceID,
                                           selector: kAudioDevicePropertyLatency,
                                           scope: scope,
                                           defaultVal: UInt32(0))
        dev["latency_\(scopeName)_frames"] = latency

        // Stream format (current).
        let fmt: AudioStreamBasicDescription = getProperty(
            deviceID: deviceID,
            selector: kAudioDevicePropertyStreamFormat,
            scope: scope,
            defaultVal: AudioStreamBasicDescription()
        )
        if fmt.mSampleRate > 0 {
            dev["stream_format_\(scopeName)"] = [
                "sample_rate": fmt.mSampleRate,
                "channels": fmt.mChannelsPerFrame,
                "bits_per_channel": fmt.mBitsPerChannel,
                "bytes_per_frame": fmt.mBytesPerFrame,
                "format_id": fmt.mFormatID,
                "format_id_name": fourCharCodeToString(fmt.mFormatID),
                "format_flags": fmt.mFormatFlags
            ]
        }

        // Data source (mic source: BuiltInMic, ExtMic, etc.).
        let dataSource: UInt32 = getProperty(
            deviceID: deviceID,
            selector: kAudioDevicePropertyDataSource,
            scope: scope,
            defaultVal: UInt32(0)
        )
        if dataSource != 0 {
            dev["data_source_\(scopeName)"] = fourCharCodeToString(dataSource)
        }
    }

    return dev
}

// ===================================================================
// Main
// ===================================================================

var output: [String: Any] = [
    "ok": true,
    "tool_version": TOOL_VERSION,
    "captured_at_utc": ISO8601DateFormatter().string(from: Date())
]

let allDevices = enumerateDevices()
let defIn  = defaultInputDeviceID()
let defOut = defaultOutputDeviceID()

output["device_count"] = allDevices.count
output["system_default_input_id"]  = defIn
output["system_default_output_id"] = defOut

var devicesArray: [[String: Any]] = []
for devID in allDevices {
    devicesArray.append(inspectDevice(devID))
}
output["devices"] = devicesArray

if defIn != 0 {
    output["system_default_input"] = inspectDevice(defIn)
}
if defOut != 0 {
    output["system_default_output"] = inspectDevice(defOut)
}

output["errors"] = errors

// Serialize as JSON.
do {
    let data = try JSONSerialization.data(
        withJSONObject: output,
        options: [.prettyPrinted, .sortedKeys]
    )
    if let str = String(data: data, encoding: .utf8) {
        print(str)
    } else {
        FileHandle.standardError.write("JSON encoding failed\n".data(using: .utf8)!)
        exit(1)
    }
} catch {
    FileHandle.standardError.write("JSON serialization error: \(error)\n".data(using: .utf8)!)
    exit(1)
}
