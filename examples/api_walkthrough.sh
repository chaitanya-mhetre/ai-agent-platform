#!/usr/bin/env bash
# Walk through the HTTP API against a running server (default: offline fake model).
#   AGENTPLAT_BOOTSTRAP_ADMINS='["acme:boss"]' AGENTPLAT_OFFLINE_WEB=true uv run uvicorn agentplat.api.app:app
set -euo pipefail
BASE=${BASE:-http://localhost:8000}
ALICE=(-H "X-Tenant-Id: acme" -H "X-User-Id: alice" -H "Content-Type: application/json")
BOSS=(-H "X-Tenant-Id: acme" -H "X-User-Id: boss" -H "Content-Type: application/json")

curl -s "${BOSS[@]}" -X POST "$BASE/v1/users/alice/permissions" -d '{"permissions":["notify:send","web:read"]}'; echo
AGENT=$(curl -s "${ALICE[@]}" -X POST "$BASE/v1/agents" \
  -d '{"name":"ops","system_prompt":"Help the ops team.","allowed_tools":["calculator","http_get","send_notification"]}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "agent: $AGENT"

RUN=$(curl -s "${ALICE[@]}" -X POST "$BASE/v1/runs" \
  -d "{\"agent_id\":\"$AGENT\",\"input\":\"Notify the team that the deploy is done\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "run: $RUN"; sleep 1

curl -s "${ALICE[@]}" "$BASE/v1/runs/$RUN" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("status:",d["status"])'
APPROVAL=$(curl -s "${ALICE[@]}" "$BASE/v1/approvals" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')
curl -s "${BOSS[@]}" -X POST "$BASE/v1/approvals/$APPROVAL" -d '{"decision":"approve","comment":"ok"}'; echo
sleep 1
curl -s "${ALICE[@]}" "$BASE/v1/runs/$RUN" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("status:",d["status"],"|",d["final_output"])'
curl -s "${ALICE[@]}" "$BASE/v1/runs/$RUN/trace" | python3 -m json.tool | head -30
