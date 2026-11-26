#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$EnvFile = 'deploy\.env',
    [string]$StateRoot = $null,
    [string]$BackupRoot = $null,
    [ValidateRange(1, 600)][int]$HealthTimeoutSeconds = 180,
    [scriptblock]$ExternalInvoker,
    [scriptblock]$HealthProbe
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Operations.Common.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'Backup.Common.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'QdrantVolume.Common.psm1') -Force

function Invoke-DrillExternal {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    if ($null -ne $ExternalInvoker) {
        return @(& $ExternalInvoker $FilePath $ArgumentList)
    }
    return @(Invoke-External -FilePath $FilePath -ArgumentList $ArgumentList)
}

function Get-DrillComposeArguments {
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)][string]$ProjectName,
        [Parameter(Mandatory)][string]$DrillEnvFile,
        [Parameter(Mandatory)][string[]]$Command
    )
    return @(
        'compose',
        '--project-name', $ProjectName,
        '--project-directory', $Config.RepositoryRoot,
        '--file', $Config.ComposeFile,
        '--env-file', $DrillEnvFile
    ) + $Command
}

function Assert-RegularNonReparseFile {
    param([Parameter(Mandatory)][string]$LiteralPath)
    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) {
        throw "Required file was not found: $LiteralPath"
    }
    $item = Get-Item -LiteralPath $LiteralPath -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Reparse-point files are not allowed: $LiteralPath"
    }
}

function Assert-SafeArchiveMembers {
    param([Parameter(Mandatory)][string[]]$MemberNames)
    foreach ($rawName in $MemberNames) {
        $name = ([string]$rawName).Trim()
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        if ($name.StartsWith('/') -or $name.StartsWith('\') -or $name -match '^[a-zA-Z]:') {
            throw 'Archive contains an absolute or drive-qualified member'
        }
        if (@($name -split '[\\/]') -contains '..') {
            throw 'Archive contains a parent traversal member'
        }
    }
}

function Assert-NoArchiveLinks {
    param([Parameter(Mandatory)][string[]]$VerboseEntries)
    foreach ($rawEntry in $VerboseEntries) {
        $entry = ([string]$rawEntry).TrimStart()
        if ([string]::IsNullOrWhiteSpace($entry)) { continue }
        if ($entry -match '^[lh]' -or $entry -match ' -> ' -or $entry -match ' link to ') {
            throw 'Archive contains a symbolic or hard link entry'
        }
    }
}

function ConvertTo-ComposePath {
    param([Parameter(Mandatory)][string]$Path)
    return [IO.Path]::GetFullPath($Path).Replace('\', '/')
}

function Write-PrivateDrillEnv {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination,
        [Parameter(Mandatory)][string]$DataRoot,
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$QdrantVolumeName
    )
    Assert-SafeDockerResourceName -Name $QdrantVolumeName | Out-Null
    $overrideNames = @('APP_BIND_ADDRESS', 'APP_PORT', 'DEPLOY_DATA_ROOT', 'DEPLOY_ENV_FILE', 'QDRANT_VOLUME_NAME')
    $preserved = @()
    foreach ($line in [IO.File]::ReadAllLines($Source)) {
        $match = [regex]::Match($line, '^\s*([^#=\s]+)\s*=')
        if ($match.Success -and $overrideNames -contains $match.Groups[1].Value) {
            continue
        }
        $preserved += $line
    }
    $content = @($preserved) + @(
        'APP_BIND_ADDRESS=127.0.0.1',
        "APP_PORT=$Port",
        "DEPLOY_DATA_ROOT=$(ConvertTo-ComposePath $DataRoot)",
        "DEPLOY_ENV_FILE=$(ConvertTo-ComposePath $Destination)",
        "QDRANT_VOLUME_NAME=$QdrantVolumeName"
    )
    [IO.File]::WriteAllLines(
        $Destination,
        $content,
        (New-Object System.Text.UTF8Encoding($false))
    )

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $identity,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
    [IO.File]::SetAccessControl($Destination, $acl)
}

function Test-DrillHealth {
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)][string]$ProjectName,
        [Parameter(Mandatory)][string]$DrillEnvFile
    )
    if ($null -ne $HealthProbe) {
        $result = & $HealthProbe $Config $ProjectName
        if ($result -is [bool]) { return $result }
        return $null -ne $result -and [bool]$result.Healthy
    }

    $lines = Invoke-DrillExternal -FilePath 'docker' -ArgumentList (
        Get-DrillComposeArguments -Config $Config -ProjectName $ProjectName -DrillEnvFile $DrillEnvFile -Command @(
            'ps', '--format', '{{.Service}}|{{.State}}|{{.Health}}', 'app', 'qdrant'
        )
    )
    $healthy = @{}
    foreach ($line in $lines) {
        $parts = ([string]$line) -split '\|', 3
        if ($parts.Count -eq 3 -and $parts[1] -eq 'running' -and $parts[2] -eq 'healthy') {
            $healthy[$parts[0]] = $true
        }
    }
    return $healthy.ContainsKey('app') -and $healthy.ContainsKey('qdrant')
}

