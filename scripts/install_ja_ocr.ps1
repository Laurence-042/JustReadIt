#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install the Japanese Windows OCR language capability required by JustReadIt.

.DESCRIPTION
    JustReadIt uses Windows.Media.Ocr for fast bounding-box detection.
    The Japanese OCR capability (Language.OCR~~~ja-JP~0.0.1.0) is a lightweight
    data-only package (~6 MB) downloaded from Windows Update.

    This is NOT the full Japanese display language — it does not change any
    system UI, keyboard layout, or regional settings.

.NOTES
    Requirements:
      - Windows 10 21H1 (19043) or later / Windows 11 any version
      - Administrator privileges (script header enforces this)
      - Internet access to Windows Update (or WSUS / offline cabinet)

    After installation the OCR engine is available immediately without reboot.
#>

$CapabilityName = "Language.OCR~~~ja-JP~0.0.1.0"

Write-Host "JustReadIt — Japanese OCR Capability Installer" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# ── Check current state ──────────────────────────────────────────────────────
Write-Host "Checking capability state..."
$cap = Get-WindowsCapability -Online -Name $CapabilityName -ErrorAction SilentlyContinue

if ($null -eq $cap) {
    Write-Error "Could not query Windows capabilities. Are you on Windows 10 21H1 or later?"
    exit 1
}

if ($cap.State -eq "Installed") {
    Write-Host "  [OK] $CapabilityName is already installed." -ForegroundColor Green
    Write-Host ""
    Write-Host "You can now launch JustReadIt normally."
    exit 0
}

Write-Host "  [--] Current state: $($cap.State)" -ForegroundColor Yellow
Write-Host ""

# ── Install ──────────────────────────────────────────────────────────────────
Write-Host "Installing Japanese OCR language capability..." -ForegroundColor Cyan
Write-Host "  (downloading ~6 MB from Windows Update)"
Write-Host ""

try {
    $result = Add-WindowsCapability -Online -Name $CapabilityName -ErrorAction Stop

    if ($result.RestartNeeded) {
        Write-Host "  [OK] Installed. A restart is recommended (but not required for OCR)." -ForegroundColor Green
    } else {
        Write-Host "  [OK] Installed successfully. No restart needed." -ForegroundColor Green
    }
} catch {
    Write-Error "Installation failed: $_"
    Write-Host ""
    Write-Host "Possible causes:" -ForegroundColor Yellow
    Write-Host "  - No internet access / Windows Update blocked by policy"
    Write-Host "  - Group Policy restricts optional feature installation"
    Write-Host ""
    Write-Host "Manual alternative: run this command in an elevated PowerShell:"
    Write-Host "  Add-WindowsCapability -Online -Name '$CapabilityName'" -ForegroundColor Cyan
    exit 1
}

Write-Host ""
Write-Host "Verifying installation..."
$cap = Get-WindowsCapability -Online -Name $CapabilityName
if ($cap.State -eq "Installed") {
    Write-Host "  [OK] Verified. Japanese OCR is ready." -ForegroundColor Green
} else {
    Write-Warning "Capability state is '$($cap.State)' after install. Try running JustReadIt anyway."
}

Write-Host ""
Write-Host "Done. You can now launch JustReadIt." -ForegroundColor Cyan
