$ErrorActionPreference = 'Stop'
$adb = Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe'
if (-not (Test-Path $adb)) { throw "adb не найден: $adb" }

$devices = & $adb devices
if (($devices | Select-String "`tdevice$").Count -ne 1) {
    throw 'Подключите ровно одно разблокированное Android-устройство с USB debugging.'
}

$properties = @(
    'ro.product.manufacturer', 'ro.product.model', 'ro.product.device',
    'ro.build.version.release', 'ro.build.version.sdk', 'ro.build.version.incremental',
    'ro.build.fingerprint', 'ro.miui.ui.version.name', 'ro.miui.region'
)

'=== DEVICE ==='
foreach ($property in $properties) {
    $value = & $adb shell getprop $property
    "$property=$value"
}

'=== DEFAULT DIALER ==='
& $adb shell cmd role get-role-holders android.app.role.DIALER

'=== TELECOM ==='
& $adb shell dumpsys telecom

'=== RECORDING PACKAGES ==='
& $adb shell pm list packages | Select-String 'dialer|phone|contacts|soundrecorder|recorder'

'=== KNOWN MIUI RECORDING DIRECTORIES ==='
foreach ($path in @('/sdcard/MIUI/sound_recorder/call_rec', '/sdcard/MIUI/sound_recorder', '/sdcard/Recordings/Call')) {
    "-- $path"
    & $adb shell ls -la $path 2>&1
}

'=== MO54 PERMISSIONS (after installation) ==='
& $adb shell dumpsys package ru.mo54.calls.debug | Select-String 'READ_PHONE_STATE|READ_CALL_LOG|RECORD_AUDIO|POST_NOTIFICATIONS'

