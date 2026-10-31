param(
    [string]$Distro = "Ubuntu",
    [string]$LinuxUser = "kc",
    [string]$ProjectPath = "/home/kc/workspace/akubot"
)
$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "akubot"
try {
    if (-not (Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue)) {
        $dockerDesktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
        if (-not (Test-Path -LiteralPath $dockerDesktop)) {
            throw "Docker Desktop was not found. Install it before starting akubot."
        }
        Write-Host "Opening Docker Desktop..."
        Start-Process -FilePath $dockerDesktop
    }
    $dashboard = $null
    & "$env:SystemRoot\System32\wsl.exe" --distribution $Distro --user $LinuxUser --cd $ProjectPath --exec python3 "$ProjectPath/scripts/start_bot.py" | ForEach-Object {
        $line = [string]$_
        if ($line -match '^AKUBOT_DASHBOARD=(http://localhost:[0-9]+/)$') {
            $dashboard = $Matches[1]
        } else {
            Write-Host $line
        }
    }
    if ($LASTEXITCODE -ne 0 -or -not $dashboard) {
        throw "Startup did not finish. Review the message above and check Docker Desktop."
    }
    Start-Process $dashboard
} catch {
    Write-Host "akubot could not start: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}
