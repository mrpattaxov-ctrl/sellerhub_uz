[CmdletBinding(DefaultParameterSetName = "Direct")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Direct")]
    [string]$Prompt,

    [Parameter(Mandatory = $true, ParameterSetName = "Config")]
    [string]$ConfigPath,

    [string]$WorkingDirectory = "",
    [string]$ClaudeCommand = "claude",
    [string]$ResumeSessionId,
    [switch]$ContinueLatest,
    [switch]$SkipPermissions,
    [switch]$OpenInVSCode,
    [string]$VSCodeCommand = "code",
    [string]$LogDirectory = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $WorkingDirectory) {
    $WorkingDirectory = Split-Path -Parent $scriptDirectory
}
if (-not $LogDirectory) {
    $LogDirectory = Join-Path $scriptDirectory "logs"
}

function Resolve-ClaudeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommandName
    )

    if (Test-Path -LiteralPath $CommandName) {
        return (Resolve-Path -LiteralPath $CommandName).Path
    }

    try {
        return (Get-Command $CommandName -ErrorAction Stop).Source
    }
    catch {
        throw "Claude CLI was not found. Install it or pass -ClaudeCommand with the full executable path."
    }
}

function Resolve-CommandPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommandName,
        [Parameter(Mandatory = $true)]
        [string]$NotFoundMessage
    )

    if (Test-Path -LiteralPath $CommandName) {
        return (Resolve-Path -LiteralPath $CommandName).Path
    }

    try {
        return (Get-Command $CommandName -ErrorAction Stop).Source
    }
    catch {
        throw $NotFoundMessage
    }
}

if ($PSCmdlet.ParameterSetName -eq "Config") {
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        throw "Config file not found: $ConfigPath"
    }

    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    $Prompt = [string]$config.Prompt
    if (-not $Prompt) {
        throw "Config file is missing Prompt."
    }

    if ($config.WorkingDirectory) {
        $WorkingDirectory = [string]$config.WorkingDirectory
    }

    if ($config.ClaudeCommand) {
        $ClaudeCommand = [string]$config.ClaudeCommand
    }

    if ($config.ResumeSessionId) {
        $ResumeSessionId = [string]$config.ResumeSessionId
    }

    if ($null -ne $config.ContinueLatest) {
        $ContinueLatest = [bool]$config.ContinueLatest
    }

    if ($null -ne $config.SkipPermissions) {
        $SkipPermissions = [bool]$config.SkipPermissions
    }

    if ($null -ne $config.OpenInVSCode) {
        $OpenInVSCode = [bool]$config.OpenInVSCode
    }

    if ($config.VSCodeCommand) {
        $VSCodeCommand = [string]$config.VSCodeCommand
    }

    if ($config.LogDirectory) {
        $LogDirectory = [string]$config.LogDirectory
    }
}

if ($ContinueLatest -and $ResumeSessionId) {
    throw "Use either -ContinueLatest or -ResumeSessionId, not both."
}

if (-not (Test-Path -LiteralPath $WorkingDirectory)) {
    throw "Working directory not found: $WorkingDirectory"
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $LogDirectory "claude-scheduled-$timestamp.log"

if ($OpenInVSCode) {
    if (-not $ResumeSessionId) {
        throw "-OpenInVSCode requires -ResumeSessionId so VS Code can target the exact chat."
    }

    $resolvedVSCode = Resolve-CommandPath `
        -CommandName $VSCodeCommand `
        -NotFoundMessage "VS Code executable was not found. Install VS Code or pass -VSCodeCommand with the full executable path."

    Add-Type -AssemblyName System.Web
    $encodedSession = [System.Web.HttpUtility]::UrlEncode($ResumeSessionId)
    $encodedPrompt = [System.Web.HttpUtility]::UrlEncode($Prompt)
    $openUri = "vscode://anthropic.claude-code/open?session=$encodedSession&prompt=$encodedPrompt"

    "[$(Get-Date -Format s)] $resolvedVSCode --open-url $openUri" | Tee-Object -FilePath $logPath -Append
    Start-Process -FilePath $resolvedVSCode -ArgumentList @("--open-url", $openUri) | Out-Null
}
else {
    $resolvedClaude = Resolve-ClaudeCommand -CommandName $ClaudeCommand

    $arguments = @()
    if ($SkipPermissions) {
        $arguments += "--dangerously-skip-permissions"
    }
    if ($ResumeSessionId) {
        $arguments += @("-r", $ResumeSessionId)
    }
    elseif ($ContinueLatest) {
        $arguments += "-c"
    }
    $arguments += @("-p", $Prompt)

    Push-Location $WorkingDirectory
    try {
        "[$(Get-Date -Format s)] $resolvedClaude $($arguments -join ' ')" | Tee-Object -FilePath $logPath -Append
        & $resolvedClaude @arguments 2>&1 | Tee-Object -FilePath $logPath -Append
    }
    finally {
        Pop-Location
    }
}
