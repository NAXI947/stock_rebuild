#!/usr/bin/env bash
set -euo pipefail
umask 077

fail() {
    printf 'Release error: %s\n' "$*" >&2
    exit 1
}

here=$(cd -- "$(dirname -- "$0")" && pwd -P)
root=$(cd -- "${STOCK_DEPLOY_ROOT:-$here/..}" && pwd -P)
export STOCK_DEPLOY_ROOT=$root
template=$here/docker-compose.prod.yml
automation_template=$here/docker-compose.automation.yml
state=$root/.stock-v4-releases
control=$root/runtime/release-control
pause=$control/paused
legacy=$root/.release.env
active=$state/active
pending=$state/pending

validate_manifest() {
    local file=$1 key value line
    local -A values=()
    [[ -f "$file" && ! -L "$file" ]] || fail 'Release manifest must be a regular file'
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" == *=* ]] || fail 'Malformed release manifest'
        key=${line%%=*}
        value=${line#*=}
        case "$key" in
            BUILD_RUN_ID|BUILD_COMMIT|BACKEND_IMAGE|WEB_IMAGE) ;;
            *) fail 'Unexpected release manifest field' ;;
        esac
        [[ ! -v "values[$key]" ]] || fail 'Duplicate release manifest field'
        values[$key]=$value
    done < "$file"
    [[ "${values[BUILD_RUN_ID]:-}" =~ ^[1-9][0-9]*$ &&
        "${values[BUILD_COMMIT]:-}" =~ ^[0-9a-f]{40}$ &&
        "${values[BACKEND_IMAGE]:-}" =~ ^ghcr\.io/naxi947/stock-v4-backend@sha256:[0-9a-f]{64}$ &&
        "${values[WEB_IMAGE]:-}" =~ ^ghcr\.io/naxi947/stock-v4-web@sha256:[0-9a-f]{64}$ ]] ||
        fail 'A verified build ID, commit and both fixed GHCR digests are required'
    backend_image=${values[BACKEND_IMAGE]}
    web_image=${values[WEB_IMAGE]}
}

read_automation() {
    local file=$1 key value line
    automation=()
    [[ -f "$file" && ! -L "$file" && "$(stat -c '%s' "$file")" -le 1024 ]] ||
        fail 'Automation settings must be a small regular file'
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" == *=* ]] || fail 'Malformed automation setting'
        key=${line%%=*}
        value=${line#*=}
        case "$key" in
            STOCK_HERMES_TRIGGER_MODE)
                [[ "$value" == internal || "$value" == octop ]] || fail 'Hermes trigger requires internal or octop'
                ;;
            STOCK_AUTOMATION_HERMES|STOCK_AUTOMATION_COLLECT|STOCK_AUTOMATION_QQ|STOCK_AUTOMATION_STOCK_RESEARCH|STOCK_AUTOMATION_FINDESK)
                [[ "$value" == true || "$value" == false ]] || fail 'Automation switches require true or false'
                ;;
            STOCK_AUTOMATION_SNAPSHOT_KEY)
                [[ "$value" =~ ^stock-v4:prod:[A-Za-z0-9_.:-]{1,100}$ ]] || fail 'Invalid production snapshot key'
                ;;
            STOCK_LEFT_RIGHT_EOD_CODES)
                [[ "$value" =~ ^[0-9]{6}\.(SH|SZ|BJ)(,[0-9]{6}\.(SH|SZ|BJ)){0,49}$ ]] ||
                    fail 'LEFT/RIGHT scope requires 1 to 50 comma-separated stock codes'
                local -a lr_codes
                local -A lr_seen=()
                local lr_code
                IFS=, read -r -a lr_codes <<< "$value"
                for lr_code in "${lr_codes[@]}"; do
                    [[ ! -v "lr_seen[$lr_code]" ]] || fail 'Duplicate LEFT/RIGHT stock code'
                    lr_seen[$lr_code]=1
                done
                ;;
            *) fail 'Unexpected automation setting' ;;
        esac
        [[ ! -v "automation[$key]" ]] || fail 'Duplicate automation setting'
        automation[$key]=$value
    done < "$file"
    [[ "${automation[STOCK_AUTOMATION_HERMES]:-false}" == true ]] || fail 'Automation settings require the Hermes schedule'
    if [[ "${automation[STOCK_AUTOMATION_STOCK_RESEARCH]:-false}" == true &&
          "${automation[STOCK_AUTOMATION_COLLECT]:-false}" != true ]]; then
        fail 'Stock research requires official collection'
    fi
    if [[ "${automation[STOCK_AUTOMATION_FINDESK]:-false}" == true &&
          "${automation[STOCK_AUTOMATION_COLLECT]:-false}" != true ]]; then
        fail 'Findesk requires official collection'
    fi
}

