param(
    [string]$Distro = "Ubuntu",
    [string]$LinuxUser = "kc",
    [string]$ProjectPath = "/home/kc/workspace/akubot",
    [switch]$CheckOnly
)
$ErrorActionPreference = "Stop"
try {
    foreach ($value in @($Distro, $LinuxUser, $ProjectPath)) {
        if ($value.Contains('"') -or $value.Contains("`r") -or $value.Contains("`n") -or $value.Contains('\')) {
            throw "The WSL startup settings contain an unsupported character."
        }
    }
    if (-not $CheckOnly -and -not (Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue)) {
        $dockerDesktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
        if (-not (Test-Path -LiteralPath $dockerDesktop)) {
            throw "Docker Desktop was not found. Install it before starting akubot."
        }
        Write-Output "Opening Docker Desktop..."
        Start-Process -FilePath $dockerDesktop
    }
    # Start WSL directly. Native-command invocation can silently do nothing in
    # some Windows/WSL PowerShell environments.
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path $env:SystemRoot "System32\wsl.exe"
    function WslArgument([string]$value) {
        if ($value -match '\s') { return '"' + $value + '"' }
        return $value
    }
    $info.Arguments = '--distribution {0} --user {1} --cd {2} --exec python3 {3}' -f (WslArgument $Distro), (WslArgument $LinuxUser), (WslArgument $ProjectPath), (WslArgument "$ProjectPath/scripts/start_bot.py")
    if ($CheckOnly) { $info.Arguments += ' --check' }
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    if (-not $process.Start()) { throw "Windows could not start WSL." }
    # Drain stderr concurrently so neither redirected stream can block startup.
    $errorTask = $process.StandardError.ReadToEndAsync()
    $dashboard = $null
    while ($null -ne ($line = $process.StandardOutput.ReadLine())) {
        $line = $line.Replace([string][char]0, "")
        if ($line -match '^AKUBOT_DASHBOARD=(http://localhost:[0-9]+/)$') {
            $dashboard = $Matches[1]
        } else {
            Write-Output $line
        }
    }
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    $errorOutput = $errorTask.GetAwaiter().GetResult().Replace([string][char]0, "")
    $process.Dispose()
    if ($exitCode -ne 0) {
        if ($errorOutput.Trim()) { Write-Output $errorOutput.Trim() }
        throw "Startup did not finish. Review the message above and check Docker Desktop."
    }
    if ($CheckOnly) { exit 0 }
    if (-not $dashboard) { throw "The dashboard did not report that it was ready." }
    Start-Process $dashboard
    exit 0
} catch {
    Write-Output "akubot could not start: $($_.Exception.Message)"
    exit 1
}
