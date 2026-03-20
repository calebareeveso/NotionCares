#!/usr/bin/env bash
# Shared MCP streamable-http helpers (initialize session + notify).
# Source this file: source "$(dirname "$0")/mcp_session.sh"
#
# Env: MCP_URL (default https://debbi-unlovely-melanie.ngrok-free.dev/mcp)

set -euo pipefail

MCP_URL="${MCP_URL:-https://debbi-unlovely-melanie.ngrok-free.dev/mcp}"

mcp_common_headers() {
  printf '%s\n' \
    "Content-Type: application/json" \
    "Accept: application/json, text/event-stream"
}

# Sets global SESSION_ID from initialize response headers.
mcp_init_session() {
  local tmp_body
  tmp_body="$(mktemp)"
  SESSION_ID="$(
    curl -sS -D - -o "$tmp_body" \
      -X POST "$MCP_URL" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cli-test-mcp","version":"1.0.0"}}}' \
    | awk '/mcp-session-id:/ {print $2}' | tr -d '\r'
  )"

  if [[ -z "${SESSION_ID:-}" ]]; then
    echo "Failed to obtain mcp-session-id. Response body:" >&2
    cat "$tmp_body" >&2
    rm -f "$tmp_body"
    exit 1
  fi
  rm -f "$tmp_body"
}

mcp_notify_initialized() {
  curl -sS -X POST "$MCP_URL" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SESSION_ID" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
    >/dev/null
}

# Args: JSON object of tools/call params only, e.g. {"name":"send_message","arguments":{...}}
mcp_tools_call() {
  local params_json="$1"
  local payload
  payload="$(python3 -c "
import json, sys
params = json.loads(sys.argv[1])
out = {
    'jsonrpc': '2.0',
    'id': 3,
    'method': 'tools/call',
    'params': params,
}
print(json.dumps(out))
" "$params_json")"

  local tmp_body
  tmp_body="$(mktemp)"

  local curl_http_code
  local curl_exit_code=0
  if [[ -n "${MCP_CURL_MAX_TIME:-}" ]]; then
    # Abort the HTTP request if the upstream/proxy hangs onto it.
    curl_http_code="$(
      curl -sS --show-error --max-time "${MCP_CURL_MAX_TIME}" -X POST "$MCP_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "mcp-session-id: $SESSION_ID" \
        -d "$payload" \
        -o "$tmp_body" \
        -w "%{http_code}" || true
    )"
    curl_exit_code=$?
  else
    curl_http_code="$(
      curl -sS --show-error -X POST "$MCP_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "mcp-session-id: $SESSION_ID" \
        -d "$payload" \
        -o "$tmp_body" \
        -w "%{http_code}" || true
    )"
    curl_exit_code=$?
  fi

  if [[ -s "$tmp_body" ]]; then
    # Print response body exactly once.
    cat "$tmp_body"
    echo
  else
    echo "{\"jsonrpc\":\"2.0\",\"id\":3,\"error\":{\"code\":0,\"message\":\"Empty MCP response body\"}}" \
      >&2
  fi

  echo "CURL_EXIT_CODE=$curl_exit_code HTTP_CODE=$curl_http_code" >&2
  rm -f "$tmp_body"
}