compose() {
    local manifest=$1 config=$2 settings=$3 overlay=$4
    shift 4
    validate_manifest "$manifest"
    local -a files=(-f "$config")
    local -A automation=()
    if [[ -n "$settings" ]]; then
        read_automation "$settings"
        [[ -f "$overlay" && ! -L "$overlay" ]] || fail 'Automation Compose overlay is missing'
        files+=(-f "$overlay")
    fi
    BACKEND_IMAGE=$backend_image WEB_IMAGE=$web_image \
        STOCK_HERMES_TRIGGER_MODE=${automation[STOCK_HERMES_TRIGGER_MODE]:-internal} \
        STOCK_AUTOMATION_HERMES=${automation[STOCK_AUTOMATION_HERMES]:-false} \
        STOCK_AUTOMATION_COLLECT=${automation[STOCK_AUTOMATION_COLLECT]:-false} \
        STOCK_AUTOMATION_QQ=${automation[STOCK_AUTOMATION_QQ]:-false} \
        STOCK_AUTOMATION_STOCK_RESEARCH=${automation[STOCK_AUTOMATION_STOCK_RESEARCH]:-false} \
        STOCK_AUTOMATION_FINDESK=${automation[STOCK_AUTOMATION_FINDESK]:-false} \
        STOCK_LEFT_RIGHT_EOD_CODES=${automation[STOCK_LEFT_RIGHT_EOD_CODES]:-} \
        STOCK_AUTOMATION_SNAPSHOT_KEY=${automation[STOCK_AUTOMATION_SNAPSHOT_KEY]:-stock-v4:prod:source:market} \
        docker compose --project-directory "$root" --project-name stock-v4 \
            --env-file "$manifest" "${files[@]}" "$@"
}

preflight() {
    local role part dir token=$root/data/workspace.token calendar=$root/data/trading-calendar.json
    local -a token_lines
    [[ -f "$root/.env" && ! -L "$root/.env" && "$(stat -c '%a' "$root/.env")" == 600 ]] ||
        fail 'The deployment .env must be a private regular file with mode 0600'
    [[ -f "$token" && ! -L "$token" && "$(stat -c '%u:%g:%a' "$token")" == 10001:10001:600 ]] ||
        fail 'Set data/workspace.token to UID:GID 10001:10001 and mode 0600'
    [[ -d "$root/data" && ! -L "$root/data" && -d "$root/runtime" && ! -L "$root/runtime" ]] ||
        fail 'data and runtime must be existing regular directories'
    [[ "$(stat -c '%s' "$token")" -le 129 ]] || fail 'workspace.token is too large'
    mapfile -t token_lines < "$token"
    [[ "${#token_lines[@]}" -eq 1 && "${token_lines[0]}" =~ ^[A-Za-z0-9_-]{32,128}$ ]] ||
        fail 'workspace.token must contain a single 32–128 character token'
    [[ -f "$calendar" && ! -L "$calendar" && "$(stat -c '%u:%g:%a' "$calendar")" == 0:10001:640 ]] ||
        fail 'Set data/trading-calendar.json to UID:GID 0:10001 and mode 0640'
    [[ -d "$root/runtime/tmp" && ! -L "$root/runtime/tmp" ]] ||
        fail 'runtime/tmp must be an existing regular directory'
    [[ "$(stat -c '%u:%g:%a' "$root/runtime/tmp")" == 10001:10001:700 ]] ||
        fail 'Set runtime/tmp itself to UID:GID 10001:10001 and mode 0700 for the shared budget'
    for role in api worker web; do
        for part in '' /system /cache; do
            dir=$root/runtime/tmp/$role$part
            [[ -d "$dir" && ! -L "$dir" && "$(stat -c '%u:%g:%a' "$dir")" == 10001:10001:700 ]] ||
                fail "Expected private UID:GID 10001:10001 directory runtime/tmp/$role$part"
        done
    done
    docker network inspect 1panel-network >/dev/null
}

