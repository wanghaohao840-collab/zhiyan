#Requires -Version 5.1
Set-StrictMode -Version Latest

$operationsModule = Join-Path $PSScriptRoot 'Operations.Common.psm1'
Import-Module $operationsModule

$script:DailyPattern = '^assistant-(?<stamp>\d{8}T\d{6}Z)\.tar\.gz$'
$script:WeeklyPattern = '^assistant-week-(?<year>\d{4})-W(?<week>\d{2})\.tar\.gz$'

function Assert-NoReparseAncestors {
    param([Parameter(Mandatory)][string]$Path)

    $current = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $current)) {
        $current = [IO.Path]::GetDirectoryName($current)
    }
    while (-not [string]::IsNullOrWhiteSpace($current)) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Reparse-point paths are not allowed: $current"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current.TrimEnd('\', '/'))
        if ([string]::IsNullOrWhiteSpace($parent) -or
            $parent.Equals($current, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $current = $parent
    }
}

function Assert-BackupPathAncestorsSafe {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    Assert-NoReparseAncestors -Path $Path
    return [IO.Path]::GetFullPath($Path)
}

function Get-IsoWeekInfo {
    [CmdletBinding()]
    param([Parameter(Mandatory)][DateTime]$UtcDate)

    $day = [int]$UtcDate.DayOfWeek
    if ($day -eq 0) { $day = 7 }
    $thursday = $UtcDate.Date.AddDays(4 - $day)
    $calendar = [Globalization.CultureInfo]::InvariantCulture.Calendar
    $week = $calendar.GetWeekOfYear(
        $thursday,
        [Globalization.CalendarWeekRule]::FirstFourDayWeek,
        [DayOfWeek]::Monday
    )
    return [PSCustomObject]@{ Year = $thursday.Year; Week = $week }
}

function Test-RegularNonReparseFile {
    param([Parameter(Mandatory)][string]$LiteralPath)

    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) {
        return $false
    }
    $item = Get-Item -LiteralPath $LiteralPath -Force
    return -not [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Get-BackupSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$LiteralPath)

    $fileHash = Get-Command Get-FileHash -ErrorAction SilentlyContinue
    if ($null -ne $fileHash) {
        return (Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256).Hash
    }

    $stream = [IO.File]::OpenRead($LiteralPath)
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '')
        } finally {
            $algorithm.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Assert-BackupTreeSafe {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$AllowedRoot
    )

    $safePath = Assert-SafePath -Path $Path -AllowedRoot $AllowedRoot -AllowRoot
    Assert-NoReparseAncestors -Path $safePath
    if (-not (Test-Path -LiteralPath $safePath -PathType Container)) {
        throw "Directory was not found: $safePath"
    }
    $rootItem = Get-Item -LiteralPath $safePath -Force
    if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Reparse points are not allowed: $safePath"
    }
    foreach ($item in @(Get-ChildItem -LiteralPath $safePath -Force -Recurse)) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Reparse points are not allowed: $($item.FullName)"
        }
    }
    return $safePath
}

function Get-BackupNamePattern {
    param([Parameter(Mandatory)][string]$Prefix)

    if ($Prefix -eq 'assistant-') {
        return $script:DailyPattern
    }
    if ($Prefix -eq 'assistant-week-') {
        return $script:WeeklyPattern
    }
    throw "Unsupported backup prefix: $Prefix"
}

function Get-CompleteBackupSets {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Directory,
        [Parameter(Mandatory)][string]$Prefix
    )

    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        return @()
    }
    $directoryItem = Get-Item -LiteralPath $Directory -Force
    Assert-NoReparseAncestors -Path $directoryItem.FullName
    if ($directoryItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Backup directory must not be a reparse point: $Directory"
    }
    $pattern = Get-BackupNamePattern -Prefix $Prefix
    $sets = @()
    foreach ($archiveItem in @(Get-ChildItem -LiteralPath $directoryItem.FullName -File -Force)) {
        $match = [regex]::Match($archiveItem.Name, $pattern)
        if (-not $match.Success) {
            continue
        }
        $archive = $archiveItem.FullName
        $checksum = "$archive.sha256"
        $metadata = "$archive.meta"
        if (-not (Test-RegularNonReparseFile -LiteralPath $archive) -or
            -not (Test-RegularNonReparseFile -LiteralPath $checksum) -or
            -not (Test-RegularNonReparseFile -LiteralPath $metadata)) {
            continue
        }
        $key = if ($Prefix -eq 'assistant-') {
            $match.Groups['stamp'].Value
        } else {
            '{0}-W{1}' -f $match.Groups['year'].Value, $match.Groups['week'].Value
        }
        $qdrantArchive = "$archive.qdrant-volume.tar.gz"
        $qdrantChecksum = "$qdrantArchive.sha256"
        $hasQdrantArchive = Test-RegularNonReparseFile -LiteralPath $qdrantArchive
        $hasQdrantChecksum = Test-RegularNonReparseFile -LiteralPath $qdrantChecksum
        if ($hasQdrantArchive -xor $hasQdrantChecksum) {
            continue
        }
        $sets += [PSCustomObject]@{
            Key = $key
            Archive = $archive
            Checksum = $checksum
            Metadata = $metadata
            QdrantArchive = if ($hasQdrantArchive) { $qdrantArchive } else { $null }
            QdrantChecksum = if ($hasQdrantChecksum) { $qdrantChecksum } else { $null }
        }
    }
    return @($sets | Sort-Object -Property Key -Descending)
}

