#!/bin/sh
set -eu

if [ "$#" -lt 1 ]; then
    echo "Usage: deploy/restore.sh <archive> [--env-file path]" >&2
    exit 2
fi

archive="$1"
shift
env_file="${DEPLOY_ENV_FILE:-deploy/.env}"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --env-file)
            [ "$#" -ge 2 ] || {
                echo "--env-file requires a path" >&2
                exit 2
            }
            env_file="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

[ -f "$env_file" ] || {
    echo "Environment file not found: $env_file" >&2
    exit 1
}
[ -f "$archive" ] || {
    echo "Archive not found: $archive" >&2
    exit 1
}

read_env_value() {
    key="$1"
    carriage_return="$(printf '\r')"
    while IFS= read -r line; do
        line="${line%"$carriage_return"}"
        case "$line" in
            "$key="*)
                value="${line#*=}"
                case "$value" in
                    \"*\") value="${value#\"}"; value="${value%\"}" ;;
                    \'*\') value="${value#\'}"; value="${value%\'}" ;;
                esac
                printf '%s\n' "$value"
                return
                ;;
        esac
    done < "$env_file"
}

read_meta_value() {
    meta_file="$1"
    key="$2"
    carriage_return="$(printf '\r')"
    while IFS= read -r line; do
        line="${line%"$carriage_return"}"
        case "$line" in
            "$key="*)
                printf '%s\n' "${line#*=}"
                return
                ;;
        esac
    done < "$meta_file"
}

compose() {
    docker compose --env-file "$env_file" --profile graph "$@"
}

list_active_services() {
    compose ps --all --services \
        --status running --status paused --status restarting --status removing
}

data_root="${DEPLOY_DATA_ROOT:-$(read_env_value DEPLOY_DATA_ROOT)}"
data_root="${data_root:-./deploy-data}"
case "$data_root" in
    ""|/|.)
        echo "Refusing to restore into an unsafe data root: $data_root" >&2
        exit 1
        ;;
esac
[ -d "$data_root" ] || {
    echo "Current data root not found: $data_root" >&2
    exit 1
}

archive_dir="$(cd "$(dirname "$archive")" && pwd -P)"
archive_name="$(basename "$archive")"
archive="$archive_dir/$archive_name"
checksum="$archive.sha256"
[ -f "$checksum" ] || {
    echo "Checksum not found: $checksum" >&2
    exit 1
}
(
    cd "$archive_dir"
    sha256sum -c "$archive_name.sha256"
)
archive_sha256="$(awk '{print $1}' "$checksum")"

qdrant_archive="$archive.qdrant-volume.tar.gz"
qdrant_archive_name="$(basename "$qdrant_archive")"
qdrant_sha256=""
if [ -f "$qdrant_archive" ]; then
    [ -f "$qdrant_archive.sha256" ] || {
        echo "Qdrant checksum not found: $qdrant_archive.sha256" >&2
        exit 1
    }
    (
        cd "$archive_dir"
        sha256sum -c "$qdrant_archive_name.sha256"
    )
    qdrant_sha256="$(awk '{print $1}' "$qdrant_archive.sha256")"
    if ! tar -tzf "$qdrant_archive" | awk '
        $0 ~ /^\// || $0 ~ /(^|\/)\.\.(\/|$)/ { bad=1 }
        $0 != "." && $0 != "./" && $0 !~ /^\.\// { bad=1 }
        END { exit bad }
    '; then
        echo "Qdrant archive contains an unsafe path" >&2
        exit 1
    fi
    if tar -tvzf "$qdrant_archive" | awk '$1 ~ /^[lh]/ { found=1 } END { exit !found }'; then
        echo "Qdrant archive contains a symbolic or hard link" >&2
        exit 1
    fi
fi

if ! tar -tzf "$archive" | awk '
    $0 ~ /^\// || $0 ~ /(^|\/)\.\.(\/|$)/ { bad=1 }
    $0 != "." && $0 != "./" && $0 !~ /^\.\// { bad=1 }
    END { exit bad }
'; then
    echo "Archive contains an unsafe path" >&2
    exit 1
fi
if tar -tvzf "$archive" | awk '$1 ~ /^[lh]/ { found=1 } END { exit !found }'; then
    echo "Archive contains a symbolic or hard link" >&2
    exit 1
fi

