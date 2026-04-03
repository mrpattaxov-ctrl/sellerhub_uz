[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$TaskName,

    [Parameter(Mandatory = $true)]
    [datetime]$At,

    [Parameter(Mandatory = $true)]
    [string]$Prompt,

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

function Quote-TaskArgument {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    return '"' + $Value.Replace('"', '""') + '"'
}

if ($At -le (Get-Date)) {
    throw "The scheduled time must be in the future."
}

if ($ContinueLatest -and $ResumeSessionId) {
    throw "Use either -ContinueLatest or -ResumeSessionId, not both."
}

if ($OpenInVSCode -and -not $ResumeSessionId) {
    throw "-OpenInVSCode requires -ResumeSessionId."
}

if (-not (Test-Path -LiteralPath $WorkingDirectory)) {
    throw "Working directory not found: $WorkingDirectory"
}

$invokeScriptPath = Join-Path $scriptDirectory "invoke_claude_scheduled_message.ps1"
if (-not (Test-Path -LiteralPath $invokeScriptPath)) {
    throw "Invoke script not found: $invokeScriptPath"
}

$messageDirectory = Join-Path $scriptDirectory "scheduled_messages"
New-Item -ItemType Directory -Path $messageDirectory -Force | Out-Null

$safeTaskName = ($TaskName -replace '[^A-Za-z0-9._-]', '_')
$configPath = Join-Path $messageDirectory "$safeTaskName.json"

$config = [ordered]@{
    Prompt           = $Prompt
    WorkingDirectory = $WorkingDirectory
    ClaudeCommand    = $ClaudeCommand
    ResumeSessionId  = $ResumeSessionId
    ContinueLatest   = [bool]$ContinueLatest
    SkipPermissions  = [bool]$SkipPermissions
    OpenInVSCode     = [bool]$OpenInVSCode
    VSCodeCommand    = $VSCodeCommand
    LogDirectory     = $LogDirectory
}

$configJson = $config | ConvertTo-Json -Depth 4
$actionArgs = @(
    "-NoProfile"
    "-ExecutionPolicy", "Bypass"
    "-File", (Quote-TaskArgument -Value $invokeScriptPath)
    "-ConfigPath", (Quote-TaskArgument -Value $configPath)
)

$action = New-ScheduledTaskAction `
    -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument ($actionArgs -join " ")

$trigger = New-ScheduledTaskTrigger -Once -At $At
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled Claude Code message task")) {
    Set-Content -LiteralPath $configPath -Value $configJson -Encoding UTF8

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Scheduled Claude Code prompt" `
        -Force | Out-Null

    [pscustomobject]@{
        TaskName   = $TaskName
        Scheduled  = $At
        ConfigPath = $configPath
    }
}