bundle_id() {
    local dir=$1 digest file
    local -a files=("$dir/release.env" "$dir/docker-compose.prod.yml")
    if [[ -e "$dir/automation.env" || -L "$dir/automation.env" || -e "$dir/docker-compose.automation.yml" || -L "$dir/docker-compose.automation.yml" ]]; then
        files+=("$dir/automation.env" "$dir/docker-compose.automation.yml")
        for file in "${files[@]}"; do
            [[ -f "$file" && ! -L "$file" ]] || fail 'Incomplete automation bundle'
        done
        digest=$({ for file in "${files[@]}"; do sha256sum < "$file" || return; done; } | sha256sum) || return
    else
        digest=$(cat -- "${files[@]}" | sha256sum) || return
    fi
    printf '%s\n' "${digest%% *}"
}

bundle_path() {
    local id=$1 dir
    [[ "$id" =~ ^[0-9a-f]{64}$ ]] || fail 'Malformed release state'
    dir=$state/$id
    [[ -d "$dir" && ! -L "$dir" && -f "$dir/docker-compose.prod.yml" &&
        ! -L "$dir/docker-compose.prod.yml" ]] || fail 'Release bundle is missing'
    validate_manifest "$dir/release.env"
    [[ "$(bundle_id "$dir")" == "$id" ]] || fail 'Release bundle integrity check failed'
    printf '%s\n' "$dir"
}

compose_bundle() {
    local bundle=$1 settings=''
    shift
    if [[ -f "$bundle/automation.env" ]]; then
        settings=$bundle/automation.env
    fi
    compose "$bundle/release.env" "$bundle/docker-compose.prod.yml" "$settings" "$bundle/docker-compose.automation.yml" "$@"
}

save_bundle() {
    local manifest=$1 settings=$2 stage id
    stage=$(mktemp -d "$state/.bundle-XXXXXXXX")
    cp -- "$manifest" "$stage/release.env"
    cp -- "$template" "$stage/docker-compose.prod.yml"
    if [[ -n "$settings" ]]; then
        cp -- "$settings" "$stage/automation.env"
        cp -- "$automation_template" "$stage/docker-compose.automation.yml"
    fi
    validate_manifest "$stage/release.env"
    id=$(bundle_id "$stage")
    if [[ -d "$state/$id" ]]; then
        rm -- "$stage/release.env" "$stage/docker-compose.prod.yml"
        if [[ -n "$settings" ]]; then
            rm -- "$stage/automation.env" "$stage/docker-compose.automation.yml"
        fi
        rmdir -- "$stage"
    else
        mv -T -- "$stage" "$state/$id"
    fi
    bundle_path "$id" >/dev/null
    printf '%s\n' "$id"
}

read_active() {
    local -a entries
    current=
    previous=
    if [[ -f "$active" ]]; then
        [[ ! -L "$active" ]] || fail 'Invalid active release state'
        mapfile -t entries < "$active"
        [[ "${#entries[@]}" -eq 2 ]] || fail 'Invalid active release state'
        current=${entries[0]}
        previous=${entries[1]}
        bundle_path "$current" >/dev/null
        if [[ -n "$previous" ]]; then
            bundle_path "$previous" >/dev/null
        fi
    fi
}

write_state() {
    local target=$1
    shift
    printf '%s\n' "$@" > "$target.next"
    mv -Tf -- "$target.next" "$target"
}

check_first_install() {
    if [[ -e "$legacy" ]]; then
        [[ -f "$legacy" && ! -L "$legacy" ]] || fail 'Invalid legacy .release.env'
        cmp -s -- "$legacy" <(printf '%s\n' \
            'BACKEND_IMAGE=ghcr.io/your_lowercase_owner/stock-v4-backend@sha256:REPLACE_WITH_REAL_DIGEST' \
            'WEB_IMAGE=ghcr.io/your_lowercase_owner/stock-v4-web@sha256:REPLACE_WITH_REAL_DIGEST') ||
            fail 'Found a legacy release that requires explicit adoption'
    fi
    local existing
    existing=$(docker ps --all --quiet --filter label=com.docker.compose.project=stock-v4)
    [[ -z "$existing" ]] || fail 'Existing stock-v4 project containers require explicit adoption'
    existing=$(docker ps --all --quiet --filter 'name=^/stock-v4-(backend|worker|web)$')
    [[ -z "$existing" ]] || fail 'Existing stock-v4 containers require explicit adoption'
}