function Protect-DrillText {
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Text,
        [Parameter(Mandatory)][string]$DeploymentEnvFile
    )
    $safe = Protect-LogText $Text
    if (-not (Test-Path -LiteralPath $DeploymentEnvFile -PathType Leaf)) {
        return $safe
    }
    foreach ($line in [IO.File]::ReadAllLines($DeploymentEnvFile)) {
        $match = [regex]::Match($line, '^\s*(?<name>[^#=\s]+)\s*=\s*(?<value>.*)\s*$')
        if (-not $match.Success -or $match.Groups['name'].Value -notmatch '(?i)(KEY|TOKEN|PASSWORD|SECRET)') {
            continue
        }
        $value = $match.Groups['value'].Value.Trim().Trim('"').Trim("'")
        if (-not [string]::IsNullOrEmpty($value)) {
            $safe = $safe.Replace($value, '[REDACTED]')
        }
    }
    return Protect-LogText $safe
}

function Remove-ValidatedDrillDirectory {
    param(
        [Parameter(Mandatory)][string]$DrillDirectory,
        [Parameter(Mandatory)][string]$DrillsRoot,
        [Parameter(Mandatory)][string]$ExpectedName
    )
    $safePath = Assert-SafePath -Path $DrillDirectory -AllowedRoot $DrillsRoot
    $parent = [IO.Path]::GetDirectoryName($safePath).TrimEnd('\', '/')
    $root = [IO.Path]::GetFullPath($DrillsRoot).TrimEnd('\', '/')
    if (-not $parent.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        -not [IO.Path]::GetFileName($safePath).Equals($ExpectedName, [StringComparison]::Ordinal)) {
        throw 'Drill cleanup target does not match the validated drill directory'
    }
    Assert-BackupTreeSafe -Path $safePath -AllowedRoot $root | Out-Null
    Remove-Item -LiteralPath $safePath -Recurse -Force
}

$reportRepositoryRoot = [IO.Path]::GetFullPath($RepositoryRoot)
function New-RestoreDrillReportPath {
    param([Parameter(Mandatory)][string]$StateRoot)

    $reportStateRoot = [IO.Path]::GetFullPath($StateRoot)
    $reportRoot = Join-Path $reportStateRoot 'reports'
    Assert-SafePath -Path $reportRoot -AllowedRoot $reportStateRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $reportRoot | Out-Null
    $reportStamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffffffZ')
    $reportPath = Join-Path $reportRoot "restore-drill-$reportStamp.json"
    Assert-SafePath -Path $reportPath -AllowedRoot $reportRoot | Out-Null
    return $reportPath
}

$fallbackStateSetting = if ([string]::IsNullOrWhiteSpace($StateRoot)) {
    'deploy-state'
} else {
    $StateRoot
}
$fallbackReportStateRoot = if ([IO.Path]::IsPathRooted($fallbackStateSetting)) {
    [IO.Path]::GetFullPath($fallbackStateSetting)
} else {
    [IO.Path]::GetFullPath((Join-Path $reportRepositoryRoot $fallbackStateSetting))
}
$reportPath = $null
$redactionEnvFile = if ([IO.Path]::IsPathRooted($EnvFile)) {
    [IO.Path]::GetFullPath($EnvFile)
} else {
    [IO.Path]::GetFullPath((Join-Path $reportRepositoryRoot $EnvFile))
}

$config = $null
$archiveName = $null
$archivePath = $null
$drillsRoot = $null
$drillId = $null
$drillDirectory = $null
$drillDataRoot = $null
$temporaryEnv = $null
$projectName = $null
$port = $null
$composeAttempted = $false
$drillVolumeCreated = $false
$drillVolumeName = $null
$failureMessage = $null
$failureStage = $null
$failureCategory = $null
$operationLock = $null
$stage = 'configuration'
$category = 'preflight'

try {
    $config = Get-OperationsConfig -RepositoryRoot $RepositoryRoot -EnvFile $EnvFile -StateRoot $StateRoot -BackupRoot $BackupRoot
    $reportPath = New-RestoreDrillReportPath -StateRoot $config.StateRoot
    $redactionEnvFile = $config.EnvFile
    $backupPath = [IO.Path]::GetFullPath($config.BackupRoot).TrimEnd('\', '/')
    $backupParent = [IO.Path]::GetDirectoryName($backupPath)
    Assert-BackupTreeSafe -Path $backupPath -AllowedRoot $backupParent | Out-Null

    $stage = 'backup-discovery'
    $dailyDirectory = Join-Path $backupPath 'daily'
    Assert-BackupTreeSafe -Path $dailyDirectory -AllowedRoot $backupPath | Out-Null
    $sets = @(Get-CompleteBackupSets -Directory $dailyDirectory -Prefix 'assistant-')
    if ($sets.Count -eq 0) {
        throw 'No complete daily backup is available for a restore drill'
    }
    $backupSet = $sets[0]
    $archivePath = Assert-SafePath -Path $backupSet.Archive -AllowedRoot $dailyDirectory
    $checksumPath = Assert-SafePath -Path $backupSet.Checksum -AllowedRoot $dailyDirectory
    Assert-RegularNonReparseFile -LiteralPath $archivePath
    Assert-RegularNonReparseFile -LiteralPath $checksumPath
    $archiveName = [IO.Path]::GetFileName($archivePath)
    $qdrantArchivePath = $backupSet.QdrantArchive
    $qdrantChecksumPath = $backupSet.QdrantChecksum

    $stage = 'checksum-validation'
    $checksumLine = (Get-Content -LiteralPath $checksumPath -Raw).Trim()
    $checksumMatch = [regex]::Match($checksumLine, '^(?<hash>[0-9a-fA-F]{64})  (?<name>[^\\/]+)$')
    if (-not $checksumMatch.Success -or
        -not $checksumMatch.Groups['name'].Value.Equals($archiveName, [StringComparison]::Ordinal)) {
        throw 'Checksum sidecar does not match the selected archive name'
    }
    $actualHash = Get-BackupSha256 -LiteralPath $archivePath
    if (-not $actualHash.Equals($checksumMatch.Groups['hash'].Value, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Backup checksum mismatch'
    }
    $qdrantActualHash = $null
    if (-not [string]::IsNullOrWhiteSpace($qdrantArchivePath)) {
        $qdrantArchivePath = Assert-SafePath -Path $qdrantArchivePath -AllowedRoot $dailyDirectory
        $qdrantChecksumPath = Assert-SafePath -Path $qdrantChecksumPath -AllowedRoot $dailyDirectory
        Assert-RegularNonReparseFile -LiteralPath $qdrantArchivePath
        Assert-RegularNonReparseFile -LiteralPath $qdrantChecksumPath
        $qdrantLine = (Get-Content -LiteralPath $qdrantChecksumPath -Raw).Trim()
        $qdrantMatch = [regex]::Match($qdrantLine, '^(?<hash>[0-9a-fA-F]{64})  (?<name>[^\\/]+)$')
        if (-not $qdrantMatch.Success -or
            -not $qdrantMatch.Groups['name'].Value.Equals([IO.Path]::GetFileName($qdrantArchivePath), [StringComparison]::Ordinal)) {
            throw 'Qdrant checksum sidecar does not match the selected archive name'
        }
        $qdrantActualHash = Get-BackupSha256 -LiteralPath $qdrantArchivePath
        if (-not $qdrantActualHash.Equals($qdrantMatch.Groups['hash'].Value, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Qdrant backup checksum mismatch'
        }
    }

    $stage = 'member-validation'
    $members = @(Invoke-DrillExternal -FilePath 'tar.exe' -ArgumentList @('-tzf', $archivePath))
    Assert-SafeArchiveMembers -MemberNames $members
    $archiveHasGraphData = @($members | Where-Object { ([string]$_).Trim() -match '^(?:\.[\\/])?neo4j[\\/]data(?:[\\/]|$)' }).Count -gt 0
    if ($archiveHasGraphData) {
        Assert-GraphBackupPair -Archive $archivePath -ArchiveHash $actualHash -QdrantArchive $qdrantArchivePath -QdrantHash $qdrantActualHash
    }
    $verboseEntries = @(Invoke-DrillExternal -FilePath 'tar.exe' -ArgumentList @('-tvzf', $archivePath))
    Assert-NoArchiveLinks -VerboseEntries $verboseEntries
    $preExtractionHash = Get-BackupSha256 -LiteralPath $archivePath
    if (-not $preExtractionHash.Equals($checksumMatch.Groups['hash'].Value, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Backup checksum changed during restore drill validation'
    }

    $operationLock = Enter-OperationsLock -StateRoot $config.StateRoot
    $stage = 'drill-setup'
    $drillsRoot = Join-Path $backupPath '.drills'
    Assert-BackupPathAncestorsSafe -Path $drillsRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $drillsRoot | Out-Null
    Assert-BackupTreeSafe -Path $drillsRoot -AllowedRoot $backupPath | Out-Null
    $drillId = [Guid]::NewGuid().ToString('N')
    $drillDirectory = Join-Path $drillsRoot $drillId
    $drillDataRoot = Join-Path $drillDirectory 'data'
    $temporaryEnv = Join-Path $drillDirectory 'restore-drill.env'
    Assert-SafePath -Path $drillDirectory -AllowedRoot $drillsRoot | Out-Null
    New-Item -ItemType Directory -Path $drillDataRoot | Out-Null
    $projectName = "assistant-drill-$($drillId.Substring(0, 12))"
    $drillVolumeName = "zhiyan-drill-$($drillId.Substring(0, 20))"
    $port = Get-FreeTcpPort
    while ($port -eq 7860) { $port = Get-FreeTcpPort }

    $category = 'execution'
    $stage = 'extraction'
    Invoke-DrillExternal -FilePath 'tar.exe' -ArgumentList @('-C', $drillDataRoot, '-xzf', $archivePath) | Out-Null
    Assert-BackupTreeSafe -Path $drillDataRoot -AllowedRoot $drillDirectory | Out-Null
    $requiredDirectories = @(if ([string]::IsNullOrWhiteSpace($qdrantArchivePath)) { 'app'; 'qdrant' } else { 'app' })
    if ($archiveHasGraphData) { $requiredDirectories += 'neo4j/data' }
    foreach ($requiredDirectory in $requiredDirectories) {
        if (-not (Test-Path -LiteralPath (Join-Path $drillDataRoot $requiredDirectory) -PathType Container)) {
            throw "Archive is missing required directory: $requiredDirectory"
        }
    }

    $stage = 'environment-setup'
    Write-PrivateDrillEnv -Source $config.EnvFile -Destination $temporaryEnv -DataRoot $drillDataRoot -Port $port -QdrantVolumeName $drillVolumeName
    if (-not [string]::IsNullOrWhiteSpace($qdrantArchivePath) -or $null -eq $ExternalInvoker) {
        $helperImage = Get-QdrantHelperImage -Config $config -ExternalInvoker $ExternalInvoker
        if (-not (New-QdrantVolume -Name $drillVolumeName -ExternalInvoker $ExternalInvoker)) {
            throw "Restore drill volume already exists: $drillVolumeName"
        }
        $drillVolumeCreated = $true
        if (-not [string]::IsNullOrWhiteSpace($qdrantArchivePath)) {
            Import-QdrantVolume -Name $drillVolumeName -Archive $qdrantArchivePath -HelperImage $helperImage -RequireEmpty -ExternalInvoker $ExternalInvoker
        } else {
            Import-QdrantDirectory -Name $drillVolumeName -SourceDirectory (Join-Path $drillDataRoot 'qdrant') -HelperImage $helperImage -RequireEmpty -ExternalInvoker $ExternalInvoker
        }
    }
    $stage = 'compose-start'
    $composeAttempted = $true
    $startCommand = @('up', '-d')
    if ([IO.Path]::GetFileName($config.ComposeFile) -eq 'compose.release.yaml') {
        $startCommand += @('--no-build', '--pull', 'never')
    }
    Invoke-DrillExternal -FilePath 'docker' -ArgumentList (
        Get-DrillComposeArguments -Config $config -ProjectName $projectName -DrillEnvFile $temporaryEnv -Command $startCommand
    ) | Out-Null
    $stage = 'health-check'
    $healthy = Wait-Until -TimeoutSeconds $HealthTimeoutSeconds -IntervalSeconds 2 -Condition {
        Test-DrillHealth -Config $config -ProjectName $projectName -DrillEnvFile $temporaryEnv
    }
    if (-not $healthy) {
        throw 'Restore drill services did not become healthy'
    }
    $stage = 'smoke-test'
    Invoke-DrillExternal -FilePath $config.Python -ArgumentList @(
        (Join-Path $config.RepositoryRoot 'deploy\smoke_test.py'),
        '--env-file', $temporaryEnv,
        '--project-name', $projectName
    ) | Out-Null
} catch {
    $failureMessage = $_.Exception.Message
    $failureStage = $stage
    $failureCategory = $category
} finally {
    if ($composeAttempted -and $null -ne $config -and $null -ne $projectName -and $null -ne $temporaryEnv) {
        try {
            Invoke-DrillExternal -FilePath 'docker' -ArgumentList (
                Get-DrillComposeArguments -Config $config -ProjectName $projectName -DrillEnvFile $temporaryEnv -Command @('down')
            ) | Out-Null
        } catch {
            $cleanupError = "compose down failed: $($_.Exception.Message)"
            if ($null -eq $failureMessage) {
                $failureMessage = $cleanupError
                $failureStage = 'compose-down'
                $failureCategory = 'cleanup'
            } else {
                $failureMessage = "$failureMessage; $cleanupError"
            }
        }
    }
    if ($null -ne $temporaryEnv -and (Test-Path -LiteralPath $temporaryEnv -PathType Leaf)) {
        try {
            Remove-Item -LiteralPath $temporaryEnv -Force
        } catch {
            $cleanupError = "temporary environment cleanup failed: $($_.Exception.Message)"
            if ($null -eq $failureMessage) {
                $failureMessage = $cleanupError
                $failureStage = 'environment-cleanup'
                $failureCategory = 'cleanup'
            } else {
                $failureMessage = "$failureMessage; $cleanupError"
            }
        }
    }
    if ($null -ne $operationLock) {
        Exit-OperationsLock -Lock $operationLock
    }
}

if ($null -eq $failureMessage -and $null -ne $drillDirectory) {
    try {
        Remove-ValidatedDrillDirectory -DrillDirectory $drillDirectory -DrillsRoot $drillsRoot -ExpectedName $drillId
    } catch {
        $failureMessage = "drill data cleanup failed: $($_.Exception.Message)"
        $failureStage = 'drill-cleanup'
        $failureCategory = 'cleanup'
    }
}

if ($null -eq $failureMessage -and $drillVolumeCreated) {
    try {
        Remove-QdrantVolume -Name $drillVolumeName -RequiredPrefix 'zhiyan-drill-' -ExternalInvoker $ExternalInvoker
        $drillVolumeCreated = $false
    } catch {
        $failureMessage = "drill volume cleanup failed: $($_.Exception.Message)"
        $failureStage = 'volume-cleanup'
        $failureCategory = 'cleanup'
    }
}

$safeFailure = if ($null -eq $failureMessage) { $null } else {
    Protect-DrillText -Text $failureMessage -DeploymentEnvFile $redactionEnvFile
}
$retainedData = if ($null -ne $failureMessage -and
    $null -ne $drillDataRoot -and
    (Test-Path -LiteralPath $drillDataRoot -PathType Container)) {
    $drillDataRoot
} else {
    $null
}
$report = [ordered]@{
    status = if ($null -eq $failureMessage) { 'succeeded' } else { 'failed' }
    completed_at = (Get-Date).ToUniversalTime().ToString('o')
    archive = $archiveName
    project_name = $projectName
    bind_address = '127.0.0.1'
    port = $port
    failure_stage = $failureStage
    failure_category = $failureCategory
    retained_data = $retainedData
    qdrant_volume = $drillVolumeName
    retained_qdrant_volume = if ($drillVolumeCreated) { $drillVolumeName } else { $null }
    error = $safeFailure
} | ConvertTo-Json
if ($null -eq $reportPath) {
    $reportPath = New-RestoreDrillReportPath -StateRoot $fallbackReportStateRoot
}
[IO.File]::WriteAllText($reportPath, $report, (New-Object System.Text.UTF8Encoding($false)))

if ($null -ne $failureMessage) {
    throw "Restore drill failed: $safeFailure"
}

[PSCustomObject]@{
    Report = $reportPath
    Archive = $archivePath
    ProjectName = $projectName
    Port = $port
}
