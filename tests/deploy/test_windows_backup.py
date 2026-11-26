from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).parents[2]
WINDOWS = ROOT / "deploy" / "windows"
COMMON = WINDOWS / "Backup.Common.psm1"
BACKUP = WINDOWS / "Backup-Deployment.ps1"
RESTORE = WINDOWS / "Restore-Deployment.ps1"
DRILL = WINDOWS / "Invoke-RestoreDrill.ps1"
UPDATE = WINDOWS / "Update-Deployment.ps1"
OPERATIONS = WINDOWS / "Operations.Common.psm1"


def run_ps(script: str, *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def ps_quote(value: Path | str) -> str:
    return str(value).replace("'", "''")


def make_set(directory: Path, archive_name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".sha256", ".meta"):
        (directory / f"{archive_name}{suffix}").write_text("test", encoding="ascii")


def make_repository(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    shutil.copyfile(ROOT / "compose.yaml", repository / "compose.yaml")
    data = tmp_path / "deployment-data"
    (data / "app").mkdir(parents=True)
    (data / "qdrant" / "collections").mkdir(parents=True)
    env_file = repository / "deploy.env"
    env_file.write_text(f"DEPLOY_DATA_ROOT={data}\n", encoding="utf-8")
    return repository, data, env_file


def write_checksum(archive: Path) -> None:
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (archive.parent / f"{archive.name}.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="ascii"
    )


def make_drill_backup(backup_root: Path) -> Path:
    archive = backup_root / "daily" / "assistant-20260811T030000Z.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"synthetic drill archive")
    write_checksum(archive)
    (archive.parent / f"{archive.name}.meta").write_text(
        '{"kind":"daily","format":1}', encoding="utf-8"
    )
    return archive


def write_drill_harness(
    path: Path,
    *,
    repository: Path,
    env_file: Path,
    state: Path,
    backups: Path,
    calls: Path,
    captured_env: Path,
    fail_smoke: bool,
    secret: str,
) -> None:
    failure_line = (
        f"    throw 'forced smoke failure {secret}'\n" if fail_smoke else "    return\n"
    )
    path.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$runner = {\n"
        "  param([string]$FilePath, [string[]]$ArgumentList)\n"
        f"  [PSCustomObject]@{{ FilePath=$FilePath; Args=@($ArgumentList) }} | ConvertTo-Json -Compress | Add-Content -LiteralPath '{ps_quote(calls)}'\n"
        "  if ($FilePath -eq 'tar.exe') {\n"
        "    if ($ArgumentList -contains '-tzf') { './app/'; './qdrant/'; return }\n"
        "    if ($ArgumentList -contains '-tvzf') { 'drwxr-xr-x user/group 0 2026-01-01 00:00 ./app/'; 'drwxr-xr-x user/group 0 2026-01-01 00:00 ./qdrant/'; return }\n"
        "    if ($ArgumentList -contains '-xzf') {\n"
        "      $target = $ArgumentList[[Array]::IndexOf($ArgumentList, '-C') + 1]\n"
        "      New-Item -ItemType Directory -Path (Join-Path $target 'app') -Force | Out-Null\n"
        "      New-Item -ItemType Directory -Path (Join-Path $target 'qdrant') -Force | Out-Null\n"
        "      Set-Content -LiteralPath (Join-Path $target 'app\\restored.txt') -Value restored\n"
        "      return\n"
        "    }\n"
        "  }\n"
        "  if ($FilePath -eq 'docker') {\n"
        "    if ($ArgumentList -contains 'ps') { 'app|running|healthy'; 'qdrant|running|healthy' }\n"
        "    return\n"
        "  }\n"
        "  if ($FilePath -like '*python.exe') {\n"
        "    $envPath = $ArgumentList[[Array]::IndexOf($ArgumentList, '--env-file') + 1]\n"
        "    if (-not ([IO.File]::GetAccessControl($envPath)).AreAccessRulesProtected) { throw 'drill env ACL still inherits access' }\n"
        f"    Get-Content -LiteralPath $envPath -Raw | Set-Content -LiteralPath '{ps_quote(captured_env)}' -NoNewline\n"
        + failure_line
        + "  }\n"
        "  throw \"unexpected external command: $FilePath\"\n"
        "}\n"
        f"& '{ps_quote(DRILL)}' -RepositoryRoot '{ps_quote(repository)}' -EnvFile '{ps_quote(env_file)}' -StateRoot '{ps_quote(state)}' -BackupRoot '{ps_quote(backups)}' -ExternalInvoker $runner -HealthTimeoutSeconds 5\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("prefix", "archive_names", "keep"),
    [
        (
            "assistant-",
            [f"assistant-202607{day:02d}T030000Z.tar.gz" for day in range(1, 11)],
            7,
        ),
        (
            "assistant-week-",
            [f"assistant-week-2026-W{week:02d}.tar.gz" for week in range(1, 7)],
            4,
        ),
    ],
)
def test_retention_keeps_newest_complete_sets(
    tmp_path: Path, prefix: str, archive_names: list[str], keep: int
):
    for archive_name in archive_names:
        make_set(tmp_path, archive_name)

    result = run_ps(
        f"Import-Module '{ps_quote(COMMON)}' -Force; "
        f"$plan = Get-RetentionPlan -Directory '{ps_quote(tmp_path)}' "
        f"-Prefix '{prefix}' -Keep {keep}; "
        "[PSCustomObject]@{ "
        "Keep=@($plan.Keep | ForEach-Object { [IO.Path]::GetFileName($_.Archive) }); "
        "Remove=@($plan.Remove | ForEach-Object { [IO.Path]::GetFileName($_.Archive) }) "
        "} | ConvertTo-Json -Compress"
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["Keep"] == list(reversed(archive_names[-keep:]))
    assert plan["Remove"] == list(reversed(archive_names[:-keep]))


def test_retention_ignores_incomplete_and_inexact_sets(tmp_path: Path):
    make_set(tmp_path, "assistant-20260701T030000Z.tar.gz")
    (tmp_path / "assistant-20260702T030000Z.tar.gz").write_text("partial")
    make_set(tmp_path, "assistant-20260703T030000Z.tar.gz.extra")

    result = run_ps(
        f"Import-Module '{ps_quote(COMMON)}' -Force; "
        f"$plan = Get-RetentionPlan -Directory '{ps_quote(tmp_path)}' "
        "-Prefix 'assistant-' -Keep 0; "
        "@($plan.Remove).Count"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_retention_rejects_reparse_point_sidecars(tmp_path: Path):
    target = tmp_path / "real-checksum"
    target.write_text("test", encoding="ascii")
    archive = "assistant-20260701T030000Z.tar.gz"
    (tmp_path / archive).write_text("test", encoding="ascii")
    (tmp_path / f"{archive}.meta").write_text("test", encoding="ascii")
    link = tmp_path / f"{archive}.sha256"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    result = run_ps(
        f"Import-Module '{ps_quote(COMMON)}' -Force; "
        f"$plan = Get-RetentionPlan -Directory '{ps_quote(tmp_path)}' "
        "-Prefix 'assistant-' -Keep 0; @($plan.Remove).Count"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"


@pytest.mark.parametrize(
    ("date", "expected"),
    [
        ("2018-12-31T12:00:00Z", {"Year": 2019, "Week": 1}),
        ("2019-01-01T12:00:00Z", {"Year": 2019, "Week": 1}),
        ("2020-12-31T12:00:00Z", {"Year": 2020, "Week": 53}),
        ("2021-01-01T12:00:00Z", {"Year": 2020, "Week": 53}),
    ],
)
def test_iso_week_year_and_number_come_from_adjusted_thursday(
    date: str, expected: dict[str, int]
):
    result = run_ps(
        f"Import-Module '{ps_quote(COMMON)}' -Force; "
        f"Get-IsoWeekInfo -UtcDate ([DateTime]::Parse('{date}').ToUniversalTime()) "
        "| ConvertTo-Json -Compress"
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


def test_backup_and_restore_scripts_have_static_safety_contracts():
    common = COMMON.read_text(encoding="utf-8")
    backup = BACKUP.read_text(encoding="utf-8")
    restore = RESTORE.read_text(encoding="utf-8")

    assert "Assert-SafePath" in common
    assert "ReparsePoint" in common
    assert "Remove-Item -LiteralPath" in common
    assert "Remove-Item -Recurse" not in common

    assert "finally" in backup
    assert "Get-FileHash -LiteralPath $archive -Algorithm SHA256" in backup
    assert "Test-PathOverlap" in backup
    assert "Get-RetentionPlan" in backup

    assert "OrdinalIgnoreCase" in restore
    assert "'-tzf'" in restore
    assert "'-tvzf'" in restore
    assert "ReparsePoint" in restore
    assert ".." in restore
    assert ".rollback-" in restore
    assert ".failed-" in restore
    assert "Rename-Item" in restore
    assert "Remove-Item -Recurse" not in restore


def test_mutating_operation_entry_points_hold_the_shared_exclusive_lock():
    for script in (BACKUP, RESTORE, DRILL, UPDATE):
        source = script.read_text(encoding="utf-8")
        assert "Enter-OperationsLock -StateRoot $config.StateRoot" in source
        assert "Exit-OperationsLock -Lock $operationLock" in source

    for script in (BACKUP, RESTORE):
        source = script.read_text(encoding="utf-8")
        assert "InheritedOperationLock" in source
        assert "Assert-InheritedOperationsLock" in source

    drill = DRILL.read_text(encoding="utf-8")
    assert "function Get-Acl" not in drill


def test_backup_accepts_only_the_updates_live_exclusive_lock(tmp_path: Path):
    if shutil.which("tar.exe") is None:
        pytest.skip("tar.exe is unavailable")

    repository, data, env_file = make_repository(tmp_path)
    (data / "app" / "marker.txt").write_text("live", encoding="utf-8")
    state = tmp_path / "state"
    backups = tmp_path / "backups"
    calls = tmp_path / "calls.jsonl"
    harness = tmp_path / "inherited-lock.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"Import-Module '{ps_quote(OPERATIONS)}' -Force\n"
        "$runner = {\n"
        "  param([string]$FilePath, [string[]]$ArgumentList)\n"
        f"  [PSCustomObject]@{{FilePath=$FilePath;Args=@($ArgumentList)}} | ConvertTo-Json -Compress | Add-Content -LiteralPath '{ps_quote(calls)}'\n"
        "  if ($FilePath -eq 'docker') { if ($ArgumentList -contains 'ps') { 'app'; 'qdrant' }; return }\n"
        "  & $FilePath @ArgumentList\n"
        "  if ($LASTEXITCODE -ne 0) { throw \"$FilePath failed: $LASTEXITCODE\" }\n"
        "}\n"
        f"$lock = Enter-OperationsLock -StateRoot '{ps_quote(state)}'\n"
        "try {\n"
        f"  & '{ps_quote(BACKUP)}' -RepositoryRoot '{ps_quote(repository)}' -EnvFile '{ps_quote(env_file)}' -StateRoot '{ps_quote(state)}' -BackupRoot '{ps_quote(backups)}' -ExternalInvoker $runner -HealthProbe {{ $true }} -InheritedOperationLock $lock\n"
        "} finally { Exit-OperationsLock -Lock $lock }\n",
        encoding="utf-8",
    )

    result = run_ps(f"& '{ps_quote(harness)}'", timeout=90)

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(list((backups / "daily").glob("assistant-*.tar.gz"))) == 1


def test_backup_rejects_a_forged_inherited_lock(tmp_path: Path):
    repository, _, env_file = make_repository(tmp_path)
    state = tmp_path / "state"
    backups = tmp_path / "backups"
    foreign = tmp_path / "foreign.lock"
    calls = tmp_path / "external-called"

    result = run_ps(
        "$foreign = [IO.File]::Open("
        f"'{ps_quote(foreign)}', [IO.FileMode]::OpenOrCreate, "
        "[IO.FileAccess]::ReadWrite, [IO.FileShare]::None); "
        "try { "
        "$runner = { param($FilePath, $ArgumentList) "
        f"Set-Content -LiteralPath '{ps_quote(calls)}' -Value called }}; "
        "try { "
        f"& '{ps_quote(BACKUP)}' -RepositoryRoot '{ps_quote(repository)}' "
        f"-EnvFile '{ps_quote(env_file)}' -StateRoot '{ps_quote(state)}' "
        f"-BackupRoot '{ps_quote(backups)}' -ExternalInvoker $runner "
        "-HealthProbe { $true } -InheritedOperationLock $foreign; exit 92 "
        "} catch { if ($_.Exception.Message -notmatch 'live exclusive operations lock') "
        "{ throw } } "
        "} finally { $foreign.Dispose() }"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not calls.exists()


def test_backup_restore_round_trip_uses_fake_docker_and_retains_rollback(tmp_path: Path):
    if shutil.which("tar.exe") is None:
        pytest.skip("tar.exe is unavailable")

    repository, data, env_file = make_repository(tmp_path)
    (data / "app" / "marker.txt").write_text("original", encoding="utf-8")
    (data / "qdrant" / "collections" / "marker.txt").write_text(
        "original-qdrant", encoding="utf-8"
    )
    state = tmp_path / "state"
    backups = tmp_path / "backups"
    calls = tmp_path / "external-calls.jsonl"

    harness = tmp_path / "round-trip.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$runner = {\n"
        "  param([string]$FilePath, [string[]]$ArgumentList)\n"
        f"  [PSCustomObject]@{{ FilePath=$FilePath; Args=$ArgumentList }} | ConvertTo-Json -Compress | Add-Content -LiteralPath '{ps_quote(calls)}'\n"
        "  if ($FilePath -eq 'docker') {\n"
        "    if ($ArgumentList -contains 'ps') { 'app'; 'qdrant' }\n"
        "    return\n"
        "  }\n"
        "  & $FilePath @ArgumentList\n"
        "  if ($LASTEXITCODE -ne 0) { throw \"$FilePath failed: $LASTEXITCODE\" }\n"
        "}\n"
        "$healthy = { param($Config) $true }\n"
        f"& '{ps_quote(BACKUP)}' -RepositoryRoot '{ps_quote(repository)}' -EnvFile '{ps_quote(env_file)}' -StateRoot '{ps_quote(state)}' -BackupRoot '{ps_quote(backups)}' -ExternalInvoker $runner -HealthProbe $healthy\n"
        "$archive = Get-ChildItem -LiteralPath "
        f"'{ps_quote(backups / 'daily')}' -Filter '*.tar.gz' | Select-Object -First 1\n"
        "if ($null -eq $archive) { throw 'backup archive missing' }\n"
        f"Set-Content -LiteralPath '{ps_quote(data / 'app' / 'marker.txt')}' -Value 'mutated'\n"
        f"& '{ps_quote(RESTORE)}' -Archive $archive.FullName -RepositoryRoot '{ps_quote(repository)}' -EnvFile '{ps_quote(env_file)}' -StateRoot '{ps_quote(state)}' -BackupRoot '{ps_quote(backups)}' -ExternalInvoker $runner -HealthProbe $healthy\n",
        encoding="utf-8",
    )

    result = run_ps(f"& '{ps_quote(harness)}'", timeout=90)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (data / "app" / "marker.txt").read_text(encoding="utf-8-sig").strip() == "original"
    assert (data / "qdrant" / "collections" / "marker.txt").read_text(
        encoding="utf-8-sig"
    ).strip() == "original-qdrant"
    rollbacks = list(tmp_path.glob("deployment-data.rollback-*"))
    assert len(rollbacks) == 1
    assert (rollbacks[0] / "app" / "marker.txt").read_text(
        encoding="utf-8-sig"
    ).strip() == "mutated"
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    docker = [call["Args"] for call in recorded if call["FilePath"] == "docker"]
    assert any("stop" in call for call in docker)
    assert any("start" in call for call in docker)


def test_backup_archive_failure_restarts_prior_services_and_skips_retention(
    tmp_path: Path,
):
    repository, data, env_file = make_repository(tmp_path)
    (data / "app" / "marker.txt").write_text("live", encoding="utf-8")
    backups = tmp_path / "backups"
    daily = backups / "daily"
    for day in range(1, 9):
        make_set(daily, f"assistant-202606{day:02d}T030000Z.tar.gz")
    calls = tmp_path / "calls.jsonl"

    result = run_ps(
        "$runner = { param([string]$FilePath, [string[]]$ArgumentList) "
        f"[PSCustomObject]@{{FilePath=$FilePath;Args=$ArgumentList}} | ConvertTo-Json -Compress | Add-Content -LiteralPath '{ps_quote(calls)}'; "
        "if ($FilePath -eq 'docker') { if ($ArgumentList -contains 'ps') { 'app'; 'qdrant' }; return }; "
        "throw 'forced archive failure' }; "
        "try { "
        f"& '{ps_quote(BACKUP)}' -RepositoryRoot '{ps_quote(repository)}' "
        f"-EnvFile '{ps_quote(env_file)}' -StateRoot '{ps_quote(tmp_path / 'state')}' "
        f"-BackupRoot '{ps_quote(backups)}' -ExternalInvoker $runner -HealthProbe {{ $true }}; exit 92 "
        "} catch { exit 0 }"
    )

    assert result.returncode == 0, result.stderr
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    docker = [call["Args"] for call in recorded if call["FilePath"] == "docker"]
    stop = next(call for call in docker if "stop" in call)
    start = next(call for call in docker if "start" in call)
    assert stop[stop.index("stop") + 1 :] == ["app", "qdrant"]
    assert start[start.index("start") + 1 :] == ["app", "qdrant"]
    assert len(list(daily.glob("*.tar.gz"))) == 8


def test_backup_rejects_reparse_ancestor_before_creating_backup_root(tmp_path: Path):
    repository, data, env_file = make_repository(tmp_path)
    (data / "app" / "marker.txt").write_text("live", encoding="utf-8")
    target = tmp_path / "real-backup-parent"
    target.mkdir()
    junction = tmp_path / "backup-parent-link"
    created = run_ps(
        f"New-Item -ItemType Junction -Path '{ps_quote(junction)}' "
        f"-Target '{ps_quote(target)}' | Out-Null"
    )
    if created.returncode != 0:
        pytest.skip(f"junctions are unavailable: {created.stderr}")
    calls = tmp_path / "external-called"
    backup_root = junction / "must-not-exist"

    try:
        result = run_ps(
            "$runner = { param($FilePath, $ArgumentList) "
            f"Set-Content -LiteralPath '{ps_quote(calls)}' -Value called }}; "
            f"& '{ps_quote(BACKUP)}' -RepositoryRoot '{ps_quote(repository)}' "
            f"-EnvFile '{ps_quote(env_file)}' -StateRoot '{ps_quote(tmp_path / 'state')}' "
            f"-BackupRoot '{ps_quote(backup_root)}' -ExternalInvoker $runner "
            "-HealthProbe { $true }"
        )

        assert result.returncode != 0
        assert "reparse" in (result.stdout + result.stderr).lower()
        assert not (target / "must-not-exist").exists()
        assert not calls.exists()
    finally:
        run_ps(f"Remove-Item -LiteralPath '{ps_quote(junction)}' -Force")


@pytest.mark.parametrize(
    ("members", "verbose"),
    [
        (["../escape"], ["-rw-r--r-- user/group 1 2026-01-01 00:00 ../escape"]),
        (["/absolute"], ["-rw-r--r-- user/group 1 2026-01-01 00:00 /absolute"]),
        ([r"C:\escape"], [r"-rw-r--r-- user/group 1 2026-01-01 00:00 C:\escape"]),
        (["./app/link"], ["lrwxrwxrwx user/group 0 2026-01-01 00:00 ./app/link -> ../../escape"]),
        (["./app/link"], ["hrw-r--r-- user/group 0 2026-01-01 00:00 ./app/link link to ./other"]),
    ],
)
def test_restore_rejects_traversal_and_links_before_stopping_services(
    tmp_path: Path, members: list[str], verbose: list[str]
):
    repository, _, env_file = make_repository(tmp_path)
    backups = tmp_path / "backups"
    archive = backups / "daily" / "assistant-20260701T030000Z.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"synthetic archive")
    write_checksum(archive)
    calls = tmp_path / "calls.jsonl"
    members_json = json.dumps(members).replace("'", "''")
    verbose_json = json.dumps(verbose).replace("'", "''")

    result = run_ps(
        f"$members = ConvertFrom-Json '{members_json}'; $verbose = ConvertFrom-Json '{verbose_json}'; "
        "$runner = { param([string]$FilePath, [string[]]$ArgumentList) "
        f"[PSCustomObject]@{{FilePath=$FilePath;Args=$ArgumentList}} | ConvertTo-Json -Compress | Add-Content -LiteralPath '{ps_quote(calls)}'; "
        "if ($ArgumentList -contains '-tzf') { $members; return }; "
        "if ($ArgumentList -contains '-tvzf') { $verbose; return }; throw 'extraction must not run' }; "
        "try { "
        f"& '{ps_quote(RESTORE)}' -Archive '{ps_quote(archive)}' "
        f"-RepositoryRoot '{ps_quote(repository)}' -EnvFile '{ps_quote(env_file)}' "
        f"-StateRoot '{ps_quote(tmp_path / 'state')}' -BackupRoot '{ps_quote(backups)}' "
        "-ExternalInvoker $runner -HealthProbe { $true }; exit 93 } catch { exit 0 }"
    )

    assert result.returncode == 0, result.stderr
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert all(call["FilePath"] != "docker" for call in recorded)
    assert all("-xzf" not in call["Args"] for call in recorded)


def test_failed_post_swap_health_restores_old_data_and_retains_diagnostics(
    tmp_path: Path,
):
    if shutil.which("tar.exe") is None:
        pytest.skip("tar.exe is unavailable")
    repository, data, env_file = make_repository(tmp_path)
    (data / "app" / "marker.txt").write_text("current", encoding="utf-8")
    source = tmp_path / "source"
    (source / "app").mkdir(parents=True)
    (source / "qdrant").mkdir(parents=True)
    (source / "app" / "marker.txt").write_text("candidate", encoding="utf-8")
    backups = tmp_path / "backups"
    archive = backups / "daily" / "assistant-20260701T030000Z.tar.gz"
    archive.parent.mkdir(parents=True)
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source / "app", arcname="app")
        bundle.add(source / "qdrant", arcname="qdrant")
    write_checksum(archive)

    result = run_ps(
        "$script:healthCalls = 0; "
        "$runner = { param([string]$FilePath, [string[]]$ArgumentList) "
        "if ($FilePath -eq 'docker') { if ($ArgumentList -contains 'ps') { 'app'; 'qdrant' }; return }; "
        "& $FilePath @ArgumentList; if ($LASTEXITCODE -ne 0) { throw 'tar failed' } }; "
        "$health = { param($Config) $script:healthCalls++; return ($script:healthCalls -gt 1) }; "
        "try { "
        f"& '{ps_quote(RESTORE)}' -Archive '{ps_quote(archive)}' "
        f"-RepositoryRoot '{ps_quote(repository)}' -EnvFile '{ps_quote(env_file)}' "
        f"-StateRoot '{ps_quote(tmp_path / 'state')}' -BackupRoot '{ps_quote(backups)}' "
        "-ExternalInvoker $runner -HealthProbe $health; exit 94 } catch { exit 0 }"
    )

    assert result.returncode == 0, result.stderr
    assert (data / "app" / "marker.txt").read_text(encoding="utf-8-sig") == "current"
    failed = [path for path in tmp_path.glob("deployment-data.failed-*") if path.is_dir()]
    diagnostics = list(tmp_path.glob("deployment-data.failed-*.diagnostic"))
    assert len(failed) == 1, result.stdout + result.stderr
    assert len(diagnostics) == 1
    assert (failed[0] / "app" / "marker.txt").read_text(encoding="utf-8-sig") == "candidate"


def test_second_stop_failure_does_not_gate_post_swap_filesystem_rollback(
    tmp_path: Path,
):
    if shutil.which("tar.exe") is None:
        pytest.skip("tar.exe is unavailable")
    repository, data, env_file = make_repository(tmp_path)
    (data / "app" / "marker.txt").write_text("current", encoding="utf-8")
    source = tmp_path / "source"
    (source / "app").mkdir(parents=True)
    (source / "qdrant").mkdir(parents=True)
    (source / "app" / "marker.txt").write_text("candidate", encoding="utf-8")
    backups = tmp_path / "backups"
    archive = backups / "daily" / "assistant-20260701T030000Z.tar.gz"
    archive.parent.mkdir(parents=True)
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source / "app", arcname="app")
        bundle.add(source / "qdrant", arcname="qdrant")
    write_checksum(archive)

    result = run_ps(
        "$global:task4HealthCalls = 0; $global:task4StopCalls = 0; "
        "$runner = { param([string]$FilePath, [string[]]$ArgumentList) "
        "if ($FilePath -eq 'docker') { "
        "if ($ArgumentList -contains 'ps') { 'app'; 'qdrant'; return }; "
        "if ($ArgumentList -contains 'stop') { $global:task4StopCalls++; "
        "if ($global:task4StopCalls -eq 2) { throw 'forced second stop failure' } }; return }; "
        "& $FilePath @ArgumentList; if ($LASTEXITCODE -ne 0) { throw 'tar failed' } }; "
        "$health = { param($Config) $global:task4HealthCalls++; return ($global:task4HealthCalls -gt 1) }; "
        f"& '{ps_quote(RESTORE)}' -Archive '{ps_quote(archive)}' "
        f"-RepositoryRoot '{ps_quote(repository)}' -EnvFile '{ps_quote(env_file)}' "
        f"-StateRoot '{ps_quote(tmp_path / 'state')}' -BackupRoot '{ps_quote(backups)}' "
        "-ExternalInvoker $runner -HealthProbe $health"
    )

    assert result.returncode != 0
    assert (data / "app" / "marker.txt").read_text(encoding="utf-8-sig") == "current"
    failed = [path for path in tmp_path.glob("deployment-data.failed-*") if path.is_dir()]
    diagnostics = list(tmp_path.glob("deployment-data.failed-*.diagnostic"))
    assert len(failed) == 1, result.stdout + result.stderr
    assert len(diagnostics) == 1
    assert (failed[0] / "app" / "marker.txt").read_text(encoding="utf-8-sig") == "candidate"
    assert "forced second stop failure" in diagnostics[0].read_text(encoding="utf-8-sig")


def test_restore_rejects_checksum_mismatch_before_external_commands(tmp_path: Path):
    backups = tmp_path / "backups"
    archive = backups / "daily" / "assistant-20260701T030000Z.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"not an archive")
    (archive.parent / f"{archive.name}.sha256").write_text(
        f"{'0' * 64}  {archive.name}\n", encoding="ascii"
    )
    repository, _, env_file = make_repository(tmp_path)
    calls = tmp_path / "called"

    result = run_ps(
        "$runner = { param($FilePath, $ArgumentList) "
        f"Set-Content -LiteralPath '{ps_quote(calls)}' -Value called }}; "
        f"& '{ps_quote(RESTORE)}' -Archive '{ps_quote(archive)}' "
        f"-RepositoryRoot '{ps_quote(repository)}' -EnvFile '{ps_quote(env_file)}' "
        f"-StateRoot '{ps_quote(tmp_path / 'state')}' -BackupRoot '{ps_quote(backups)}' "
        "-ExternalInvoker $runner -HealthProbe { $true }"
    )

    assert result.returncode != 0
    assert "checksum mismatch" in (result.stdout + result.stderr).lower()
    assert not calls.exists()


def test_restore_drill_isolates_compose_data_and_port_then_cleans_success(
    tmp_path: Path,
):
    repository, production_data, env_file = make_repository(tmp_path)
    secret = "configured-secret-value"
    env_file.write_text(
        f"DEPLOY_DATA_ROOT={production_data}\n"
        "APP_BIND_ADDRESS=0.0.0.0\n"
        "APP_PORT=7860\n"
        f"LLM_API_KEY={secret}\n",
        encoding="utf-8",
    )
    backups = tmp_path / "backups"
    selected_archive = make_drill_backup(backups)
    (backups / "daily" / "assistant-20260812T030000Z.tar.gz").write_bytes(
        b"newer but incomplete"
    )
    state = tmp_path / "state"
    calls = tmp_path / "calls.jsonl"
    captured_env = tmp_path / "captured.env"
    harness = tmp_path / "drill-success.ps1"
    write_drill_harness(
        harness,
        repository=repository,
        env_file=env_file,
        state=state,
        backups=backups,
        calls=calls,
        captured_env=captured_env,
        fail_smoke=False,
        secret=secret,
    )

    result = run_ps(f"& '{ps_quote(harness)}'", timeout=90)

    assert result.returncode == 0, result.stdout + result.stderr
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    docker = [call["Args"] for call in recorded if call["FilePath"] == "docker"]
    up = next(args for args in docker if "up" in args)
    down = next(args for args in docker if "down" in args)
    project = up[up.index("--project-name") + 1]
    temp_env = Path(up[up.index("--env-file") + 1])
    assert project.startswith("assistant-drill-")
    assert project != repository.name.lower()
    assert down[down.index("--project-name") + 1] == project
    assert "--volumes" not in down
    assert not temp_env.exists()
    tar_lists = [
        call["Args"]
        for call in recorded
        if call["FilePath"] == "tar.exe" and "-tzf" in call["Args"]
    ]
    assert len(tar_lists) == 1
    assert tar_lists[0][-1] == str(selected_archive)

    smoke = next(call for call in recorded if call["FilePath"].endswith("python.exe"))
    assert smoke["Args"][smoke["Args"].index("--project-name") + 1] == project

    drill_env = captured_env.read_text(encoding="utf-8-sig")
    assert "APP_BIND_ADDRESS=127.0.0.1" in drill_env
    port_line = next(line for line in drill_env.splitlines() if line.startswith("APP_PORT="))
    assert int(port_line.split("=", 1)[1]) != 7860
    assert (
        "DEPLOY_DATA_ROOT=" + str(backups).replace("\\", "/") + "/.drills/"
        in drill_env
    )
    assert "DEPLOY_ENV_FILE=" + str(temp_env).replace("\\", "/") in drill_env
    assert str(production_data).replace("\\", "/") not in drill_env
    assert str(production_data) not in json.dumps(recorded)

    drills_root = backups / ".drills"
    assert not list(drills_root.iterdir())
    reports = list((state / "reports").glob("restore-drill-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8-sig"))
    assert report["status"] == "succeeded"


def test_restore_drill_failure_retains_data_but_removes_env_and_redacts_report(
    tmp_path: Path,
):
    repository, production_data, env_file = make_repository(tmp_path)
    secret = "configured-secret-value"
    env_file.write_text(
        f"DEPLOY_DATA_ROOT={production_data}\nLLM_API_KEY={secret}\n",
        encoding="utf-8",
    )
    backups = tmp_path / "backups"
    make_drill_backup(backups)
    state = tmp_path / "state"
    calls = tmp_path / "calls.jsonl"
    captured_env = tmp_path / "captured.env"
    harness = tmp_path / "drill-failure.ps1"
    write_drill_harness(
        harness,
        repository=repository,
        env_file=env_file,
        state=state,
        backups=backups,
        calls=calls,
        captured_env=captured_env,
        fail_smoke=True,
        secret=secret,
    )

    result = run_ps(f"& '{ps_quote(harness)}'", timeout=90)

    assert result.returncode != 0
    assert secret not in result.stdout + result.stderr
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    docker = [call["Args"] for call in recorded if call["FilePath"] == "docker"]
    up = next(args for args in docker if "up" in args)
    down = next(args for args in docker if "down" in args)
    temp_env = Path(up[up.index("--env-file") + 1])
    assert "--volumes" not in down
    assert not temp_env.exists()

    retained = list((backups / ".drills").iterdir())
    assert len(retained) == 1
    assert (retained[0] / "data" / "app" / "restored.txt").is_file()
    assert not list(retained[0].glob("*.env"))
    reports = list((state / "reports").glob("restore-drill-*.json"))
    assert len(reports) == 1
    report_text = reports[0].read_text(encoding="utf-8-sig")
    assert secret not in report_text
    report = json.loads(report_text)
    assert report["status"] == "failed"
    assert report["retained_data"] == str(retained[0] / "data")


def test_restore_drill_has_powershell_51_parser_contract():
    result = run_ps(
        f"$errors = $null; [void][Management.Automation.Language.Parser]::ParseFile('{ps_quote(DRILL)}', [ref]$null, [ref]$errors); "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { $_.Message }; exit 1 }"
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("scenario", "expected_stage"),
    [
        ("no-complete-backup", "backup-discovery"),
        ("checksum-mismatch", "checksum-validation"),
        ("unsafe-member", "member-validation"),
    ],
)
def test_restore_drill_preflight_failures_are_reported_without_mutation(
    tmp_path: Path, scenario: str, expected_stage: str
):
    repository, production_data, env_file = make_repository(tmp_path)
    marker = production_data / "app" / "production-marker.txt"
    marker.write_text("unchanged", encoding="utf-8")
    secret = "preflight-configured-secret"
    env_file.write_text(
        f"DEPLOY_DATA_ROOT={production_data}\nLLM_API_KEY={secret}\n",
        encoding="utf-8",
    )
    backups = tmp_path / "backups"
    if scenario != "no-complete-backup":
        archive = make_drill_backup(backups)
        if scenario == "checksum-mismatch":
            (archive.parent / f"{archive.name}.sha256").write_text(
                f"{'0' * 64}  {archive.name}\n", encoding="ascii"
            )
    else:
        (backups / "daily").mkdir(parents=True)
        (backups / "daily" / "assistant-20260812T030000Z.tar.gz").write_bytes(
            b"incomplete"
        )

    state = tmp_path / "state"
    calls = tmp_path / "preflight-calls.jsonl"
    unsafe_output = "'../escape'" if scenario == "unsafe-member" else "@()"
    harness = tmp_path / f"preflight-{scenario}.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$runner = {\n"
        "  param([string]$FilePath, [string[]]$ArgumentList)\n"
        f"  [PSCustomObject]@{{ FilePath=$FilePath; Args=@($ArgumentList) }} | ConvertTo-Json -Compress | Add-Content -LiteralPath '{ps_quote(calls)}'\n"
        f"  if ($FilePath -eq 'tar.exe' -and $ArgumentList -contains '-tzf') {{ {unsafe_output}; return }}\n"
        "  throw \"unexpected external command: $FilePath\"\n"
        "}\n"
        f"& '{ps_quote(DRILL)}' -RepositoryRoot '{ps_quote(repository)}' -EnvFile '{ps_quote(env_file)}' -StateRoot '{ps_quote(state)}' -BackupRoot '{ps_quote(backups)}' -ExternalInvoker $runner -HealthTimeoutSeconds 5\n",
        encoding="utf-8",
    )

    result = run_ps(f"& '{ps_quote(harness)}'", timeout=90)

    assert result.returncode != 0
    assert secret not in result.stdout + result.stderr
    reports = list((state / "reports").glob("restore-drill-*.json"))
    assert len(reports) == 1
    report_text = reports[0].read_text(encoding="utf-8-sig")
    assert secret not in report_text
    report = json.loads(report_text)
    assert report["status"] == "failed"
    assert report["failure_stage"] == expected_stage
    assert report["failure_category"] == "preflight"
    assert report["retained_data"] is None

    recorded = (
        [json.loads(line) for line in calls.read_text().splitlines()]
        if calls.exists()
        else []
    )
    assert all(call["FilePath"] != "docker" for call in recorded)
    assert all("-xzf" not in call["Args"] for call in recorded)
    assert str(production_data) not in json.dumps(recorded)
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert not (backups / ".drills").exists()


@pytest.mark.parametrize("explicit_fallback", [False, True])
def test_restore_drill_report_uses_omitted_env_or_explicit_state_root(
    tmp_path: Path, explicit_fallback: bool
):
    repository = tmp_path / "repository"
    repository.mkdir()
    shutil.copyfile(ROOT / "compose.yaml", repository / "compose.yaml")
    data = tmp_path / "deployment-data"
    data.mkdir()
    environment_state = tmp_path / "environment-state"
    backups = tmp_path / "backups"
    (backups / "daily").mkdir(parents=True)
    env_file = repository / "deploy.env"
    env_file.write_text(
        f"DEPLOY_DATA_ROOT={data}\n"
        f"DEPLOY_STATE_ROOT={environment_state}\n"
        f"DEPLOY_BACKUP_ROOT={backups}\n",
        encoding="utf-8",
    )
    state_argument = " -StateRoot 'deploy-state'" if explicit_fallback else ""

    result = run_ps(
        f"& '{ps_quote(DRILL)}' -RepositoryRoot '{ps_quote(repository)}' "
        f"-EnvFile '{ps_quote(env_file)}'{state_argument} -HealthTimeoutSeconds 5"
    )

    assert result.returncode != 0
    expected_state = repository / "deploy-state" if explicit_fallback else environment_state
    reports = list((expected_state / "reports").glob("restore-drill-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8-sig"))
    assert report["failure_stage"] == "backup-discovery"


def test_restore_drill_configuration_failure_uses_safe_report_fallback(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    shutil.copyfile(ROOT / "compose.yaml", repository / "compose.yaml")
    env_file = repository / "deploy.env"
    env_file.write_text(
        "DEPLOY_DATA_ROOT=data\nOPERATIONS_NOTIFY_COOLDOWN_MINUTES=invalid\n",
        encoding="utf-8",
    )

    result = run_ps(
        f"& '{ps_quote(DRILL)}' -RepositoryRoot '{ps_quote(repository)}' "
        f"-EnvFile '{ps_quote(env_file)}' -HealthTimeoutSeconds 5"
    )

    assert result.returncode != 0
    reports = list((repository / "deploy-state" / "reports").glob("restore-drill-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8-sig"))
    assert report["failure_stage"] == "configuration"


@pytest.mark.parametrize("scenario", ["success", "neo4j_stays_running", "qdrant_rollback_fails"])
def test_graph_backup_is_cold_and_paired_with_qdrant(tmp_path, scenario):
    if shutil.which("tar.exe") is None:
        pytest.skip("tar.exe is unavailable")

    repository, data, env_file = make_repository(tmp_path)
    (data / "app" / "app.db").write_bytes(b"sqlite fixture")
    graph_data = data / "neo4j" / "data"
    graph_data.mkdir(parents=True)
    (graph_data / "store.bin").write_bytes(b"neo4j fixture")
    backups = tmp_path / "backups"
    state = tmp_path / "state"
    calls = tmp_path / "external-graph-calls.jsonl"
    harness = tmp_path / "graph-round-trip.ps1"
    stay_running = "$true" if scenario == "neo4j_stays_running" else "$false"
    fail_rollback = "$true" if scenario == "qdrant_rollback_fails" else "$false"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$global:runningForGraph = @('app', 'qdrant', 'neo4j')\n"
        f"$global:neo4jRemainsRunning = {stay_running}\n"
        f"$global:failGraphRollback = {fail_rollback}\n"
        "$runner = {\n"
        "  param([string]$FilePath, [string[]]$ArgumentList)\n"
        f"  [PSCustomObject]@{{FilePath=$FilePath; Args=@($ArgumentList); Running=@($global:runningForGraph)}} | ConvertTo-Json -Compress | Add-Content -LiteralPath '{ps_quote(calls)}'\n"
        "  if ($FilePath -eq 'docker') {\n"
        "    if ($ArgumentList -contains 'ps') {\n"
        "      if ($ArgumentList -notcontains '--profile' -or $ArgumentList -notcontains 'graph') { throw 'Neo4j profile not examined' }; $global:runningForGraph; return\n"
        "    }\n"
        "    if ($ArgumentList -contains 'stop') { $global:runningForGraph = @(); if ($global:neo4jRemainsRunning) { $global:runningForGraph = @('neo4j') }; return }\n"
        "    if ($ArgumentList -contains 'start') { $global:runningForGraph = @('app', 'qdrant', 'neo4j'); return }\n"
        "    if ($ArgumentList -contains 'inspect') { 'zhiyan_qdrant_data'; return }\n"
        "    if ($ArgumentList -contains 'images') { 'abcd1234abcd1234'; return }\n"
        "    if ($global:failGraphRollback -and $ArgumentList -contains '-xzf' -and $ArgumentList[-1] -like '/backup/qdrant-rollback-*') { throw 'forced Qdrant rollback import failure' }\n"
        "    if ($ArgumentList -contains '-czf') {\n"
        "      if ($global:runningForGraph.Count -ne 0) { throw 'Qdrant was exported while services were running' }\n"
        "      $mount = $ArgumentList | Where-Object { $_ -match '^type=bind,source=.+,target=/backup$' }\n"
        "      $null = $mount -match '^type=bind,source=(.+),target=/backup$'\n"
        "      [IO.File]::WriteAllBytes((Join-Path $Matches[1] ($ArgumentList[-2].Substring(8))), [byte[]]@(1, 2, 3))\n"
        "    }\n"
        "    return\n"
        "  }\n"
        "  if ($ArgumentList -contains '-czf' -and $global:runningForGraph.Count -ne 0) { throw 'graph archive was created while services were running' }\n"
        "  & $FilePath @ArgumentList\n"
        "  if ($LASTEXITCODE -ne 0) { throw 'tar failed' }\n"
        "}\n"
        "$healthy = { param($Config) $true }\n"
        f"$archive = (& '{ps_quote(BACKUP)}' -RepositoryRoot '{ps_quote(repository)}' -EnvFile '{ps_quote(env_file)}' -StateRoot '{ps_quote(state)}' -BackupRoot '{ps_quote(backups)}' -ExternalInvoker $runner -HealthProbe $healthy).Archive\n"
        f"Set-Content -LiteralPath '{ps_quote(graph_data / 'store.bin')}' -Value 'mutated'\n"
        "if ($global:failGraphRollback) { $healthy = { param($Config) $false } }\n"
        f"& '{ps_quote(RESTORE)}' -Archive $archive -RepositoryRoot '{ps_quote(repository)}' -EnvFile '{ps_quote(env_file)}' -StateRoot '{ps_quote(state)}' -BackupRoot '{ps_quote(backups)}' -ExternalInvoker $runner -HealthProbe $healthy\n",
        encoding="ascii",
    )

    result = run_ps(f"& '{ps_quote(harness)}'")
    archives = list((backups / "daily").glob("*.tar.gz.meta"))
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    if scenario == "neo4j_stays_running":
        assert result.returncode != 0
        assert "Deployment services remain running after stop" in result.stdout + result.stderr
        assert archives == []
        assert all("-czf" not in record["Args"] for record in records)
        return
    if scenario == "qdrant_rollback_fails":
        assert result.returncode != 0
        assert "forced Qdrant rollback import failure" in result.stdout + result.stderr
        assert (graph_data / "store.bin").read_text(encoding="utf-8-sig").strip() == "mutated"
        assert sum("start" in record["Args"] for record in records) == 2
        assert records[-1]["Running"] == []
        return

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(archives) == 1
    metadata = json.loads(archives[0].read_text())
    assert metadata["neo4j_data"] == "neo4j/data"
    assert metadata["qdrant_sha256"] == hashlib.sha256(b"\x01\x02\x03").hexdigest()
    with tarfile.open(archives[0].parent / metadata["archive"]) as archive:
        names = set(archive.getnames())
        assert "./app/app.db" in names
        assert "./neo4j/data/store.bin" in names
    assert (graph_data / "store.bin").read_bytes() == b"neo4j fixture"
    assert all("neo4j" in record["Args"] for record in records if "stop" in record["Args"])


def test_graph_restore_rejects_qdrant_archive_from_a_different_set(tmp_path):
    if shutil.which("tar.exe") is None:
        pytest.skip("tar.exe is unavailable")

    repository, data, env_file = make_repository(tmp_path)
    backups = tmp_path / "backups"
    archive = backups / "daily" / "assistant-20261126T030000Z.tar.gz"
    archive.parent.mkdir(parents=True)
    graph_data = data / "neo4j" / "data"
    graph_data.mkdir(parents=True)
    (graph_data / "store.bin").write_bytes(b"current graph")
    with tarfile.open(archive, "w:gz") as store:
        store.add(data, arcname=".")
    write_checksum(archive)
    qdrant = archive.with_name(f"{archive.name}.qdrant-volume.tar.gz")
    qdrant.write_bytes(b"qdrant from a different snapshot")
    write_checksum(qdrant)
    archive.with_name(f"{archive.name}.meta").write_text(
        json.dumps({
            "neo4j_data": "neo4j/data",
            "archive": archive.name,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "qdrant_archive": qdrant.name,
            "qdrant_sha256": hashlib.sha256(b"original snapshot qdrant").hexdigest(),
        }),
        encoding="utf-8",
    )

    result = run_ps(
        "$runner = { param([string]$FilePath, [string[]]$ArgumentList) "
        "if ($FilePath -eq 'docker') { throw 'deployment should not be touched' }; "
        "& $FilePath @ArgumentList }; "
        f"& '{ps_quote(RESTORE)}' -Archive '{ps_quote(archive)}' -RepositoryRoot '{ps_quote(repository)}' "
        f"-EnvFile '{ps_quote(env_file)}' -StateRoot '{ps_quote(tmp_path / 'state')}' "
        f"-BackupRoot '{ps_quote(backups)}' -ExternalInvoker $runner"
    )

    assert result.returncode != 0
    assert "The Neo4j backup metadata does not match the selected archives" in result.stdout + result.stderr
    assert "deployment should not be touched" not in result.stdout + result.stderr
    assert (graph_data / "store.bin").read_bytes() == b"current graph"
