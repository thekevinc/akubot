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
$iconSource = Join-Path $PSScriptRoot "..\akubot\web\favicon.ico"
# A new name avoids stale Windows thumbnail-cache entries after an icon update.
$iconHash = (Get-FileHash -LiteralPath $iconSource -Algorithm SHA256).Hash.Substring(0, 12).ToLowerInvariant()
$icon = Join-Path $destination "akubot-$iconHash.ico"
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Start-akubot.ps1") -Destination $launcher -Force
Copy-Item -LiteralPath $iconSource -Destination $icon -Force
if (-not ("Akubot.ShortcutRefresh" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace Akubot {
    public static class ShortcutRefresh {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        public static extern void SHChangeNotify(uint eventId, uint flags, string item, IntPtr secondItem);
    }
}
'@
}
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
    [Akubot.ShortcutRefresh]::SHChangeNotify(0x2000, 0x0005, $shortcutPath, [IntPtr]::Zero)
    Write-Host "Installed $shortcutPath"
}
