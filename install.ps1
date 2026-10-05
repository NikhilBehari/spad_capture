<#
One-command setup for spad_capture on Windows (macOS and Linux: install.sh).

  powershell -ExecutionPolicy Bypass -File install.ps1               # env + package + Realsense
  powershell -ExecutionPolicy Bypass -File install.ps1 -NoCamera     # skip the Realsense binding
  powershell -ExecutionPolicy Bypass -File install.ps1 -Flash tmf    # also flash the board (plugged in)

Options: -Name <env> (default spad_capture), -NoCamera, -Flash tmf|st, -Yes (no prompts).
Safe to re-run: an existing env is reused and the package reinstalled into it.
Written for Windows PowerShell 5.1, the version every Windows ships with.
#>
[CmdletBinding()]
param(
  [string]$Name = 'spad_capture',
  [switch]$NoCamera,
  [ValidateSet('tmf', 'st')][string]$Flash,
  [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # the progress bar slows Invoke-WebRequest many times over
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$RepoDir = $PSScriptRoot
# Miniforge refuses install paths with spaces or ^%!=,() (e.g. C:\Users\Jane Doe),
# so such profiles fall back to the system drive.
$MiniforgeDir = Join-Path $env:USERPROFILE 'miniforge3'
if ($MiniforgeDir -match '[ ^%!=,()]') { $MiniforgeDir = Join-Path $env:SystemDrive 'miniforge3' }
$MiniforgeUrl = 'https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe'
$ArduinoCliUrl = 'https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Windows_64bit.zip'

function Step([string]$Message) { Write-Host ''; Write-Host "==> $Message" -ForegroundColor Cyan }
function Info([string]$Message) { Write-Host "    $Message" }
function Die([string]$Message) { Write-Host ''; Write-Host "error: $Message" -ForegroundColor Red; exit 1 }

# Runs a program, streams its output, and returns its exit code. Windows
# PowerShell turns redirected stderr into terminating errors under 'Stop', so
# native programs run under 'Continue' and are judged by exit code alone.
function Invoke-Native([string]$Exe, [string[]]$Arguments, [switch]$Quiet) {
  if (-not (Test-Path -LiteralPath $Exe)) { return 9009 }   # cmd's "not recognized" code
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    if ($Quiet) { & $Exe @Arguments *> $null } else { & $Exe @Arguments | Out-Host }
    return $LASTEXITCODE
  } finally { $ErrorActionPreference = $previous }
}

# Same, but returns stdout as one string, or $null on failure.
function Get-NativeOutput([string]$Exe, [string[]]$Arguments) {
  if (-not (Test-Path -LiteralPath $Exe)) { return $null }
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & $Exe @Arguments 2> $null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($out -join "`n")
  } finally { $ErrorActionPreference = $previous }
}

function Assert-Windows {
  if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) {
    Die 'install.ps1 is for Windows; on macOS and Linux run ./install.sh'
  }
}

function Find-Conda {
  # conda.exe over the conda.bat shims that also sit on PATH
  $onPath = @(Get-Command conda.exe, conda -CommandType Application -ErrorAction SilentlyContinue) |
    Select-Object -First 1 -ExpandProperty Source
  $candidates = @(
    $env:CONDA_EXE,
    $onPath,
    (Join-Path $env:USERPROFILE 'miniforge3\Scripts\conda.exe'),
    (Join-Path $env:USERPROFILE 'mambaforge\Scripts\conda.exe'),
    (Join-Path $env:USERPROFILE 'miniconda3\Scripts\conda.exe'),
    (Join-Path $env:USERPROFILE 'anaconda3\Scripts\conda.exe'),
    (Join-Path $env:LOCALAPPDATA 'miniforge3\Scripts\conda.exe'),
    (Join-Path $env:LOCALAPPDATA 'miniconda3\Scripts\conda.exe'),
    (Join-Path $env:LOCALAPPDATA 'anaconda3\Scripts\conda.exe'),
    (Join-Path $env:ProgramData 'miniforge3\Scripts\conda.exe'),
    (Join-Path $env:ProgramData 'miniconda3\Scripts\conda.exe'),
    (Join-Path $env:ProgramData 'anaconda3\Scripts\conda.exe')
  )
  foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate) -and
        (Invoke-Native $candidate @('--version') -Quiet) -eq 0) {
      return $candidate
    }
  }
  return $null
}

function Install-Miniforge {
  if (Test-Path -LiteralPath $MiniforgeDir) {
    Die "no working conda found, and $MiniforgeDir already exists. Fix or remove it, then re-run."
  }
  if (-not $Yes) {
    try { $reply = Read-Host "    No conda found. Install Miniforge into ${MiniforgeDir}? [y/N]" }
    catch { Die "no conda found and no console to ask on. Re-run with -Yes to install Miniforge into $MiniforgeDir." }
    if ($reply -notmatch '^(y|yes)$') {
      Die 'conda is required. Install Miniforge (https://github.com/conda-forge/miniforge) and re-run.'
    }
  }
  $installer = Join-Path $env:TEMP 'Miniforge3-Windows-x86_64.exe'
  Info 'downloading Miniforge3-Windows-x86_64.exe'
  try { Invoke-WebRequest -Uri $MiniforgeUrl -OutFile $installer -UseBasicParsing }
  catch { Die "could not download Miniforge; check the network and re-run. ($($_.Exception.Message))" }
  try {
    # NSIS silent install; /D must come last and unquoted.
    $proc = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
      '/S', '/InstallationType=JustMe', '/RegisterPython=0', '/AddToPath=0', "/D=$MiniforgeDir")
    if ($proc.ExitCode -ne 0) { Die "Miniforge installer failed (exit $($proc.ExitCode))." }
  } finally { Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue }
  $conda = Join-Path $MiniforgeDir 'Scripts\conda.exe'
  Info "installed. To use conda in new terminals: & '$conda' init powershell"
  return $conda
}

