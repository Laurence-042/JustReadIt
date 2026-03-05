<#
.SYNOPSIS
    Build hook_engine.dll (x64) from hook_engine.c using MSVC.

.DESCRIPTION
    Requires "x64 Native Tools Command Prompt for VS" (or vcvarsall x64 already run).
    Outputs hook_engine.dll into src\hook\.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Debug
#>

param(
    [switch]$Debug
)

$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path   # src\hook
$RepoRoot    = Split-Path -Parent (Split-Path -Parent $ScriptDir)  # repo root
$MinHookDir  = Join-Path $RepoRoot "3rd\hook\MinHook_134_bin"
$IncludeDir  = Join-Path $MinHookDir "include"
$LibFile     = Join-Path $MinHookDir "bin\MinHook.x64.lib"
$Src         = Join-Path $ScriptDir "hook_engine.c"
$OutDll      = Join-Path $ScriptDir "hook_engine.dll"

if (-not (Test-Path $Src))     { throw "Source not found: $Src" }
if (-not (Test-Path $LibFile)) { throw "MinHook lib not found: $LibFile" }

# Check that cl.exe is in PATH (requires x64 Developer Command Prompt)
if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    throw "cl.exe not found in PATH.`nRun this script from 'x64 Native Tools Command Prompt for VS'."
}

Push-Location $ScriptDir
try {
    $optFlags = if ($Debug) { @("/Od", "/Zi") } else { @("/O2", "/GS-") }

    $clArgs = @(
        "/LD",                      # build DLL
        @($optFlags),              # each flag as its own argument
        "/W3",
        $Src,
        "/I", $IncludeDir,
        "/Fe:$OutDll",
        "/link",
        $LibFile,
        "psapi.lib",
        "/SUBSYSTEM:WINDOWS",
        "/DLL"
    )

    Write-Host "cl.exe $($clArgs -join ' ')"
    & cl.exe @clArgs

    if ($LASTEXITCODE -ne 0) { throw "Compilation failed (exit $LASTEXITCODE)" }

    Write-Host "`n[OK] hook_engine.dll -> $OutDll"

    # Also copy MinHook.x64.dll next to the output so it is found at injection time
    $MhDll = Join-Path $MinHookDir "bin\MinHook.x64.dll"
    if (Test-Path $MhDll) {
        Copy-Item $MhDll (Join-Path $ScriptDir "MinHook.x64.dll") -Force
        Write-Host "[OK] MinHook.x64.dll copied to $ScriptDir"
    }
}
finally {
    Pop-Location
}
