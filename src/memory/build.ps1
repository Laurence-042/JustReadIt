<#
.SYNOPSIS
    Build mem_scan.dll (x64) from mem_scan.c using MSVC.

.DESCRIPTION
    Requires "x64 Native Tools Command Prompt for VS" (or vcvarsall x64 already run).
    Outputs mem_scan.dll into src\memory\.

    No external dependencies — pure C with CRT only.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Debug
#>

param(
    [switch]$Debug
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path   # src\memory
$Src       = Join-Path $ScriptDir "mem_scan.c"
$OutDll    = Join-Path $ScriptDir "mem_scan.dll"

if (-not (Test-Path $Src)) { throw "Source not found: $Src" }

if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    throw "cl.exe not found in PATH.`nRun this script from 'x64 Native Tools Command Prompt for VS'."
}

Push-Location $ScriptDir
try {
    $optFlags = if ($Debug) { @("/Od", "/Zi") } else { @("/O2", "/GS-") }

    $clArgs = @(
        "/LD",
        @($optFlags),
        "/W3",
        $Src,
        "/Fe:$OutDll",
        "/link",
        "/SUBSYSTEM:WINDOWS",
        "/DLL"
    )

    Write-Host "cl.exe $($clArgs -join ' ')"
    & cl.exe @clArgs

    if ($LASTEXITCODE -ne 0) { throw "Compilation failed (exit $LASTEXITCODE)" }

    Write-Host "`n[OK] mem_scan.dll -> $OutDll"
}
finally {
    Pop-Location
}
