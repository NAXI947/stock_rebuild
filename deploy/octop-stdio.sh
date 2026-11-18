#!/usr/bin/env bash
set -euo pipefail

deploy=/opt/1panel/apps/local/stock-system/deploy
bridge=$deploy/octop_bridge.py
skill=$deploy/a-share-hotspot-scout.md
qq_template=$deploy/qq-brief.md
if [[ ! "${STOCK_V4_OCTOP_CONTAINER:-}" =~ ^1Panel-octop-[A-Za-z0-9_-]+$ ]]; then
    printf 'Configured OCTOP container name is invalid.\n' >&2
    exit 1
fi
if [[ ! -f "$skill" || -L "$skill" || "$(stat -c '%u:%g:%a' "$skill")" != 0:0:644 ||
      "$(stat -c '%s' "$skill")" -gt 36864 ]]; then
    printf 'Install the reviewed Stock V4 skill as a regular root-owned mode 0644 file.\n' >&2
    exit 1
fi
STOCK_V4_SKILL_B64=$(base64 -w 0 -- "$skill")
export STOCK_V4_SKILL_B64
STOCK_V4_QQ_TEMPLATE_B64=
if [[ -e "$qq_template" || -L "$qq_template" ]]; then
    if [[ ! -f "$qq_template" || -L "$qq_template" ||
          "$(stat -c '%u:%g:%a' "$qq_template")" != 0:0:644 ||
          "$(stat -c '%s' "$qq_template")" -gt 8192 ]]; then
        printf 'Ignoring QQ template with invalid ownership, mode or size.\n' >&2
    else
        STOCK_V4_QQ_TEMPLATE_B64=$(base64 -w 0 -- "$qq_template")
    fi
fi
export STOCK_V4_QQ_TEMPLATE_B64
exec /usr/bin/docker exec -i \
    -e PYTHONDONTWRITEBYTECODE=1 -e LANGSMITH_TRACING=false -e LANGCHAIN_TRACING_V2=false \
    -e STOCK_V4_QQ_ROUTE_SHA256 -e STOCK_V4_SKILL_B64 -e STOCK_V4_QQ_TEMPLATE_B64 "$STOCK_V4_OCTOP_CONTAINER" \
    python -c "$(cat -- "$bridge")"
