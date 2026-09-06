<#
.SYNOPSIS
    Build BareTail into a standalone Windows executable.

.DESCRIPTION
    Produces a single .exe with no installer and no Python required on the
    target machine, which is how the original BareTail is distributed and what
    makes it portable -- copy it to a USB stick or a network share and run it.

    Settings live beside the executable, so the built .exe carries its
    configuration with it wherever it is copied.

.PARAMETER OneDir
    Build a folder instead of a single file. Starts noticeably faster, because
    a one-file build unpacks itself to a temporary directory on every launch.
    Worth it if you open logs from Explorer all day.

.PARAMETER Console
    Keep the console window attached. Off by default -- this is a GUI
    application -- but turn it on if the build misbehaves and you need to see
    a traceback.

.PARAMETER SkipTests
    Do not run the test suite first. The tests take a few seconds and catch a
    broken build before it is packaged, so this is not recommended.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -OneDir
    .\build.ps1 -Console -SkipTests
#>

[CmdletBinding()]
param(
    [switch]$OneDir,
    [switch]$Console,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$name = 'BareTail'

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "    $text" -ForegroundColor Yellow }
function Write-Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }

# Windows PowerShell wraps anything a native program writes to stderr in an
# ErrorRecord, and with ErrorActionPreference = Stop that becomes a terminating
# error. PyInstaller reports all of its ordinary progress on stderr, so running
# it directly would abort a build that is in fact succeeding. Every external
# command therefore runs with the preference relaxed, and success is judged by
# the exit code -- which is the only thing that actually means anything.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$FailureMessage
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0 -and $FailureMessage) { throw $FailureMessage }
    return $code
}

function Test-Native {
    param([string]$Exe, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Get-Native {
    param([string]$Exe, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        return (& $Exe @Arguments | Select-Object -Last 1)
    } finally {
        $ErrorActionPreference = $previous
    }
}

Push-Location $root
try {
    # --- Python -------------------------------------------------------------
    Write-Step 'Checking Python'
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        throw 'python was not found on PATH. Install Python 3.10 or newer and try again.'
    }
    $python = $pythonCmd.Source

    # No quotes inside this expression: PowerShell strips embedded double
    # quotes when handing arguments to a native program, which turned an
    # earlier "%d.%d" formatting version of this line into a SyntaxError.
    $version = Get-Native $python @('-c', 'import sys; print(sys.version.split()[0])')
    Write-Ok "python $version at $python"

    if (-not (Test-Native $python @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'))) {
        throw "Python 3.10 or newer is required; found $version."
    }

    # tkinter ships with python.org builds but is a separate package on some
    # installs, and its absence would only surface when the .exe is run.
    if (-not (Test-Native $python @('-c', 'import tkinter'))) {
        throw 'tkinter is not available in this Python. Reinstall Python with the tcl/tk option enabled.'
    }

    # --- PyInstaller --------------------------------------------------------
    Write-Step 'Checking PyInstaller'
    if (-not (Test-Native $python @('-c', 'import PyInstaller'))) {
        Write-Warn 'PyInstaller not found; installing it'
        Invoke-Native $python @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pyinstaller') `
            'Could not install PyInstaller.' | Out-Null
    }
    $pyiVersion = Get-Native $python @('-c', 'import PyInstaller; print(PyInstaller.__version__)')
    Write-Ok "PyInstaller $pyiVersion"

    # --- Tests --------------------------------------------------------------
    if ($SkipTests) {
        Write-Warn 'Skipping tests (-SkipTests)'
    } else {
        Write-Step 'Running the test suite'
        if (Test-Native $python @('-c', 'import pytest')) {
            Invoke-Native $python @('-m', 'pytest', 'tests/', '-q') `
                'Tests failed. Fix them before building, or pass -SkipTests to build anyway.' | Out-Null
            Write-Ok 'Tests passed'
        } else {
            Write-Warn 'pytest is not installed; skipping tests'
            Write-Note 'Install it with:  python -m pip install pytest'
        }
    }

    # --- Clean --------------------------------------------------------------
    Write-Step 'Cleaning previous build'
    foreach ($dir in @('build', 'dist')) {
        if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    }
    Get-ChildItem -Path $root -Filter '*.spec' -File -ErrorAction SilentlyContinue |
        Remove-Item -Force
    Write-Ok 'build/, dist/ and *.spec removed'

    # --- Build --------------------------------------------------------------
    Write-Step 'Building (this takes a minute)'
    $pyiArgs = @('-m', 'PyInstaller', '--name', $name, '--noconfirm', '--clean')

    if ($OneDir)  { $pyiArgs += '--onedir'  } else { $pyiArgs += '--onefile' }
    if ($Console) { $pyiArgs += '--console' } else { $pyiArgs += '--windowed' }

    # Nothing here needs the heavyweight libraries PyInstaller probes for, and
    # excluding them keeps the executable to a sensible size.
    foreach ($module in @('numpy', 'pandas', 'matplotlib', 'PIL', 'scipy',
                          'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
                          'setuptools', 'pytest')) {
        $pyiArgs += @('--exclude-module', $module)
    }

    $icon = Join-Path $root 'baretail.ico'
    if (Test-Path $icon) {
        $pyiArgs += @('--icon', $icon)
        Write-Ok 'using baretail.ico'
    }

    $pyiArgs += 'baretail.py'
    Invoke-Native $python $pyiArgs 'PyInstaller failed.' | Out-Null

    # --- Report -------------------------------------------------------------
    if ($OneDir) {
        $exe = Join-Path $root "dist\$name\$name.exe"
    } else {
        $exe = Join-Path $root "dist\$name.exe"
    }
    if (-not (Test-Path $exe)) { throw "PyInstaller reported success but $exe is missing." }

    $sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ''
    Write-Step 'Done'
    Write-Ok "$exe  ($sizeMb MB)"
    Write-Host ''
    Write-Note 'Try it:'
    Write-Note "  $exe path\to\some.log"
    Write-Host ''
    Write-Note 'Self-contained: no Python needed on the target machine.'
    Write-Note 'Settings are written beside the executable, so copying it'
    Write-Note 'carries your highlight rules along with it.'
    if (-not $OneDir) {
        Write-Host ''
        Write-Note 'A one-file build unpacks itself on every launch, costing a'
        Write-Note 'second or so of startup. Use -OneDir if that matters more'
        Write-Note 'than having a single file.'
    }
}
finally {
    Pop-Location
}
