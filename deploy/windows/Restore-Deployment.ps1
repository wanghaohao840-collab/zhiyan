#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Archive,
    [string]$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$EnvFile = 'deploy\.env',
    [string]$StateRoot = $null,
    [string]$BackupRoot = $null,
    [ValidateRange(1, 600)][int]$HealthTimeoutSeconds = 180,
    [scriptblock]$ExternalInvoker,
    [scriptblock]$HealthProbe,
    [IO.FileStream]$InheritedOperationLock,
    [switch]$KeepStopped
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Operations.Common.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'Backup.Common.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'QdrantVolume.Common.psm1') -Force

function Assert-InheritedOperationsLock {
    param(
        [Parameter(Mandatory)][IO.FileStream]$Lock,
        [Parameter(Mandatory)][string]$StateRoot
    )

    $expectedPath = [IO.Path]::GetFullPath((Join-Path $StateRoot 'operations.lock'))
    try {
        $actualPath = [IO.Path]::GetFullPath($Lock.Name)
    } catch {
        throw 'The inherited lock must be a live exclusive operations lock'
    }
    if (-not $actualPath.Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase) -or
        -not $Lock.CanRead -or
        -not $Lock.CanWrite -or
        $Lock.SafeFileHandle.IsClosed -or
        $Lock.SafeFileHandle.IsInvalid) {
        throw 'The inherited lock must be a live exclusive operations lock'
    }

    foreach ($access in @([IO.FileAccess]::Read, [IO.FileAccess]::Write)) {
        $probe = $null
        $exclusive = $false
        try {
            # A None-sharing probe would itself conflict with even a shared
            # parent handle. Only parent-denied access proves exclusion.
            $probe = [IO.File]::Open(
                $expectedPath, [IO.FileMode]::Open, $access,
                ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
            )
        } catch [IO.IOException] {
            if (($_.Exception.HResult -band 0xffff) -eq 32) {
                $exclusive = $true
            }
        } finally {
            if ($null -ne $probe) { $probe.Dispose() }
        }
        if (-not $exclusive) {
            throw 'The inherited lock must be a live exclusive operations lock'
        }
    }
}

