#!/bin/sh
set -eu

env_file="${DEPLOY_ENV_FILE:-deploy/.env}"
backup_root="${DEPLOY_BACKUP_ROOT:-./backups}"

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
        --backup-root)
            [ "$#" -ge 2 ] || {
                echo "--backup-root requires a path" >&2
                exit 2
            }
            backup_root="$2"
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

compose() {
    docker compose --env-file "$env_file" --profile graph "$@"
}

list_active_services() {
    compose ps --all --services \
        --status running --status paused --status restarting --status removing
}

data_root="${DEPLOY_DATA_ROOT:-$(read_env_value DEPLOY_DATA_ROOT)}"
data_root="${data_root:-./deploy-data}"
qdrant_volume="${QDRANT_VOLUME_NAME:-$(read_env_value QDRANT_VOLUME_NAME)}"
qdrant_volume="${qdrant_volume:-zhiyan_qdrant_data}"
case "$qdrant_volume" in
    *[!A-Za-z0-9_.-]*|[-.]*|"")
        echo "Unsafe QDRANT_VOLUME_NAME: $qdrant_volume" >&2
        exit 1
        ;;
esac

[ -d "$data_root" ] || {
    echo "DEPLOY_DATA_ROOT must exist before backup: $data_root" >&2
    exit 1
}

mkdir -p "$backup_root"
data_abs="$(cd "$data_root" && pwd -P)"
backup_abs="$(cd "$backup_root" && pwd -P)"
case "$backup_abs/" in
    "$data_abs/"*)
        echo "Backup root must be outside DEPLOY_DATA_ROOT" >&2
        exit 1
        ;;
esac

running_services="$(list_active_services)"
graph_enabled_raw="$(printf '%s' "${ZHIYAN_GRAPH_ENABLED:-$(read_env_value ZHIYAN_GRAPH_ENABLED)}" | tr '[:upper:]' '[:lower:]')"
graph_enabled=0
case "$graph_enabled_raw" in
    1|true|yes|on) graph_enabled=1 ;;
esac
graph_running=0
for svc in $running_services; do
    if [ "$svc" = "neo4j" ]; then
        graph_running=1
    fi
done
neo4j_dir="$data_abs/neo4j/data"
graph_present=0
[ -d "$neo4j_dir" ] && graph_present=1

if [ "$graph_enabled" -eq 1 ]; then
    neo4j_uri="${NEO4J_URI:-$(read_env_value NEO4J_URI)}"
    case "$neo4j_uri" in
        neo4j://neo4j|neo4j://neo4j/|neo4j://neo4j:7687|neo4j://neo4j:7687/|bolt://neo4j|bolt://neo4j/|bolt://neo4j:7687|bolt://neo4j:7687/)
            ;;
        *)
            echo "The deployment graph must use the locally backed-up Neo4j service" >&2
            exit 1
            ;;
    esac
fi
if [ "$graph_enabled" -eq 1 ] || [ "$graph_running" -eq 1 ]; then
    [ "$graph_present" -eq 1 ] || {
        echo "The Neo4j data directory is missing from the deployment data root" >&2
        exit 1
    }
fi
neo4j_data_included=0
if [ "$graph_present" -eq 1 ] || [ "$graph_enabled" -eq 1 ] || [ "$graph_running" -eq 1 ]; then
    neo4j_data_included=1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive_name="assistant-$timestamp.tar.gz"
archive="$backup_abs/$archive_name"
metadata="$archive.meta"
qdrant_archive="$archive.qdrant-volume.tar.gz"

restart_services() {
    status="$?"
    trap - EXIT HUP INT TERM
    if [ -n "$running_services" ]; then
        # Compose service names cannot contain shell whitespace.
        # shellcheck disable=SC2086
        compose start $running_services >/dev/null
    fi
    exit "$status"
}
trap restart_services EXIT HUP INT TERM

compose stop >/dev/null
if [ "$neo4j_data_included" -eq 1 ]; then
    still_active="$(list_active_services)"
    [ -z "$still_active" ] || {
        echo "Deployment services remain running after stop: $still_active" >&2
        exit 1
    }
fi
docker volume inspect "$qdrant_volume" >/dev/null
helper_image="$(compose images -q qdrant)"
[ -n "$helper_image" ] || {
    echo "Unable to resolve the Qdrant helper image" >&2
    exit 1
}
docker run --rm --user 0:0 \
    --mount "type=volume,source=$qdrant_volume,target=/source,readonly" \
    --mount "type=bind,source=$backup_abs,target=/backup" \
    --entrypoint tar "$helper_image" \
    -C /source -czf "/backup/$(basename "$qdrant_archive")" .
tar -C "$data_abs" -czf "$archive" .
(
    cd "$backup_abs"
    sha256sum "$archive_name" > "$archive_name.sha256"
    sha256sum "$(basename "$qdrant_archive")" > "$(basename "$qdrant_archive").sha256"
)
archive_sha256="$(awk '{print $1}' "$archive.sha256")"
qdrant_sha256="$(awk '{print $1}' "$qdrant_archive.sha256")"
{
    printf 'created_at=%s\n' "$timestamp"
    printf 'data_root=%s\n' "$data_abs"
    printf 'running_services=%s\n' "$(printf '%s' "$running_services" | tr '\n' ' ')"
    printf 'git_revision=%s\n' "$(git rev-parse HEAD 2>/dev/null || printf unknown)"
    printf 'format=2\n'
    printf 'archive=%s\n' "$archive_name"
    printf 'sha256=%s\n' "$archive_sha256"
    printf 'qdrant_volume_name=%s\n' "$qdrant_volume"
    printf 'qdrant_archive=%s\n' "$(basename "$qdrant_archive")"
    printf 'qdrant_sha256=%s\n' "$qdrant_sha256"
    if [ "$neo4j_data_included" -eq 1 ]; then
        printf 'neo4j_data=neo4j/data\n'
    fi
    printf 'images='
    compose images --format json 2>/dev/null \
        | tr '\n' ' '
    printf '\n'
} > "$metadata"
printf 'Backup created: %s\n' "$archive"