start_pause() {
    : > "$pause"
    chmod 644 "$pause"
}

clear_pause() {
    rm -- "$pause" && rm -f -- "$pending"
}

drain_api() {
    local bundle=$1 api running
    api=$(compose_bundle "$bundle" ps --all --quiet stock-backend) || return
    [[ -n "$api" ]] || return 0
    running=$(docker inspect --format '{{.State.Running}}' "$api") || return
    [[ "$running" == true ]] || return 0
    # The single-request WSGI server answers after an in-flight write finishes.
    compose_bundle "$bundle" exec -T stock-backend python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=35).close()" </dev/null
}

wait_background_jobs() {
    local bundle=$1
    # Run in the OLD backend image: this inline reader deliberately needs no new
    # runtime command. Both collection and ETF maintenance use the backfill table.
    # The pause marker prevents new claims; running collectors checkpoint to
    # QUEUED, while bounded manual backtests finish before worker shutdown.
    compose_bundle "$bundle" exec -T stock-backend python -B - <<'DRAIN_PY'
from time import monotonic, sleep
from modules.strategy_hermes.runtime import _ledger, _paused

ledger, paused = _ledger(), _paused()
deadline, quiet_since, previous = monotonic() + 140, None, None
while True:
    if not paused():
        raise SystemExit("Release pause is missing; refusing worker shutdown.")
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '5s'")
        counts = conn.execute("""SELECT
            (SELECT count(*) FROM left_right_backfill_jobs WHERE ns=%s AND status='RUNNING'),
            (SELECT count(*) FROM manual_jobs WHERE ns=%s AND status='RUNNING')""",
            (ledger.ns, ledger.ns)).fetchone()
    now = monotonic()
    if counts != previous:
        print(f"Release drain: background={counts[0]}, manual={counts[1]}", flush=True)
        previous = counts
    if not any(counts):
        quiet_since = now if quiet_since is None else quiet_since
        if now - quiet_since >= 1:
            break
    else:
        quiet_since = None
    if now >= deadline:
        raise SystemExit("Background jobs are still running; maintenance stays paused and worker stays running.")
    sleep(.5)
DRAIN_PY
}

drain_worker() {
    local bundle=$1 mode=${2:-release} worker running exit_state
    worker=$(compose_bundle "$bundle" ps --all --quiet stock-worker) || return
    [[ -n "$worker" ]] || return 0
    running=$(docker inspect --format '{{.State.Running}}' "$worker") || return
    if [[ "$mode" == recover ]]; then
        [[ "$running" == true ]] || return 0
    fi
    if [[ "$running" == true ]]; then
        wait_background_jobs "$bundle" || return
    fi
    compose_bundle "$bundle" stop --timeout 150 stock-worker || return
    exit_state=$(docker inspect --format '{{.State.ExitCode}} {{.State.OOMKilled}}' "$worker") || return
    if [[ "$exit_state" != '0 false' ]]; then
        printf 'Worker did not exit cleanly (%s); maintenance stays paused.\n' "$exit_state" >&2
        return 1
    fi
}

ensure_images() {
    local bundle=$1
    validate_manifest "$bundle/release.env"
    if ! docker image inspect "$backend_image" "$web_image" >/dev/null 2>&1; then
        compose_bundle "$bundle" pull
    fi
}

start_bundle() {
    local bundle=$1
    compose_bundle "$bundle" up -d --no-build --pull never --force-recreate --wait --wait-timeout 120 &&
        compose_bundle "$bundle" exec -T stock-backend python -m modules.strategy_hermes.runtime smoke </dev/null
}

recover_previous() {
    local candidate=$1 restored
    if [[ -n "$current" ]]; then
        restored=$(bundle_path "$current") || return
        # The pause marker remains present throughout recovery.
        ensure_images "$restored" || return
        start_bundle "$restored" || return
    else
        compose_bundle "$candidate" stop || return
        compose_bundle "$candidate" rm -f stock-web stock-worker stock-backend || return
    fi
    clear_pause
}