function Test-PathOverlap {
    param([Parameter(Mandatory)][string]$First, [Parameter(Mandatory)][string]$Second)
    $firstPath = [IO.Path]::GetFullPath($First).TrimEnd('\', '/')
    $secondPath = [IO.Path]::GetFullPath($Second).TrimEnd('\', '/')
    if ($firstPath.Equals($secondPath, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    return $firstPath.StartsWith($secondPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        $secondPath.StartsWith($firstPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Invoke-RestoreExternal {
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$ArgumentList = @())
    if ($null -ne $ExternalInvoker) { return @(& $ExternalInvoker $FilePath $ArgumentList) }
    return @(Invoke-External -FilePath $FilePath -ArgumentList $ArgumentList)
}

function Get-ComposeArguments {
    param([Parameter(Mandatory)]$Config, [Parameter(Mandatory)][string[]]$Command)
    return @('compose', '--project-directory', $Config.RepositoryRoot, '--file', $Config.ComposeFile, '--env-file', $Config.EnvFile, '--profile', 'graph') + $Command
}

function Get-RunningDeploymentServices {
    param([Parameter(Mandatory)]$Config)
    $output = Invoke-RestoreExternal -FilePath 'docker' -ArgumentList (
        Get-ComposeArguments -Config $Config -Command @('ps', '--all', '--services', '--status', 'running', '--status', 'paused', '--status', 'restarting', '--status', 'removing')
    )
    $services = @()
    foreach ($line in $output) {
        $name = ([string]$line).Trim()
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        if ($name -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.-]*$') { throw 'Compose returned an unsafe service name' }
        if ($services -notcontains $name) { $services += $name }
    }
    return @($services)
}

function Invoke-ComposeForServices {
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)][ValidateSet('stop', 'start')][string]$Action,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Services
    )
    if ($Services.Count -eq 0) { return }
    Invoke-RestoreExternal -FilePath 'docker' -ArgumentList (
        Get-ComposeArguments -Config $Config -Command (@($Action) + $Services)
    ) | Out-Null
}

function Test-RestoreHealth {
    param([Parameter(Mandatory)]$Config)
    if ($null -ne $HealthProbe) {
        $result = & $HealthProbe $Config
        if ($result -is [bool]) { return $result }
        return $null -ne $result -and [bool]$result.Healthy
    }
    return Wait-Until -TimeoutSeconds $HealthTimeoutSeconds -IntervalSeconds 5 -Condition {
        (Test-ComposeHealth -Config $Config).Healthy
    }
}

function Assert-RegularNonReparseFile {
    param([Parameter(Mandatory)][string]$LiteralPath)
    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) { throw "Required file was not found: $LiteralPath" }
    $item = Get-Item -LiteralPath $LiteralPath -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse-point files are not allowed: $LiteralPath" }
}

function Assert-SafeArchiveMembers {
    param([Parameter(Mandatory)][string[]]$MemberNames)
    foreach ($rawName in $MemberNames) {
        $name = ([string]$rawName).Trim()
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        if ($name.StartsWith('/') -or $name.StartsWith('\') -or $name -match '^[a-zA-Z]:') {
            throw 'Archive contains an absolute or drive-qualified member'
        }
        if (@($name -split '[\\/]') -contains '..') { throw 'Archive contains a parent traversal member'
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

function Rename-Sibling {
    param([Parameter(Mandatory)][string]$LiteralPath, [Parameter(Mandatory)][string]$Destination)
    $sourceParent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($LiteralPath))
    $destinationParent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Destination))
    if (-not $sourceParent.Equals($destinationParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Restore renames must remain within one sibling directory'
    }
    Rename-Item -LiteralPath $LiteralPath -NewName ([IO.Path]::GetFileName($Destination))
}

if ($KeepStopped -and $null -eq $InheritedOperationLock) {
    throw 'KeepStopped requires an inherited live exclusive operations lock'
}
$config = Get-OperationsConfig -RepositoryRoot $RepositoryRoot -EnvFile $EnvFile -StateRoot $StateRoot -BackupRoot $BackupRoot
if ($KeepStopped) {
    Assert-InheritedOperationsLock -Lock $InheritedOperationLock -StateRoot $config.StateRoot | Out-Null
}
$dataRoot = [IO.Path]::GetFullPath($config.DataRoot).TrimEnd('\', '/')
$backupPath = [IO.Path]::GetFullPath($config.BackupRoot).TrimEnd('\', '/')
if ((Test-PathOverlap $dataRoot $backupPath) -or (Test-PathOverlap $dataRoot $config.StateRoot)) {
    throw 'Deployment data must not overlap backup or operations state roots'
}
$backupParent = [IO.Path]::GetDirectoryName($backupPath)
Assert-BackupTreeSafe -Path $backupPath -AllowedRoot $backupParent | Out-Null

$archivePath = Assert-SafePath -Path $Archive -AllowedRoot $backupPath
$archiveName = [IO.Path]::GetFileName($archivePath)
if ($archiveName -notmatch '^assistant-\d{8}T\d{6}Z\.tar\.gz$' -and
    $archiveName -notmatch '^assistant-week-\d{4}-W\d{2}\.tar\.gz$') {
    throw 'Archive name does not match the deployment backup contract'
}
$checksumPath = "$archivePath.sha256"
Assert-RegularNonReparseFile -LiteralPath $archivePath
Assert-RegularNonReparseFile -LiteralPath $checksumPath
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

$members = @(Invoke-RestoreExternal -FilePath 'tar.exe' -ArgumentList @('-tzf', $archivePath))
Assert-SafeArchiveMembers -MemberNames $members
$verboseEntries = @(Invoke-RestoreExternal -FilePath 'tar.exe' -ArgumentList @('-tvzf', $archivePath))
Assert-NoArchiveLinks -VerboseEntries $verboseEntries
$preExtractionHash = Get-BackupSha256 -LiteralPath $archivePath
if (-not $preExtractionHash.Equals($checksumMatch.Groups['hash'].Value, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Backup checksum changed during restore validation'
}

$qdrantArchivePath = "$archivePath.qdrant-volume.tar.gz"
$qdrantChecksumPath = "$qdrantArchivePath.sha256"
$qdrantActualHash = $null
$useQdrantVolume = Test-Path -LiteralPath $qdrantArchivePath -PathType Leaf
if ($useQdrantVolume) {
    Assert-RegularNonReparseFile -LiteralPath $qdrantChecksumPath
    $qdrantChecksumLine = (Get-Content -LiteralPath $qdrantChecksumPath -Raw).Trim()
    $qdrantChecksumMatch = [regex]::Match($qdrantChecksumLine, '^(?<hash>[0-9a-fA-F]{64})  (?<name>[^\\/]+)$')
    if (-not $qdrantChecksumMatch.Success -or
        -not $qdrantChecksumMatch.Groups['name'].Value.Equals([IO.Path]::GetFileName($qdrantArchivePath), [StringComparison]::Ordinal)) {
        throw 'Qdrant checksum sidecar does not match the selected archive name'
    }
    $qdrantActualHash = Get-BackupSha256 -LiteralPath $qdrantArchivePath
    if (-not $qdrantActualHash.Equals($qdrantChecksumMatch.Groups['hash'].Value, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Qdrant backup checksum mismatch'
    }
}

$archiveHasGraphData = @($members | Where-Object { ([string]$_).Trim() -match '^(?:\.[\\/])?neo4j[\\/]data(?:[\\/]|$)' }).Count -gt 0
if ($archiveHasGraphData) {
    Assert-GraphBackupPair -Archive $archivePath -ArchiveHash $actualHash -QdrantArchive $qdrantArchivePath -QdrantHash $qdrantActualHash
}

$ownsOperationLock = $null -eq $InheritedOperationLock
if ($ownsOperationLock) {
    $operationLock = Enter-OperationsLock -StateRoot $config.StateRoot
} else {
    Assert-InheritedOperationsLock -Lock $InheritedOperationLock -StateRoot $config.StateRoot | Out-Null
    $operationLock = $InheritedOperationLock
}
try {
$dataParent = [IO.Path]::GetDirectoryName($dataRoot)
Assert-BackupTreeSafe -Path $dataRoot -AllowedRoot $dataParent | Out-Null
$runningServices = @(Get-RunningDeploymentServices -Config $config)
$neo4jData = Test-LocalGraphData -Config $config -GraphRunning:($runningServices -contains 'neo4j') -AllowMissing
if ($neo4jData -and -not $archiveHasGraphData) {
    throw 'The selected archive has no Neo4j data for this graph deployment'
}
$graphRestore = $neo4jData -or $archiveHasGraphData
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$stagingPath = "$dataRoot.staging-$stamp-$([Guid]::NewGuid().ToString('N'))"
$rollbackPath = "$dataRoot.rollback-$stamp"
$failedPath = "$dataRoot.failed-$stamp"
$diagnosticPath = "$failedPath.diagnostic"
$qdrantRollbackArchive = Join-Path $config.StateRoot "qdrant-rollback-$stamp.tar.gz"
foreach ($candidate in @($stagingPath, $rollbackPath, $failedPath, $diagnosticPath)) {
    Assert-SafePath -Path $candidate -AllowedRoot $dataParent | Out-Null
    if (Test-Path -LiteralPath $candidate) { throw "Restore diagnostic path already exists: $candidate" }
}

New-Item -ItemType Directory -Path $stagingPath | Out-Null
Invoke-RestoreExternal -FilePath 'tar.exe' -ArgumentList @('-C', $stagingPath, '-xzf', $archivePath) | Out-Null
Assert-BackupTreeSafe -Path $stagingPath -AllowedRoot $dataParent | Out-Null
$requiredDirectories = @(if ($useQdrantVolume) { 'app' } else { 'app'; 'qdrant' })
if ($archiveHasGraphData) { $requiredDirectories += 'neo4j/data' }
foreach ($requiredDirectory in $requiredDirectories) {
    if (-not (Test-Path -LiteralPath (Join-Path $stagingPath $requiredDirectory) -PathType Container)) {
        throw "Archive is missing required directory: $requiredDirectory"
    }
}

$rollbackCreated = $false
$candidateInstalled = $false
$qdrantCandidateInstalled = $false
$qdrantRollbackCreated = $false
$qdrantVolumeName = $null
$qdrantHelperImage = $null
try {
    Invoke-ComposeForServices -Config $config -Action stop -Services $runningServices
    if (($KeepStopped -or $graphRestore) -and @(Get-RunningDeploymentServices -Config $config).Count -ne 0) {
        throw 'Deployment services remain running after stop'
    }
    if ($useQdrantVolume) {
        New-Item -ItemType Directory -Force -Path $config.StateRoot | Out-Null
        Assert-SafePath -Path $qdrantRollbackArchive -AllowedRoot $config.StateRoot | Out-Null
        if (Test-Path -LiteralPath $qdrantRollbackArchive) {
            throw "Qdrant rollback archive already exists: $qdrantRollbackArchive"
        }
        $qdrantVolumeName = Get-QdrantVolumeName -Config $config
        $qdrantHelperImage = Get-QdrantHelperImage -Config $config -ExternalInvoker $ExternalInvoker
        if (-not (Test-QdrantVolumeExists -Name $qdrantVolumeName -ExternalInvoker $ExternalInvoker)) {
            throw "Production Qdrant volume was not found: $qdrantVolumeName"
        }
        Export-QdrantVolume -Name $qdrantVolumeName -Archive $qdrantRollbackArchive -HelperImage $qdrantHelperImage -ExternalInvoker $ExternalInvoker | Out-Null
        $qdrantRollbackCreated = $true
    }
    Rename-Sibling -LiteralPath $dataRoot -Destination $rollbackPath
    $rollbackCreated = $true
    Rename-Sibling -LiteralPath $stagingPath -Destination $dataRoot
    $candidateInstalled = $true
    if ($useQdrantVolume) {
        Clear-QdrantVolume -Name $qdrantVolumeName -HelperImage $qdrantHelperImage -ExternalInvoker $ExternalInvoker
        Import-QdrantVolume -Name $qdrantVolumeName -Archive $qdrantArchivePath -HelperImage $qdrantHelperImage -RequireEmpty -ExternalInvoker $ExternalInvoker
        $qdrantCandidateInstalled = $true
    }
    if (-not $KeepStopped) {
        Invoke-ComposeForServices -Config $config -Action start -Services $runningServices
        if (-not (Test-RestoreHealth -Config $config)) { throw 'Deployment did not become healthy after the restore swap' }
    }
} catch {
    $restoreError = $_
    $compensationErrors = @()
    $rollbackRestored = $false
    $rollbackSafe = $true
    if ($rollbackCreated) {
        try {
            Invoke-ComposeForServices -Config $config -Action stop -Services $runningServices
            if ($graphRestore -and @(Get-RunningDeploymentServices -Config $config).Count -ne 0) {
                throw 'Deployment services remain running during graph restore rollback'
            }
        } catch {
            $compensationErrors += "stop failed services: $($_.Exception.Message)"
            if ($graphRestore) { $rollbackSafe = $false }
        }
    }
    if ($rollbackCreated -and $rollbackSafe) {
        if ($candidateInstalled -and (Test-Path -LiteralPath $dataRoot -PathType Container)) {
            try {
                Rename-Sibling -LiteralPath $dataRoot -Destination $failedPath
            } catch {
                $compensationErrors += "retain failed candidate: $($_.Exception.Message)"
            }
        }
        if (Test-Path -LiteralPath $rollbackPath -PathType Container) {
            try {
                Rename-Sibling -LiteralPath $rollbackPath -Destination $dataRoot
                $rollbackRestored = $true
            } catch {
                $compensationErrors += "restore rollback: $($_.Exception.Message)"
            }
        } else {
            $compensationErrors += 'restore rollback: rollback directory is missing'
        }
    }
    if ($useQdrantVolume -and $qdrantRollbackCreated -and $rollbackSafe) {
        try {
            Clear-QdrantVolume -Name $qdrantVolumeName -HelperImage $qdrantHelperImage -ExternalInvoker $ExternalInvoker
            Import-QdrantVolume -Name $qdrantVolumeName -Archive $qdrantRollbackArchive -HelperImage $qdrantHelperImage -RequireEmpty -ExternalInvoker $ExternalInvoker
            $qdrantCandidateInstalled = $false
        } catch {
            $compensationErrors += "restore Qdrant rollback: $($_.Exception.Message)"
        }
    }
    $restartSucceeded = $false
    if (-not $KeepStopped -and $rollbackSafe -and (-not $graphRestore -or $compensationErrors.Count -eq 0)) {
        try {
            Invoke-ComposeForServices -Config $config -Action start -Services $runningServices
            $restartSucceeded = $true
        } catch {
            $compensationErrors += "restart services: $($_.Exception.Message)"
        }
    }
    if ($rollbackCreated -and $rollbackRestored -and $restartSucceeded) {
        try {
            if (-not (Test-RestoreHealth -Config $config)) {
                $compensationErrors += 'rollback health: restored deployment did not become healthy'
            }
        } catch {
            $compensationErrors += "rollback health: $($_.Exception.Message)"
        }
    }
    $diagnostic = [ordered]@{
        failed_at = (Get-Date).ToUniversalTime().ToString('o')
        restore_error = Protect-LogText $restoreError.Exception.Message
        compensation_error = if ($compensationErrors.Count -eq 0) { $null } else { Protect-LogText ($compensationErrors -join '; ') }
        compensation_errors = @($compensationErrors | ForEach-Object { Protect-LogText $_ })
        failed_candidate = if (Test-Path -LiteralPath $failedPath) { $failedPath } else { $null }
        retained_staging = if (Test-Path -LiteralPath $stagingPath) { $stagingPath } else { $null }
        qdrant_rollback = if ($qdrantRollbackCreated) { $qdrantRollbackArchive } else { $null }
        qdrant_candidate_installed = $qdrantCandidateInstalled
    } | ConvertTo-Json
    try { [IO.File]::WriteAllText($diagnosticPath, $diagnostic, (New-Object System.Text.UTF8Encoding($false))) } catch {}
    if ($compensationErrors.Count -gt 0) {
        throw "Restore failed: $($restoreError.Exception.Message). Compensation issues: $($compensationErrors -join '; ')"
    }
    throw $restoreError
}

[PSCustomObject]@{
    RestoredArchive = $archivePath
    DataRoot = $dataRoot
    Rollback = $rollbackPath
    QdrantVolume = $qdrantVolumeName
    QdrantRollback = if ($qdrantRollbackCreated) { $qdrantRollbackArchive } else { $null }
    KeptStopped = [bool]$KeepStopped
    PreviouslyRunningServices = @($runningServices)
}
} finally {
    if ($ownsOperationLock) {
        Exit-OperationsLock -Lock $operationLock
    }
}
