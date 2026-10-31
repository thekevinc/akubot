param(
    [string]$Distro = "Ubuntu",
    [string]$LinuxUser = "kc",
    [string]$ProjectPath = "/home/kc/workspace/akubot"
)
$ErrorActionPreference = "Stop"
# This installer runs on Windows. Application data stays in WSL.
foreach ($value in @($Distro, $LinuxUser, $ProjectPath)) {
    if ($value.Contains('"') -or $value.Contains("`r") -or $value.Contains("`n")) {
        throw "Shortcut arguments cannot contain quotes or newlines."
    }
}
$destination = Join-Path $env:LOCALAPPDATA "akubot\launcher"
New-Item -ItemType Directory -Path $destination -Force | Out-Null
$launcher = Join-Path $destination "Start-akubot.ps1"
$icon = Join-Path $destination "akubot.ico"
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Start-akubot.ps1") -Destination $launcher -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "..\akubot\web\favicon.ico") -Destination $icon -Force
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Distro "{1}" -LinuxUser "{2}" -ProjectPath "{3}"' -f $launcher, $Distro, $LinuxUser, $ProjectPath
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    if (-not $folder) { throw "Windows could not locate the shortcut folder." }
    $shortcutPath = Join-Path $folder "akubot.lnk"
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $powershell
    $shortcut.Arguments = $arguments
    $shortcut.WorkingDirectory = $destination
    $shortcut.IconLocation = "$icon,0"
    $shortcut.Description = "Start akubot and open its local dashboard"
    $shortcut.Save()
    Write-Host "Installed $shortcutPath"
}