archive_has_graph=0
if tar -tzf "$archive" | awk '$0 ~ /^(\.\/)?neo4j\/data(\/|$)/ { found=1 } END { exit !found }'; then
    archive_has_graph=1
fi

if [ "$archive_has_graph" -eq 1 ]; then
    meta_file="$archive.meta"
    [ -f "$qdrant_archive" ] && [ -n "$qdrant_sha256" ] && [ -f "$meta_file" ] || {
        echo "A graph restore requires a verified SQLite, Neo4j and Qdrant backup set" >&2
        exit 1
    }
    meta_neo4j="$(read_meta_value "$meta_file" neo4j_data)"
    meta_archive="$(read_meta_value "$meta_file" archive)"
    meta_sha256="$(read_meta_value "$meta_file" sha256)"
    meta_qdrant_archive="$(read_meta_value "$meta_file" qdrant_archive)"
    meta_qdrant_sha256="$(read_meta_value "$meta_file" qdrant_sha256)"
    if [ "$meta_neo4j" != "neo4j/data" ] ||
        [ "$meta_archive" != "$archive_name" ] ||
        [ "$meta_sha256" != "$archive_sha256" ] ||
        [ "$meta_qdrant_archive" != "$qdrant_archive_name" ] ||
        [ "$meta_qdrant_sha256" != "$qdrant_sha256" ]; then
        echo "The Neo4j backup metadata does not match the selected archives" >&2
        exit 1
    fi
fi

data_abs="$(cd "$data_root" && pwd -P)"
parent="$(dirname "$data_abs")"
staging="$(mktemp -d "$parent/.assistant-restore.XXXXXX")"
rollback="${data_abs}.rollback-$(date -u +%Y%m%dT%H%M%SZ)"
failed_restore="${data_abs}.failed-$(date -u +%Y%m%dT%H%M%SZ)"
qdrant_volume="${QDRANT_VOLUME_NAME:-$(read_env_value QDRANT_VOLUME_NAME)}"
qdrant_volume="${qdrant_volume:-zhiyan_qdrant_data}"
case "$qdrant_volume" in
    *[!A-Za-z0-9_.-]*|[-.]*|"")
        echo "Unsafe QDRANT_VOLUME_NAME: $qdrant_volume" >&2
        exit 1
        ;;
esac
qdrant_rollback="$archive_dir/.qdrant-rollback-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
running_services="$(list_active_services)"

graph_enabled_raw="$(printf '%s' "${ZHIYAN_GRAPH_ENABLED:-$(read_env_value ZHIYAN_GRAPH_ENABLED)}" | tr '[:upper:]' '[:lower:]')"
graph_enabled=0
case "$graph_enabled_raw" in
    1|true|yes|on) graph_enabled=1 ;;
esac
local_has_graph=0
if [ "$graph_enabled" -eq 1 ] || [ -d "$data_abs/neo4j/data" ]; then
    local_has_graph=1
fi
for svc in $running_services; do
    if [ "$svc" = "neo4j" ]; then
        local_has_graph=1
    fi
done
if [ "$local_has_graph" -eq 1 ] && [ "$archive_has_graph" -eq 0 ]; then
    echo "The selected archive has no Neo4j data for this graph deployment" >&2
    exit 1
fi
graph_restore=0
if [ "$local_has_graph" -eq 1 ] || [ "$archive_has_graph" -eq 1 ]; then
    graph_restore=1
fi

services_stopped=0
swap_done=0
qdrant_mutated=0

start_services() {
    if [ -n "$running_services" ]; then
        # shellcheck disable=SC2086
        compose start $running_services >/dev/null
    fi
}

wait_for_health() {
    deadline="$(( $(date +%s) + 180 ))"
    while [ "$(date +%s)" -lt "$deadline" ]; do
        all_healthy=1
        for service in $running_services; do
            container_id="$(
                compose ps -q "$service"
            )"
            state="$(
                docker inspect \
                    --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' \
                    "$container_id"
            )"
            [ "$state" = "running healthy" ] || all_healthy=0
        done
        [ "$all_healthy" -eq 1 ] && return 0
        sleep 2
    done
    return 1
}

