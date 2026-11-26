#Requires -Version 5.1
[CmdletBinding()]
param(
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
    if ($firstPath.Equals($secondPath, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $firstPath.StartsWith($secondPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        $secondPath.StartsWith($firstPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Invoke-BackupExternal {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    if ($null -ne $ExternalInvoker) {
        return @(& $ExternalInvoker $FilePath $ArgumentList)
    }
    return @(Invoke-External -FilePath $FilePath -ArgumentList $ArgumentList)
}

function Get-ComposeArguments {
    param([Parameter(Mandatory)]$Config, [Parameter(Mandatory)][string[]]$Command)
    return @(
        'compose',
        '--project-directory', $Config.RepositoryRoot,
        '--file', $Config.ComposeFile,
        '--env-file', $Config.EnvFile,
        '--profile', 'graph'
    ) + $Command
}

function Get-RunningDeploymentServices {
    param([Parameter(Mandatory)]$Config)

    $output = Invoke-BackupExternal -FilePath 'docker' -ArgumentList (
        Get-ComposeArguments -Config $Config -Command @('ps', '--all', '--services', '--status', 'running', '--status', 'paused', '--status', 'restarting', '--status', 'removing')
    )
    $services = @()
    foreach ($line in $output) {
        $name = ([string]$line).Trim()
        if ([string]::IsNullOrWhiteSpace($name)) {
            continue
        }
        if ($name -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.-]*$') {
            throw "Compose returned an unsafe service name"
        }
        if ($services -notcontains $name) {
            $services += $name
        }
    }
    return @($services)
}

function Invoke-ComposeForServices {
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)][ValidateSet('stop', 'start')][string]$Action,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Services
    )
    if ($Services.Count -eq 0) {
        return
    }
    Invoke-BackupExternal -FilePath 'docker' -ArgumentList (
        Get-ComposeArguments -Config $Config -Command (@($Action) + $Services)
    ) | Out-Null
}

function Test-BackupHealth {
    param([Parameter(Mandatory)]$Config)

    if ($null -ne $HealthProbe) {
        $result = & $HealthProbe $Config
        if ($result -is [bool]) {
            return $result
        }
        return $null -ne $result -and [bool]$result.Healthy
    }
    return Wait-Until -TimeoutSeconds $HealthTimeoutSeconds -IntervalSeconds 5 -Condition {
        (Test-ComposeHealth -Config $Config).Healthy
    }
}

function Write-BackupMetadata {
    param(
        [Parameter(Mandatory)][string]$Archive,
        [Parameter(Mandatory)][string]$Hash,
        [Parameter(Mandatory)][DateTime]$CreatedUtc,
        [Parameter(Mandatory)][ValidateSet('daily', 'weekly')][string]$Kind,
        [string]$QdrantArchive,
        [string]$QdrantHash,
        [string]$QdrantVolumeName,
        [switch]$Neo4jDataIncluded
    )
    $payload = [ordered]@{
        archive = [IO.Path]::GetFileName($Archive)
        sha256 = $Hash
        created_at = $CreatedUtc.ToString('o')
        kind = $Kind
        format = if ([string]::IsNullOrWhiteSpace($QdrantArchive)) { 1 } else { 2 }
        qdrant_volume_name = $QdrantVolumeName
        qdrant_archive = if ([string]::IsNullOrWhiteSpace($QdrantArchive)) { $null } else { [IO.Path]::GetFileName($QdrantArchive) }
        qdrant_sha256 = $QdrantHash
        neo4j_data = if ($Neo4jDataIncluded) { 'neo4j/data' } else { $null }
    } | ConvertTo-Json
    [IO.File]::WriteAllText(
        "$Archive.meta",
        $payload,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Write-BackupChecksum {
    param([Parameter(Mandatory)][string]$archive)

    if ($null -ne (Get-Command Get-FileHash -ErrorAction SilentlyContinue)) {
        $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    } else {
        $hash = (Get-BackupSha256 -LiteralPath $archive).ToLowerInvariant()
    }
    Set-Content -LiteralPath "$archive.sha256" -Encoding ASCII -Value "$hash  $([IO.Path]::GetFileName($archive))"
    return $hash
}

function Get-BackupRetentionCount {
    param(
        [Parameter(Mandatory)][string]$EnvFile,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][int]$Default
    )
    $raw = Read-DeployEnvValue -EnvFile $EnvFile -Name $Name
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $Default
    }
    $parsed = 0
    if (-not [int]::TryParse($raw, [ref]$parsed) -or $parsed -lt 1 -or $parsed -gt 365) {
        throw "$Name must be an integer between 1 and 365"
    }
    return $parsed
}

if ($KeepStopped -and $null -eq $InheritedOperationLock) {
    throw 'KeepStopped requires an inherited live exclusive operations lock'
}
$config = Get-OperationsConfig -RepositoryRoot $RepositoryRoot -EnvFile $EnvFile -StateRoot $StateRoot -BackupRoot $BackupRoot
$dataRoot = $config.DataRoot
$backupPath = $config.BackupRoot
if ((Test-PathOverlap $dataRoot $backupPath) -or (Test-PathOverlap $dataRoot $config.StateRoot)) {
    throw 'Deployment data must not overlap backup or operations state roots'
}

$ownsOperationLock = $null -eq $InheritedOperationLock
if ($ownsOperationLock) {
    $operationLock = Enter-OperationsLock -StateRoot $config.StateRoot
} else {
    Assert-InheritedOperationsLock -Lock $InheritedOperationLock -StateRoot $config.StateRoot | Out-Null
    $operationLock = $InheritedOperationLock
}
try {

$dataParent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($dataRoot))
Assert-BackupTreeSafe -Path $dataRoot -AllowedRoot $dataParent | Out-Null
$backupParent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($backupPath))
Assert-BackupPathAncestorsSafe -Path $backupPath | Out-Null
New-Item -ItemType Directory -Force -Path $backupPath | Out-Null
Assert-BackupTreeSafe -Path $backupPath -AllowedRoot $backupParent | Out-Null

