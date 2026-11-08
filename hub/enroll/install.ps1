# =============================================================================
#  Agent Bullpen: add this Windows PC to your hub
#
#  Your hub's "Add a machine" page made this script for one machine. You can
#  read it before running it; reading it doesn't use up the code:
#
#      irm '__HUB_URL__/enroll/__CODE__/install.ps1'
#
#  Run it in a normal PowerShell window (not "Run as administrator"). Windows
#  asks for admin approval itself when an installer needs it.
#
#  What it does:
#    1. Downloads the runner and the machine setup script (__SETUP__) from
#       your hub at __HUB_URL__ into %USERPROFILE%\.bullpen\setup\, and checks
#       each file against the sha256 sums listed below.
#    2. Runs that setup script. It asks before each step and:
#         - installs what is missing with winget: Git, Python and, if you want
#           Gemini CLI, Node.js; then Claude Code (Anthropic's installer);
#         - connects this PC to the hub with the one-time code below. The hub
#           gives the PC its own runner token, saved in .bullpen\runner.json;
#         - asks which agents to offer, and signs you in to each Claude account
#           (in your browser);
#         - starts the runner now and whenever you sign in to Windows (a
#           scheduled task called "Agent Bullpen Runner").
#
#  It talks only to your hub and to the official download sites of the tools
#  it installs. It sends no usage data anywhere.
#
#  The code works once, and only until it expires (__MINUTES__ minutes after
#  this script was made). Running the script again is safe: finished steps are
#  skipped. Once the code is used, run the saved setup script instead:
#
#      powershell -ExecutionPolicy Bypass -File $HOME\.bullpen\setup\__SETUP__
#
#  To choose the machine's name on the dashboard, run this first in the same
#  window:   $env:BULLPEN_NAME = 'build-box'
#
#  Made by Agent Bullpen __VERSION__.
# =============================================================================
# Everything runs inside this block, so nothing is left behind in your PowerShell session
# and an error stops the script without closing the window.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # downloads are much faster without the progress bar
    $Hub = '__HUB_URL__'
    $Code = '__CODE__'
    $Setup = '__SETUP__'
    # File name and sha256 of everything this script downloads from the hub.
    $Files = [ordered]@{
__FILES__
    }

    Write-Host ''
    Write-Host "Agent Bullpen: adding $env:COMPUTERNAME to the hub at $Hub" -ForegroundColor White
    if (-not $Files.Contains('runner.py')) { throw 'The hub has no runner files to hand out. Check its installation (docs/04).' }
    if (-not $Files.Contains($Setup)) {
        throw "The hub has no $Setup to hand out (its installation is older or incomplete). Update the hub, or set this PC up by hand: see docs/06-windows-machines.md."
    }

    $Dir = Join-Path $env:USERPROFILE '.bullpen\setup'
    $RunnerDir = Join-Path $Dir 'runner'
    New-Item -ItemType Directory -Force -Path $RunnerDir | Out-Null

    Write-Host "  -  downloading the runner and $Setup from the hub" -ForegroundColor Cyan
    foreach ($name in $Files.Keys) {
        if ($name.EndsWith('.py')) { $dest = Join-Path $RunnerDir $name } else { $dest = Join-Path $Dir $name }
        $tmp = "$dest.download"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "$Hub/enroll/$Code/files/$name" -OutFile $tmp
        } catch {
            throw "Couldn't download $name from the hub ($($_.Exception.Message)). Is the code still valid? It works for an hour, once."
        }
        $sum = (Get-FileHash -Algorithm SHA256 -Path $tmp).Hash.ToLower()
        if ($sum -ne $Files[$name]) {
            Remove-Item -Force $tmp
            throw "$name didn't match its checksum. Nothing was installed; try again."
        }
        Move-Item -Force $tmp $dest
    }
    Write-Host "  [ok] saved in $Dir" -ForegroundColor Green

    # Run the setup in its own PowerShell, with scripts allowed for that one process only.
    # The code goes in the environment, not the command line.
    $exe = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path $exe)) { $exe = Join-Path $PSHOME 'pwsh.exe' }
    $env:BULLPEN_ENROLL_CODE = $Code
    try {
        & $exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dir $Setup) -Hub $Hub -RunnerFrom $RunnerDir
    } finally {
        Remove-Item Env:\BULLPEN_ENROLL_CODE -ErrorAction SilentlyContinue
    }
}
