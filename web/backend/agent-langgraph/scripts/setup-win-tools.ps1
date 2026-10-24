# Install optional packaging CLI tools on Windows via Chocolatey.
# Used by: pnpm run setup:tools
#Requires -Version 5.1
$ErrorActionPreference = "Stop"

$packages = @(
    @{ Tool = "rsync"; Package = "rsync" },
    @{ Tool = "zip";  Package = "zip" }
)

function Test-Tool([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    Write-Error @"
Chocolatey is not installed.

Install Chocolatey first (admin PowerShell):
  Set-ExecutionPolicy Bypass -Scope Process -Force
  [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
  iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

Then re-run: pnpm run setup:tools
"@
    exit 1
}

$installed = 0
foreach ($entry in $packages) {
    if (Test-Tool $entry.Tool) {
        Write-Host "OK  $($entry.Tool) already on PATH"
        continue
    }
    Write-Host "Installing $($entry.Package) via Chocolatey ..."
    & choco install $entry.Package -y --no-progress
    if ($LASTEXITCODE -ne 0) {
        Write-Error "choco install $($entry.Package) failed (exit $LASTEXITCODE)"
        exit $LASTEXITCODE
    }
    $installed++
}

if ($installed -gt 0) {
    Write-Host ""
    Write-Host "Installed $installed package(s). Open a NEW terminal so PATH updates take effect."
    Write-Host "Then verify: pnpm run check:tools"
} else {
    Write-Host ""
    Write-Host "All packaging tools are already available."
}