function Get-RetentionPlan {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Directory,
        [Parameter(Mandatory)][string]$Prefix,
        [Parameter(Mandatory)][ValidateRange(0, 10000)][int]$Keep
    )

    $sets = @(Get-CompleteBackupSets -Directory $Directory -Prefix $Prefix)
    return [PSCustomObject]@{
        Keep = @($sets | Select-Object -First $Keep)
        Remove = if ($Keep -eq 0) { @($sets) } else { @($sets | Select-Object -Skip $Keep) }
    }
}

function Remove-BackupSet {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$BackupSet,
        [Parameter(Mandatory)][string]$AllowedRoot
    )

    $root = [IO.Path]::GetFullPath($AllowedRoot).TrimEnd('\', '/')
    foreach ($property in @('Archive', 'Checksum', 'Metadata', 'QdrantArchive', 'QdrantChecksum')) {
        $literalPath = [string]$BackupSet.$property
        if ([string]::IsNullOrWhiteSpace($literalPath)) {
            continue
        }
        $safePath = Assert-SafePath -Path $literalPath -AllowedRoot $root
        $parent = [IO.Path]::GetDirectoryName($safePath).TrimEnd('\', '/')
        if (-not $parent.Equals($root, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Backup set file must be directly below its allowed root: $safePath"
        }
        if (-not (Test-RegularNonReparseFile -LiteralPath $safePath)) {
            throw "Backup set file is missing, non-regular, or a reparse point: $safePath"
        }
        Assert-NoReparseAncestors -Path $safePath
        Assert-SafePath -Path $safePath -AllowedRoot $root | Out-Null
        Remove-Item -LiteralPath $safePath -Force
    }
}

function Test-LocalGraphData {
    param(
        [Parameter(Mandatory)]$Config,
        [switch]$GraphRunning,
        [switch]$AllowMissing
    )
    $enabled = (Read-DeployEnvValue -EnvFile $Config.EnvFile -Name 'ZHIYAN_GRAPH_ENABLED') -in @('1', 'true', 'yes', 'on')
    $graphDirectory = Join-Path $Config.DataRoot 'neo4j\data'
    $present = Test-Path -LiteralPath $graphDirectory -PathType Container
    if ($enabled) {
        $uri = Read-DeployEnvValue -EnvFile $Config.EnvFile -Name 'NEO4J_URI'
        if ($uri -notmatch '^(neo4j|bolt)://neo4j(:7687)?/?$') {
            throw 'The deployment graph must use the locally backed-up Neo4j service'
        }
    }
    if (($GraphRunning -or ($enabled -and -not $AllowMissing)) -and -not $present) {
        throw 'The Neo4j data directory is missing from the deployment data root'
    }
    if (($present -or $enabled -or $GraphRunning) -and -not [string]::IsNullOrWhiteSpace($env:DEPLOY_DATA_ROOT)) {
        $overlay = $env:DEPLOY_DATA_ROOT
        if (-not [IO.Path]::IsPathRooted($overlay)) {
            $overlay = Join-Path $Config.RepositoryRoot $overlay
        }
        $overlay = [IO.Path]::GetFullPath($overlay).TrimEnd('\', '/')
        if (-not $overlay.Equals($Config.DataRoot.TrimEnd('\', '/'), [StringComparison]::OrdinalIgnoreCase)) {
            throw 'The Neo4j bind mount must use the same deployment data root as the cold backup'
        }
    }
    return ($present -or $enabled -or $GraphRunning)
}

function Assert-GraphBackupPair {
    param(
        [Parameter(Mandatory)][string]$Archive,
        [Parameter(Mandatory)][string]$ArchiveHash,
        [string]$QdrantArchive,
        [string]$QdrantHash
    )
    if ([string]::IsNullOrWhiteSpace($QdrantHash) -or
        -not (Test-RegularNonReparseFile -LiteralPath "$Archive.meta")) {
        throw 'A graph restore requires a verified SQLite, Neo4j and Qdrant backup set'
    }
    $metadata = Get-Content -LiteralPath "$Archive.meta" -Raw | ConvertFrom-Json
    $required = @('neo4j_data', 'archive', 'sha256', 'qdrant_archive', 'qdrant_sha256')
    if ($null -eq $metadata -or @($required | Where-Object { $null -eq $metadata.PSObject.Properties[$_] }).Count -ne 0 -or
        $metadata.neo4j_data -cne 'neo4j/data' -or
        $metadata.archive -cne [IO.Path]::GetFileName($Archive) -or
        $metadata.sha256 -ne $ArchiveHash -or
        $metadata.qdrant_archive -cne [IO.Path]::GetFileName($QdrantArchive) -or
        $metadata.qdrant_sha256 -ne $QdrantHash) {
        throw 'The Neo4j backup metadata does not match the selected archives'
    }
}

Export-ModuleMember -Function @(
    'Assert-BackupPathAncestorsSafe',
    'Assert-BackupTreeSafe',
    'Assert-GraphBackupPair',
    'Get-BackupSha256',
    'Get-CompleteBackupSets',
    'Get-IsoWeekInfo',
    'Get-RetentionPlan',
    'Remove-BackupSet',
    'Test-LocalGraphData'
)