function Get-EnvPrefix([string]$Conda) {
  $json = Get-NativeOutput $Conda @('env', 'list', '--json')
  if (-not $json) { Die 'could not list conda environments.' }
  # Parse only the "envs" array: newer conda adds an envs_details object keyed by
  # prefix, and PowerShell 5.1 rejects it when a prefix is listed twice in
  # different case (C:\ and c:\), as conda does on Windows.
  if ($json -notmatch '"envs"\s*:\s*\[[^\]]*\]') { Die 'could not parse conda env list --json.' }
  try { $envs = ("{$($Matches[0])}" | ConvertFrom-Json).envs } catch { Die 'could not parse conda env list --json.' }
  foreach ($prefix in $envs) {
    if ((Split-Path $prefix -Leaf) -eq $Name) { return $prefix }
  }
  return $null
}

function Initialize-Env([string]$Conda) {
  $prefix = Get-EnvPrefix $Conda
  if ($prefix) {
    Info "reusing existing env '$Name'"
  } else {
    $code = Invoke-Native $Conda @('env', 'create', '-n', $Name, '-f', (Join-Path $RepoDir 'environment.yml'))
    if ($code -ne 0) {
      Die "could not create env '$Name' from environment.yml. If an earlier attempt left it half-made, remove it (conda env remove -n $Name) and re-run."
    }
    $prefix = Get-EnvPrefix $Conda
    if (-not $prefix) { Die "env '$Name' was created but conda does not list it." }
  }
  if (-not (Test-Path -LiteralPath (Join-Path $prefix 'python.exe'))) {
    Die "env '$Name' exists but has no python.exe."
  }
  Info "env at $prefix"
  return $prefix
}

# PyPI ships pyrealsense2 wheels for Windows, so the [rgb] extra covers the camera.
function Install-Package([string]$Prefix) {
  $target = if ($NoCamera) { '.' } else { '.[rgb]' }
  Push-Location -LiteralPath $RepoDir
  try {
    $code = Invoke-Native (Join-Path $Prefix 'python.exe') @(
      '-m', 'pip', 'install', '--disable-pip-version-check', '-e', $target)
  } finally { Pop-Location }
  if ($code -ne 0) {
    Die 'pip install of spad_capture failed (see output above). If pyrealsense2 is the failure, re-run with -NoCamera.'
  }
}

# spad's own arduino-cli bootstrap needs a POSIX shell, so on Windows the
# installer drops arduino-cli.exe into the env's Scripts dir, on PATH once activated.
function Install-ArduinoCli([string]$Prefix) {
  $exe = Join-Path $Prefix 'Scripts\arduino-cli.exe'
  if (Test-Path -LiteralPath $exe) { Info 'arduino-cli already in the env'; return }
  if (Get-Command arduino-cli -CommandType Application -ErrorAction SilentlyContinue) {
    Info 'arduino-cli already on PATH'; return
  }
  $zip = Join-Path $env:TEMP 'arduino-cli_latest_Windows_64bit.zip'
  $unpack = Join-Path $env:TEMP 'arduino-cli-unpack'
  try {
    Invoke-WebRequest -Uri $ArduinoCliUrl -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $unpack -Force
    Copy-Item -LiteralPath (Join-Path $unpack 'arduino-cli.exe') -Destination $exe
  } catch {
    Die "could not install arduino-cli: $($_.Exception.Message). Re-run, or install it from https://arduino.github.io/arduino-cli/latest/installation/"
  } finally {
    Remove-Item -LiteralPath $zip, $unpack -Recurse -Force -ErrorAction SilentlyContinue
  }
  Info "arduino-cli installed at $exe"
}

function Test-Install([string]$Prefix) {
  $spad = Join-Path $Prefix 'Scripts\spad.exe'
  if ((Invoke-Native $spad @('--help') -Quiet) -ne 0) { Die "'spad' was installed but does not run." }
  Info 'spad CLI ok'
  if (-not $NoCamera) {
    $code = Invoke-Native (Join-Path $Prefix 'python.exe') @('-c', 'import pyrealsense2') -Quiet
    if ($code -ne 0) { Die 'pyrealsense2 does not import. Re-run, or use -NoCamera to skip it.' }
    Info 'pyrealsense2 ok'
  }
}

function Main {
  Assert-Windows

  Step 'Locating conda'
  $conda = Find-Conda
  if (-not $conda) { $conda = Install-Miniforge }
  Info "using $conda ($(Get-NativeOutput $conda @('--version')))"

  Step "Creating the '$Name' environment"
  $prefix = Initialize-Env $conda

  Step 'Installing spad_capture'
  Install-Package $prefix

  Step 'Installing arduino-cli (for flashing)'
  Install-ArduinoCli $prefix

  Step 'Checking the install'
  Test-Install $prefix

  if ($Flash) {
    Step "Flashing the $Flash board"
    $env:PATH = "$(Join-Path $prefix 'Scripts');$env:PATH"   # so spad finds arduino-cli
    $code = Invoke-Native (Join-Path $prefix 'Scripts\spad.exe') @($Flash, 'flash')
    if ($code -ne 0) { Die "flashing failed; the env is installed, so fix the board and run: spad $Flash flash" }
  }

  Step 'Done'
  Info 'Activate the env, then capture:'
  Info ''
  Info "    conda activate $Name"
  Info '    spad tmf capture        # or: spad st capture'
  if (-not $Flash) { Info '    spad tmf flash          # first time with a board: flash it' }
  if (-not $NoCamera) { Info '    spad camera check       # Realsense status' }
}

Main