$dailyDirectory = Join-Path $backupPath 'daily'
$weeklyDirectory = Join-Path $backupPath 'weekly'
New-Item -ItemType Directory -Force -Path $dailyDirectory | Out-Null
New-Item -ItemType Directory -Force -Path $weeklyDirectory | Out-Null
Assert-BackupTreeSafe -Path $dailyDirectory -AllowedRoot $backupPath | Out-Null
Assert-BackupTreeSafe -Path $weeklyDirectory -AllowedRoot $backupPath | Out-Null

$now = (Get-Date).ToUniversalTime()
$stamp = $now.ToString('yyyyMMddTHHmmssZ')
$archive = Join-Path $dailyDirectory "assistant-$stamp.tar.gz"
$qdrantArchive = "$archive.qdrant-volume.tar.gz"
foreach ($candidate in @($archive, "$archive.sha256", "$archive.meta", $qdrantArchive, "$qdrantArchive.sha256")) {
    if (Test-Path -LiteralPath $candidate) {
        throw "Refusing to overwrite an existing backup file: $candidate"
    }
}

$runningServices = @(Get-RunningDeploymentServices -Config $config)
$neo4jData = Test-LocalGraphData -Config $config -GraphRunning:($runningServices -contains 'neo4j')
$operationError = $null
$restartError = $null
try {
    Invoke-ComposeForServices -Config $config -Action stop -Services $runningServices
    if (($KeepStopped -or $neo4jData) -and @(Get-RunningDeploymentServices -Config $config).Count -ne 0) {
        throw 'Deployment services remain running after stop'
    }
    $qdrantHash = $null
    $qdrantVolumeName = Get-QdrantVolumeName -Config $config
    if (Test-QdrantVolumeExists -Name $qdrantVolumeName -ExternalInvoker $ExternalInvoker) {
        $helperImage = Get-QdrantHelperImage -Config $config -ExternalInvoker $ExternalInvoker
        Export-QdrantVolume -Name $qdrantVolumeName -Archive $qdrantArchive -HelperImage $helperImage -ExternalInvoker $ExternalInvoker | Out-Null
        $qdrantHash = Write-BackupChecksum -Archive $qdrantArchive
    } elseif ($neo4jData -or $null -eq $ExternalInvoker) {
        throw "Required Qdrant volume was not found: $qdrantVolumeName"
    }
    Invoke-BackupExternal -FilePath 'tar.exe' -ArgumentList @('-C', $dataRoot, '-czf', $archive, '.') | Out-Null
    $hash = Write-BackupChecksum -Archive $archive
    Write-BackupMetadata -Archive $archive -Hash $hash -CreatedUtc $now -Kind daily `
        -QdrantArchive $(if ($null -ne $qdrantHash) { $qdrantArchive } else { $null }) `
        -QdrantHash $qdrantHash -QdrantVolumeName $qdrantVolumeName -Neo4jDataIncluded:$neo4jData

    $createdSet = @(Get-CompleteBackupSets -Directory $dailyDirectory -Prefix 'assistant-' | Where-Object {
        $_.Archive.Equals($archive, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($createdSet.Count -ne 1) {
        throw 'Daily backup archive or one of its sidecars is incomplete'
    }
} catch {
    $operationError = $_
} finally {
    try {
        if (-not $KeepStopped) {
            Invoke-ComposeForServices -Config $config -Action start -Services $runningServices
        }
    } catch {
        $restartError = $_
    }
}

if ($null -ne $operationError) {
    throw $operationError
}
if ($null -ne $restartError) {
    throw $restartError
}
if ($KeepStopped) {
    # Parent owns the live lock, restart/recovery and final backup retention.
    [PSCustomObject]@{
        Archive = $archive
        Checksum = "$archive.sha256"
        Metadata = "$archive.meta"
        QdrantArchive = if (Test-Path -LiteralPath $qdrantArchive -PathType Leaf) { $qdrantArchive } else { $null }
        QdrantChecksum = if (Test-Path -LiteralPath "$qdrantArchive.sha256" -PathType Leaf) { "$qdrantArchive.sha256" } else { $null }
        KeptStopped = $true
        PreviouslyRunningServices = @($runningServices)
    }
    return
}
if (-not (Test-BackupHealth -Config $config)) {
    throw 'Deployment did not become healthy after the cold backup'
}

$iso = Get-IsoWeekInfo -UtcDate $now
$weeklyName = 'assistant-week-{0:D4}-W{1:D2}.tar.gz' -f $iso.Year, $iso.Week
$weeklyArchive = Join-Path $weeklyDirectory $weeklyName
$weeklyQdrantArchive = "$weeklyArchive.qdrant-volume.tar.gz"
$weeklySet = @(Get-CompleteBackupSets -Directory $weeklyDirectory -Prefix 'assistant-week-' | Where-Object {
    $_.Archive.Equals($weeklyArchive, [StringComparison]::OrdinalIgnoreCase)
})
if ($weeklySet.Count -eq 0) {
    foreach ($candidate in @($weeklyArchive, "$weeklyArchive.sha256", "$weeklyArchive.meta", $weeklyQdrantArchive, "$weeklyQdrantArchive.sha256")) {
        if (Test-Path -LiteralPath $candidate) {
            throw "Incomplete weekly set already exists; refusing to overwrite it: $candidate"
        }
    }
    Copy-Item -LiteralPath $archive -Destination $weeklyArchive
    $weeklyQdrantHash = $null
    if (Test-Path -LiteralPath $qdrantArchive -PathType Leaf) {
        Copy-Item -LiteralPath $qdrantArchive -Destination $weeklyQdrantArchive
        $weeklyQdrantHash = Write-BackupChecksum -Archive $weeklyQdrantArchive
    }
    $weeklyHash = Write-BackupChecksum -Archive $weeklyArchive
    Write-BackupMetadata -Archive $weeklyArchive -Hash $weeklyHash -CreatedUtc $now -Kind weekly `
        -QdrantArchive $(if ($null -ne $weeklyQdrantHash) { $weeklyQdrantArchive } else { $null }) `
        -QdrantHash $weeklyQdrantHash -QdrantVolumeName $qdrantVolumeName -Neo4jDataIncluded:$neo4jData
    $weeklySet = @(Get-CompleteBackupSets -Directory $weeklyDirectory -Prefix 'assistant-week-' | Where-Object {
        $_.Archive.Equals($weeklyArchive, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($weeklySet.Count -ne 1) {
        throw 'Weekly backup archive or one of its sidecars is incomplete'
    }
}

$dailyRetention = Get-BackupRetentionCount -EnvFile $config.EnvFile -Name 'BACKUP_DAILY_RETENTION' -Default 7
$weeklyRetention = Get-BackupRetentionCount -EnvFile $config.EnvFile -Name 'BACKUP_WEEKLY_RETENTION' -Default 4
$dailyPlan = Get-RetentionPlan -Directory $dailyDirectory -Prefix 'assistant-' -Keep $dailyRetention
$weeklyPlan = Get-RetentionPlan -Directory $weeklyDirectory -Prefix 'assistant-week-' -Keep $weeklyRetention
foreach ($set in @($dailyPlan.Remove)) {
    Remove-BackupSet -BackupSet $set -AllowedRoot $dailyDirectory
}
foreach ($set in @($weeklyPlan.Remove)) {
    Remove-BackupSet -BackupSet $set -AllowedRoot $weeklyDirectory
}

[PSCustomObject]@{
    Archive = $archive
    Checksum = "$archive.sha256"
    Metadata = "$archive.meta"
    QdrantArchive = if (Test-Path -LiteralPath $qdrantArchive -PathType Leaf) { $qdrantArchive } else { $null }
    QdrantChecksum = if (Test-Path -LiteralPath "$qdrantArchive.sha256" -PathType Leaf) { "$qdrantArchive.sha256" } else { $null }
    WeeklyArchive = $weeklyArchive
}
} finally {
    if ($ownsOperationLock) {
        Exit-OperationsLock -Lock $operationLock
    }
}