automation_settings=
case "${1:-}" in
    check|apply)
        [[ "$#" -eq 2 || "$#" -eq 4 ]] && [[ "$2" == /* ]] ||
            fail "Usage: $0 $1 /absolute/path/release.env [--automation /absolute/path/automation.env]"
        manifest=$2
        validate_manifest "$manifest"
        if [[ "$#" -eq 4 ]]; then
            [[ "$3" == --automation && "$4" == /* ]] || fail 'Expected --automation with an absolute settings path'
            automation_settings=$4
            declare -A automation=()
            read_automation "$automation_settings"
            [[ -f "$automation_template" && ! -L "$automation_template" ]] || fail 'Automation Compose overlay is missing'
        fi
        ;;
    rollback|recover)
        [[ "$#" -eq 1 ]] || fail "Usage: $0 $1"
        ;;
    *) fail "Usage: $0 {check /absolute/path/release.env|apply /absolute/path/release.env|rollback|recover}" ;;
esac

preflight
if [[ "$1" == check ]]; then
    compose "$manifest" "$template" "$automation_settings" "$automation_template" config --quiet
    printf 'Deployment directories, token, calendar, network, manifest and Compose configuration passed.\n'
    exit 0
fi

[[ ! -L "$state" && ! -L "$control" ]] || fail 'Release state and control paths must not be symbolic links'
install -d -m 700 -- "$state"
exec 9>"$state/lock"
flock -n 9 || fail 'Another release operation is in progress'
install -d -m 755 -- "$control"
[[ ! -L "$pause" && ! -L "$pending" && ! -L "$active" ]] || fail 'Release state files must not be symbolic links'
read_active

if [[ "$1" == recover ]]; then
    [[ -f "$pause" || -f "$pending" ]] || fail 'No interrupted release to recover'
    if [[ -f "$pending" ]]; then
        candidate=$(bundle_path "$(cat -- "$pending")")
    elif [[ -n "$current" ]]; then
        candidate=$(bundle_path "$current")
    else
        fail 'No recorded release bundle to recover'
    fi
    start_pause
    if [[ -n "$current" ]]; then
        running_bundle=$(bundle_path "$current")
        drain_worker "$running_bundle" recover || fail 'Inspect the stopped worker before recovering'
        drain_api "$running_bundle" || fail 'Inspect the running API before recovering'
    fi
    recover_previous "$candidate" || fail 'Recovery failed; maintenance stays paused'
    printf 'Interrupted release recovered.\n'
    exit 0
fi

[[ ! -e "$pause" && ! -e "$pending" ]] || fail 'Interrupted release detected; inspect it and use recover'
if [[ "$1" == apply ]]; then
    if [[ -z "$current" ]]; then
        check_first_install
    fi
    next=$(save_bundle "$manifest" "$automation_settings")
    if [[ "$next" == "$current" ]]; then
        printf 'This release is already active.\n'
        exit 0
    fi
else
    [[ -n "$previous" ]] || fail 'No previous release is available'
    next=$previous
fi

candidate=$(bundle_path "$next")
compose_bundle "$candidate" config --quiet
ensure_images "$candidate"

write_state "$pending" "$next"
start_pause
trap 'if [[ -f "$pause" || -f "$pending" ]]; then printf "Maintenance may remain paused. Inspect the deployment and run recover.\n" >&2; fi' EXIT
if [[ -n "$current" ]]; then
    running_bundle=$(bundle_path "$current")
    drain_worker "$running_bundle" || fail 'Worker drain failed before schema changes'
    drain_api "$running_bundle" || fail 'API drain failed before schema changes'
fi

if { [[ "$1" == rollback ]] || compose_bundle "$candidate" run --rm --no-deps -T stock-backend \
        python -m modules.strategy_hermes.runtime migrate </dev/null; } && start_bundle "$candidate"; then
    write_state "$active" "$next" "$current"
    clear_pause
    printf 'Release %s is active.\n' "$next"
else
    printf 'Release failed. Restoring the last active application bundle.\n' >&2
    recover_previous "$candidate" || fail 'Application recovery failed; maintenance stays paused'
    fail 'Release failed and application recovery completed'
fi
