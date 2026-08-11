param(
    [string]$ReleaseApk = "../../android/app/build/outputs/apk/release/app-release.apk",
    [string]$OutputZip = "../../diagnostics/artifacts/mo54-calls-privapp-magisk.zip"
)

$ErrorActionPreference = "Stop"
$moduleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$apkSource = Resolve-Path (Join-Path $moduleRoot $ReleaseApk)
$apkTargetDir = Join-Path $moduleRoot "system/priv-app/MO54Calls"
$apkTarget = Join-Path $apkTargetDir "MO54Calls.apk"
$zipTarget = Join-Path $moduleRoot $OutputZip
$zipTargetDir = Split-Path -Parent $zipTarget

New-Item -ItemType Directory -Force -Path $apkTargetDir | Out-Null
New-Item -ItemType Directory -Force -Path $zipTargetDir | Out-Null
Copy-Item -Force -LiteralPath $apkSource -Destination $apkTarget

if (Test-Path -LiteralPath $zipTarget) {
    Remove-Item -LiteralPath $zipTarget -Force
}

[System.Reflection.Assembly]::LoadWithPartialName("System.IO.Compression") | Out-Null
[System.Reflection.Assembly]::LoadWithPartialName("System.IO.Compression.FileSystem") | Out-Null
$zip = [System.IO.Compression.ZipFile]::Open($zipTarget, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    $files = @(
        Join-Path $moduleRoot "module.prop"
        Join-Path $moduleRoot "README.md"
    ) + (Get-ChildItem -LiteralPath (Join-Path $moduleRoot "system") -File -Recurse | ForEach-Object { $_.FullName })

    foreach ($file in $files) {
        $rootPrefix = $moduleRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
        $relative = $file.Substring($rootPrefix.Length).Replace("\", "/")
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file, $relative, [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
} finally {
    $zip.Dispose()
}
Write-Host "Wrote $zipTarget"