restore_on_failure() {
    status="$?"
    trap - EXIT HUP INT TERM
    if [ "$status" -ne 0 ]; then
        rollback_failed=0
        if [ "$swap_done" -eq 1 ]; then
            compose stop >/dev/null 2>&1 || rollback_failed=1
            if [ "$graph_restore" -eq 1 ] && [ -n "$(list_active_services 2>/dev/null || true)" ]; then
                rollback_failed=1
            fi
            if [ "$rollback_failed" -eq 0 ]; then
                if [ -d "$data_abs" ]; then
                    mv "$data_abs" "$failed_restore" || rollback_failed=1
                fi
                if [ -d "$rollback" ]; then
                    mv "$rollback" "$data_abs" || rollback_failed=1
                fi
            fi
        elif [ "$services_stopped" -eq 1 ]; then
            compose stop >/dev/null 2>&1 || rollback_failed=1
        fi
        if [ "$qdrant_mutated" -eq 1 ] && [ "$rollback_failed" -eq 0 ] && [ -n "${helper_image:-}" ] && [ -f "$qdrant_rollback" ]; then
            docker run --rm --user 0:0 \
                --mount "type=volume,source=$qdrant_volume,target=/target" \
                --entrypoint sh "$helper_image" -ec \
                'find /target -mindepth 1 -depth -delete' >/dev/null 2>&1 || rollback_failed=1
            docker run --rm --user 0:0 \
                --mount "type=volume,source=$qdrant_volume,target=/target" \
                --mount "type=bind,source=$archive_dir,target=/backup,readonly" \
                --entrypoint tar "$helper_image" \
                -C /target -xzf "/backup/$(basename "$qdrant_rollback")" >/dev/null 2>&1 || rollback_failed=1
        fi
        if [ "$graph_restore" -eq 0 ] || [ "$rollback_failed" -eq 0 ]; then
            start_services || true
        else
            echo "Graph restore rollback failed; keeping deployment services stopped to prevent split state" >&2
        fi
        if [ -n "${staging:-}" ] && [ -d "$staging" ]; then
            echo "Restore staging retained for inspection: $staging" >&2
        fi
    fi
    exit "$status"
}
trap restore_on_failure EXIT HUP INT TERM

tar --no-same-owner -xzf "$archive" -C "$staging"
[ -d "$staging/app" ] || {
    echo "Archive is missing the app data directory" >&2
    exit 1
}
if [ ! -f "$qdrant_archive" ]; then
    [ -d "$staging/qdrant" ] || {
        echo "Archive is missing the qdrant data directory" >&2
        exit 1
    }
fi
if [ "$archive_has_graph" -eq 1 ]; then
    [ -d "$staging/neo4j/data" ] || {
        echo "Archive is missing the neo4j/data directory" >&2
        exit 1
    }
fi
[ ! -e "$rollback" ] || {
    echo "Rollback path already exists: $rollback" >&2
    exit 1
}

compose stop >/dev/null
services_stopped=1
if [ "$graph_restore" -eq 1 ]; then
    still_active="$(list_active_services)"
    [ -z "$still_active" ] || {
        echo "Deployment services remain running after stop: $still_active" >&2
        exit 1
    }
fi

if [ -f "$qdrant_archive" ]; then
    docker volume inspect "$qdrant_volume" >/dev/null
    helper_image="$(compose images -q qdrant)"
    [ -n "$helper_image" ] || {
        echo "Unable to resolve the Qdrant helper image" >&2
        exit 1
    }
    docker run --rm --user 0:0 \
        --mount "type=volume,source=$qdrant_volume,target=/source,readonly" \
        --mount "type=bind,source=$archive_dir,target=/backup" \
        --entrypoint tar "$helper_image" \
        -C /source -czf "/backup/$(basename "$qdrant_rollback")" .
fi

mv "$data_abs" "$rollback"
swap_done=1
mv "$staging" "$data_abs"
staging=""

if [ -f "$qdrant_archive" ]; then
    docker run --rm --user 0:0 \
        --mount "type=volume,source=$qdrant_volume,target=/target" \
        --entrypoint sh "$helper_image" -ec \
        'find /target -mindepth 1 -depth -delete'
    qdrant_mutated=1
    docker run --rm --user 0:0 \
        --mount "type=volume,source=$qdrant_volume,target=/target" \
        --mount "type=bind,source=$archive_dir,target=/backup,readonly" \
        --entrypoint tar "$helper_image" \
        -C /target -xzf "/backup/$qdrant_archive_name"
fi

start_services
wait_for_health
swap_done=0
qdrant_mutated=0
services_stopped=0
printf 'Restore completed. Rollback kept at: %s\n' "$rollback"
