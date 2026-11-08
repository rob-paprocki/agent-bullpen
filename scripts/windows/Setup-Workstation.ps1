<#
  Setup-Workstation.ps1 -- make a Windows 10 or 11 PC a machine the Agent Bullpen hub
  can send jobs to.

  The easy way is the one-line command from "Add a machine" on the hub's
  dashboard: it downloads this script from your hub and runs it with the hub's
  address and a one-time code filled in.

  To run it by hand, open a normal PowerShell window (NOT "Run as
  administrator"; Windows asks for admin approval itself when an installer
  needs it):

      Set-ExecutionPolicy -Scope Process Bypass -Force
      .\scripts\windows\Setup-Workstation.ps1

  Options (all optional; whatever isn't given is asked):
      -Hub URL          the hub's address, e.g. http://192.168.100.20:8080
      -Code CODE        a one-time code from "Add a machine" (or set BULLPEN_ENROLL_CODE)
      -Token TOKEN      a runner token instead of a code (or set BULLPEN_TOKEN)
      -Name NAME        this PC's name on the dashboard (or set BULLPEN_NAME)
      -RunnerFrom DIR   where runner.py is (default: the runner folder of this repository)

  It walks you through these steps, asking before each one:
    1. Git, Python and (for Gemini CLI) Node.js, using winget
    2. the runner files, and connecting this PC to the hub
    3. Claude Code (Anthropic's official installer)
    4. Gemini CLI (optional; needs a Gemini API key)
    5. the agents this PC offers: any number of Claude accounts and Gemini
       agents, each with its own name
    6. signing in to each Claude account
    7. the runner: starts now and whenever you sign in to Windows
    8. keeping the PC awake, and shared notes, /remember and MCP servers
  Safe to re-run: finished steps are detected and skipped. It sends nothing
  anywhere except to your hub and the download sites of what it installs.
#>
[CmdletBinding()]
param(
    [string]$Hub = $env:BULLPEN_HUB,
    [string]$Code = $env:BULLPEN_ENROLL_CODE,
    [string]$Token = $env:BULLPEN_TOKEN,
    [string]$Name = $env:BULLPEN_NAME,
    [string]$RunnerFrom = ''
)
$ErrorActionPreference = 'Stop'
# Keep the code and token out of the programs this script starts.
Remove-Item Env:\BULLPEN_ENROLL_CODE, Env:\BULLPEN_TOKEN -ErrorAction SilentlyContinue
# Windows PowerShell 5.1 on older builds may not offer TLS 1.2 by default; the installers need it.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$BullpenHome = Join-Path $env:USERPROFILE '.bullpen'
$BinDir = Join-Path $env:USERPROFILE '.local\bin'
$Config = Join-Path $BullpenHome 'runner.json'
$RunnerDir = Join-Path $BullpenHome 'runner'
$RunnerPy = Join-Path $RunnerDir 'runner.py'

function Write-Step([string]$Text) { Write-Host ''; Write-Host $Text -ForegroundColor White }
function Write-Ok([string]$Text)   { Write-Host "  [ok] $Text" -ForegroundColor Green }
function Write-Info([string]$Text) { Write-Host "  -  $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "  [!] $Text" -ForegroundColor Yellow }
function Test-Command([string]$CommandName) { [bool](Get-Command $CommandName -ErrorAction SilentlyContinue) }

function Read-YesNo([string]$Question, [bool]$Default = $true) {
    $hint = if ($Default) { 'Y/n' } else { 'y/N' }
    $answer = Read-Host "  $Question ($hint)"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim().ToLower().StartsWith('y')
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user;$BinDir"
}

function Add-UserPath([string]$Folder) {
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $user) { $user = '' }
    if (($user -split ';') -notcontains $Folder) {
        [Environment]::SetEnvironmentVariable('Path', ($user.TrimEnd(';') + ';' + $Folder).TrimStart(';'), 'User')
        Write-Info "added $Folder to your PATH"
    }
}

function Install-WithWinget([string]$Id, [string]$Label, [string[]]$Extra = @()) {
    Write-Info "installing $Label with winget (approve the admin prompt if Windows asks)"
    $wingetArgs = @('install', '--exact', '--id', $Id, '--silent', '--accept-package-agreements', '--accept-source-agreements') + $Extra
    & winget @wingetArgs
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {  # -1978335189 = already installed
        Write-Note "winget returned $LASTEXITCODE for $Label"
    }
    Update-SessionPath
}

function Get-PythonExe {
    if (Test-Command 'py') {
        try { return (& py -3 -c 'import sys; print(sys.executable)').Trim() } catch { }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike '*WindowsApps*') { return $cmd.Source }
    return $null
}

function Test-PythonVersion([string]$Exe) {
    & $Exe -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
    return ($LASTEXITCODE -eq 0)
}

function Get-NodeMajor {
    if (-not (Test-Command 'node')) { return 0 }
    $v = (& node -v) -join ''
    if ($v -match '^v(\d+)') { return [int]$Matches[1] }
    return 0
}

function Invoke-Runner {
    # Runs runner.py with this PC's config; the arguments are passed on as they are. Callers quote
    # arguments that start with '-', so PowerShell doesn't take them as its own parameters.
    & $Python $RunnerPy '--config' $Config @args
}

Write-Host ''
Write-Host 'Agent Bullpen -- Windows machine setup' -ForegroundColor White
Write-Host "  Computer: $env:COMPUTERNAME   User: $env:USERNAME"
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Note 'This window runs as administrator. The runner, the Claude logins and the scheduled task'
    Write-Note 'belong to the account running this script, so run it as the user who will use this PC.'
}
if ($env:ANTHROPIC_API_KEY) {
    Write-Note 'ANTHROPIC_API_KEY is set. Claude Code would bill that API key instead of your subscription.'
    Write-Note 'Remove it (System Properties > Environment Variables) unless that is what you want.'
}
New-Item -ItemType Directory -Force -Path $BullpenHome, $BinDir, $RunnerDir | Out-Null
Add-UserPath $BinDir
Update-SessionPath

# --------------------------------------------------------------- 1. basics
Write-Step '1/8  Git, Python and Node.js'
if (-not (Test-Command 'winget')) {
    throw 'winget was not found. Install "App Installer" from the Microsoft Store, then run this again.'
}
if (Test-Command 'git') { Write-Ok "Git: $((git --version) -join '')" }
else { Install-WithWinget 'Git.Git' 'Git for Windows (Claude Code uses its Bash; the runner uses git for diffs)' }

$Python = Get-PythonExe
if (-not $Python -or -not (Test-PythonVersion $Python)) {
    Install-WithWinget 'Python.Python.3.13' 'Python 3.13' @('--scope', 'user')
    $Python = Get-PythonExe
    if (-not $Python) { throw 'Python was installed but not found. Close this window, open a new PowerShell and run the script again.' }
    if (-not (Test-PythonVersion $Python)) { throw "The runner needs Python 3.9 or newer; $Python is older. Install a newer one and run this again." }
}
Write-Ok "Python: $Python"

$WantGemini = $false
Write-Host '  Gemini CLI needs a Gemini API key from https://aistudio.google.com/apikey, billed per use;'
Write-Host '  Google AI Pro and other consumer sign-ins no longer work in it. See docs\01.'
if (Test-Command 'gemini') { $WantGemini = $true }
elseif (Read-YesNo 'Install Gemini CLI (needs Node.js 20 or newer)?' $false) {
    $WantGemini = $true
    if ((Get-NodeMajor) -lt 20) { Install-WithWinget 'OpenJS.NodeJS.LTS' 'Node.js LTS' }
    if ((Get-NodeMajor) -lt 20) {
        $nodeVersion = if (Test-Command 'node') { (& node -v) -join '' } else { 'not found yet' }
        Write-Note "Node.js 20 or newer is needed ($nodeVersion). If it was just installed, open a new PowerShell"
        Write-Note 'window and run this again. If an old version is installed, remove it in Settings > Apps and'
        Write-Note 'install the LTS from https://nodejs.org first. Skipping Gemini CLI for now.'
        $WantGemini = $false
    }
}

# --------------------------------------------------- 2. runner + the hub
Write-Step '2/8  The runner, and connecting to the hub'
$Source = $null
$candidates = @($RunnerFrom, (Join-Path $PSScriptRoot '..\..\runner'), (Join-Path $PSScriptRoot 'runner'), $RunnerDir)
foreach ($dir in $candidates) {
    if ($dir -and (Test-Path (Join-Path $dir 'runner.py'))) { $Source = (Resolve-Path $dir).Path; break }
}
if (-not $Source) { throw "Can't find runner.py. Run this from the agent-bullpen repository, or use the command from Add a machine." }
function Get-RunnerVersion([string]$Dir) {
    $line = Select-String -Path (Join-Path $Dir 'runner.py') -Pattern '^VERSION = "([0-9.]+)"' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($line) { try { return [version]$line.Matches[0].Groups[1].Value } catch { } }
    return [version]'0.0'
}
$installed = Join-Path $RunnerDir 'runner.py'
if ($Source -eq (Resolve-Path $RunnerDir).Path) {
    Write-Ok "runner is in $RunnerDir"
} elseif (-not $RunnerFrom -and (Test-Path $installed) -and ((Get-RunnerVersion $Source) -le (Get-RunnerVersion $RunnerDir))) {
    # Re-running the saved setup (to add agents) must not put back the older runner it was saved
    # with: "Update now" may have installed a newer one since.
    Write-Ok "keeping the installed runner $(Get-RunnerVersion $RunnerDir) in $RunnerDir"
} else {
    Copy-Item -Force (Join-Path $Source '*.py') $RunnerDir
    Write-Ok "runner $(Get-RunnerVersion $Source) copied to $RunnerDir"
}

$HubDone = $false
if ($Code -or $Token) {
    if (-not $Hub) { $Hub = Read-Host '  Hub address (e.g. http://192.168.100.20:8080)' }
    if (-not $Hub) { throw "The hub's address is needed (-Hub)." }
    $nameArgs = @()
    if ($Name) { $nameArgs = @('--name', $Name) }
    $enrollArgs = @('enroll', '--hub', $Hub) + $nameArgs
    # The code or token reaches the runner through its environment, not its command line.
    if ($Code) {
        Write-Info "trading the one-time code for this PC's own runner token"
        $env:BULLPEN_ENROLL_CODE = $Code
    } else { $env:BULLPEN_TOKEN = $Token }
    try { Invoke-Runner @enrollArgs }
    finally { Remove-Item Env:\BULLPEN_ENROLL_CODE, Env:\BULLPEN_TOKEN -ErrorAction SilentlyContinue }
    if ($LASTEXITCODE -ne 0) { throw "The hub didn't connect this PC (see the message above). Nothing else was changed." }
    $HubDone = $true
} elseif ((Test-Path $Config) -and ((Get-Content $Config -Raw) -match '"token":\s*"[^"]')) {
    $currentName = (Get-Content $Config -Raw | ConvertFrom-Json).name
    if ($Name -and $Name -ne $currentName) {
        # The hub knows this PC by its name (its own token is tied to it), so renaming is done with
        # a new code made for the new name.
        throw "This PC is on the hub as '$currentName'. To rename it, click Add a machine on the dashboard, type '$Name' as the name, and run the command it shows here. Then Forget '$currentName' on the dashboard."
    }
    Write-Ok 'already connected to a hub (step 5 lets you keep or change that)'
} else {
    Write-Info "you'll be asked for the hub's address and a one-time code in step 5"
    Write-Info '(on the dashboard: Add a machine; or use the hub''s shared runner token)'
}

# --------------------------------------------------------------- 3. Claude
Write-Step '3/8  Claude Code'
if (Test-Command 'claude') {
    Write-Ok "Claude Code: $((claude --version) -join '')"
} else {
    Write-Info "installing with Anthropic's official installer (https://claude.ai/install.ps1)"
    Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
    Update-SessionPath
    if (-not (Test-Command 'claude')) { throw 'Claude Code did not end up on PATH. Open a new PowerShell window and run this again.' }
    Write-Ok "installed $((claude --version) -join '')"
}

# --------------------------------------------------------------- 4. Gemini
Write-Step '4/8  Gemini CLI'
if ($WantGemini) {
    if (Test-Command 'gemini') { Write-Ok "Gemini CLI: $((gemini --version) -join '')" }
    else {
        if (-not (Test-Command 'npm')) { throw 'npm was not found. Open a new PowerShell window (so PATH refreshes) and run this again.' }
        Write-Info 'installing @google/gemini-cli with npm'
        & npm install -g @google/gemini-cli | Out-Null
        Update-SessionPath
        if (Test-Command 'gemini') { Write-Ok 'Gemini CLI installed' } else { Write-Note 'Gemini CLI installed but not on PATH yet; open a new window later.' }
    }
} else { Write-Info 'skipped' }

# --------------------------------------------------------------- 5. agents
Write-Step '5/8  The agents this PC offers'
Write-Host '  The runner asks which agents to offer. Add one Claude agent for each Claude account you want'
Write-Host "  to use here: the first is called 'claude' (its login lives in .claude); give the others a"
Write-Host "  name such as 'work', and each gets its own login folder (.claude-work). Gemini works the same."
Write-Host '  Add the folders where your work lives as projects (now, or later from the dashboard).'
$env:BULLPEN_HUB = $Hub
$env:BULLPEN_NAME = $Name
try {
    if ($HubDone) { Invoke-Runner 'init' '--skip-hub' } else { Invoke-Runner 'init' }
} finally { Remove-Item Env:\BULLPEN_HUB, Env:\BULLPEN_NAME -ErrorAction SilentlyContinue }
if ($LASTEXITCODE -ne 0) { throw 'The runner setup did not finish (see the message above).' }

# --------------------------------------------------------------- 6. logins
Write-Step '6/8  Sign in to each Claude account'
Write-Host '  Each login opens your browser. Make sure the browser is signed in to the RIGHT Claude'
Write-Host '  account first (tip: one browser profile per account; copy the login link into it).'
Write-Host '  Choose the option to sign in with your Claude subscription.'

function Invoke-ClaudeAs([string]$Dir, [string]$Kind, [string[]]$ClaudeArgs) {
    # Runs claude with one account's settings folder, without keys from this window that would
    # make it skip the subscription login.
    $ErrorActionPreference = 'Continue'
    $names = 'CLAUDE_CONFIG_DIR', 'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDECODE'
    $saved = @{}
    foreach ($n in $names) { $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }
    try {
        foreach ($n in $names) { [Environment]::SetEnvironmentVariable($n, $null, 'Process') }
        if ($Kind -eq 'own') { $env:CLAUDE_CONFIG_DIR = $Dir }
        & claude @ClaudeArgs
    } finally {
        foreach ($n in $names) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
    }
}

function Test-SignedIn([string]$Dir, [string]$Kind) {
    # 'claude auth status' sees every kind of login; the file check covers older Claude Code versions.
    $status = (Invoke-ClaudeAs $Dir $Kind @('auth', 'status', '--json') 2>$null) -join ''
    if ($status -match '"loggedIn":\s*true') { return $true }
    return (Test-Path (Join-Path $Dir '.credentials.json'))
}

function Set-Wrapper([string]$AgentName, [string]$Dir) {
    # A command per extra account, so 'claude-work' in a terminal uses that account's login.
    $file = Join-Path $BinDir "$AgentName.cmd"
    if ((Test-Path $file) -and -not ((Get-Content $file -Raw) -match 'bullpen setup|Claude Code signed in to account')) {
        Write-Note "$file exists and isn't ours, so it was left alone"
        return
    }
    # Write the folder relative to %USERPROFILE%: a .cmd file can't hold accented letters reliably.
    $cmdDir = $Dir
    if ($Dir.StartsWith($env:USERPROFILE, [StringComparison]::OrdinalIgnoreCase)) {
        $cmdDir = '%USERPROFILE%' + $Dir.Substring($env:USERPROFILE.Length)
    }
    Set-Content -Encoding ASCII -Path $file -Value @(
        '@echo off',
        "rem Claude Code with the '$AgentName' login (made by the bullpen setup). Its login and settings live in $cmdDir",
        'setlocal',
        "set `"CLAUDE_CONFIG_DIR=$cmdDir`"",
        'set "CLAUDECODE="',
        'claude %*'
    )
}

$authHelp = $true
try { & claude auth --help *> $null; $authHelp = ($LASTEXITCODE -eq 0) } catch { $authHelp = $false }
$wrappers = @()
# Account folders can contain any letter (a profile folder with an accent, say): read them as UTF-8,
# not the console's code page.
$prevEncoding = [Console]::OutputEncoding
$env:PYTHONIOENCODING = 'utf-8'
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $accountLines = @(Invoke-Runner claude-accounts)
} finally {
    [Console]::OutputEncoding = $prevEncoding
    Remove-Item Env:\PYTHONIOENCODING -ErrorAction SilentlyContinue
}
foreach ($line in $accountLines) {
    $parts = "$line" -split "`t"
    if ($parts.Count -lt 4) { continue }
    $agent, $dir, $kind, $how = $parts[0], $parts[1], $parts[2], $parts[3]
    if ($how -ne 'login') { Write-Ok "$agent signs in with an API key or token; nothing to do"; continue }
    $cmd = 'claude'
    if ($kind -eq 'own') { Set-Wrapper $agent $dir; $cmd = $agent; $wrappers += $agent }
    if (Test-SignedIn $dir $kind) { Write-Ok "$agent is signed in ($dir)"; continue }
    if (Read-YesNo "Sign in '$agent' now?" $true) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        if ($authHelp) { Invoke-ClaudeAs $dir $kind @('auth', 'login') }
        else {
            Write-Host '  Starting Claude Code. Choose the option to log in with your Claude subscription, then type /exit.'
            Invoke-ClaudeAs $dir $kind @()
        }
        if (Test-SignedIn $dir $kind) { Write-Ok "$agent signed in" }
        else { Write-Note "no login found for $agent; later run: $cmd auth login" }
    }
}
if ($wrappers.Count) { Write-Ok "a command for each extra account is in ${BinDir}: $($wrappers -join ', ')" }

# --------------------------------------------------------------- 7. runner
Write-Step '7/8  Start the runner'
Invoke-Runner list
$cfg = Get-Content $Config -Raw | ConvertFrom-Json
foreach ($agent in $cfg.agents.PSObject.Properties.Name) {
    if (Read-YesNo "Test '$agent' now (uses a tiny bit of its quota)?" $true) { Invoke-Runner test $agent }
}

if (Read-YesNo 'Start the runner now and whenever you sign in to Windows?' $true) {
    $Pythonw = Join-Path (Split-Path $Python) 'pythonw.exe'
    if (-not (Test-Path $Pythonw)) { $Pythonw = $Python }
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$RunnerPy`" --config `"$Config`" run" -WorkingDirectory $BullpenHome
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'Agent Bullpen Runner' -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    # Restart it, so a re-run picks up new runner files.
    Stop-ScheduledTask -TaskName 'Agent Bullpen Runner' -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName 'Agent Bullpen Runner'
    Start-Sleep -Seconds 3
    $state = (Get-ScheduledTask -TaskName 'Agent Bullpen Runner').State
    Write-Ok "scheduled task 'Agent Bullpen Runner' registered (state: $state)"
    Write-Host "  Logs: $BullpenHome\runner.log"
}

# --------------------------------------------------------------- 8. extras
Write-Step '8/8  Extras'
if (Read-YesNo 'Stop this PC from sleeping while plugged in (so it stays reachable)?' $true) {
    try {
        powercfg /change standby-timeout-ac 0
        powercfg /change hibernate-timeout-ac 0
        Write-Ok 'sleep and hibernate disabled on AC power'
    } catch { Write-Note "powercfg failed: $($_.Exception.Message)" }
}
Write-Host '  Syncing shared notes, the /remember skill, the second-opinion add-on and the MCP servers'
Write-Host '  from the hub into every Claude account, Gemini CLI and Antigravity on this PC...'
Invoke-Runner sync
if ($LASTEXITCODE -eq 0) { Write-Ok 'shared context synced' }
else { Write-Note 'sync did not finish; it retries automatically once the runner can reach the hub' }

Write-Host ''
Write-Host 'All done.' -ForegroundColor White
Write-Host '  - This PC should now appear as online on the hub Overview page.'
Write-Host "  - Add a project later:  py -3 `"$RunnerPy`" add-project NAME C:\path\to\folder"
Write-Host "  - Add or remove agents: py -3 `"$RunnerPy`" init, then run this script again to sign in."
Write-Host '  - The runner runs while you are signed in to Windows (see docs\06 for automatic sign-in).'
Write-Host '  - Open a new PowerShell window so PATH changes take effect.'
