#!/usr/bin/env bash
# Interactive MCP smoke tests: Telegram (await response) or voice call.
#
# Usage:
#   ./scripts/test-mcp.sh telegram
#   ./scripts/test-mcp.sh call
#   MCP_URL=https://xxxx.ngrok-free.app/mcp ./scripts/test-mcp.sh telegram
#
# Defaults match the values you used in curl tests.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/mcp_session.sh"

DEFAULT_TEXT='Hey man, what is your name ?'
DEFAULT_PHONE='+447930002899'
DEFAULT_Q1='What did you eat today?'
DEFAULT_Q2='Did you take your medication?'
DEFAULT_TIMEOUT_TELEGRAM=300
DEFAULT_TIMEOUT_CALL=60

prompt_with_default() {
  local message="$1"
  local default="$2"
  local value=""
  if [[ -t 0 ]]; then
    read -r -p "$message [$default]: " value || true
  fi
  if [[ -z "${value:-}" ]]; then
    echo "$default"
  else
    echo "$value"
  fi
}

is_local_mcp_url() {
  local url="$1"
  [[ "$url" == http://localhost:* ]] || [[ "$url" == http://127.0.0.1:* ]] || [[ "$url" == http://[::1]/* ]]
}

run_telegram() {
  echo "MCP URL: $MCP_URL"

  # Long-held HTTP requests (waiting for Telegram reply) often fail through
  # public reverse proxies/SSO tunnels. The webhook can still be public; only
  # the tool-call side needs to be reliable.
  if ! is_local_mcp_url "$MCP_URL"; then
    local use_local="Y"
    if [[ -t 0 ]]; then
      use_local="$(prompt_with_default "Public MCP URL may drop long waits. Use localhost MCP for the tool call?" "Y")"
    fi
    local use_local_lc
    use_local_lc="$(echo "$use_local" | tr '[:upper:]' '[:lower:]')"
    if [[ "$use_local_lc" == "y" || "$use_local_lc" == "yes" ]]; then
      echo "Switching tool call URL to http://localhost:8001/mcp (webhook remains public)."
      MCP_URL="http://localhost:8001/mcp"
    fi
  fi

  local text
  text="$(prompt_with_default "Text message to send" "$DEFAULT_TEXT")"
  local default_timeout="$DEFAULT_TIMEOUT_TELEGRAM"
  if ! is_local_mcp_url "$MCP_URL"; then
    # Many public reverse proxies cut off long-held HTTP requests.
    default_timeout=60
    echo "Note: using a non-local MCP URL; proxy setups may drop long waits. Default timeout reduced to ${default_timeout}s."
  fi

  local timeout_s
  timeout_s="$(prompt_with_default "Timeout (seconds) waiting for Telegram reply" "$default_timeout")"

  echo "Creating MCP session..."
  mcp_init_session
  mcp_notify_initialized
  echo "Calling send_message_await_response..."
  local params
  params="$(python3 -c "
import json, sys
print(json.dumps({
    'name': 'send_message_await_response',
    'arguments': {
        'text': sys.argv[1],
        'timeout_seconds': int(sys.argv[2]),
    },
}))
" "$text" "$timeout_s")"
  export MCP_CURL_MAX_TIME=$((timeout_s + 20))
  mcp_tools_call "$params"
  echo
}

run_call() {
  echo "MCP URL: $MCP_URL"
  echo "This places a real outbound call via Vonage."
  local phone
  phone="$(prompt_with_default "Phone number (E.164)" "$DEFAULT_PHONE")"

  echo "Enter questions (one per line). Press Enter on an empty line to finish."
  echo "Defaults if you press Enter on first line: $DEFAULT_Q1 | $DEFAULT_Q2"
  local questions=()
  if [[ -t 0 ]]; then
    local line
    local i=0
    while IFS= read -r line; do
      [[ -z "$line" && ${#questions[@]} -gt 0 ]] && break
      if [[ -z "$line" && ${#questions[@]} -eq 0 ]]; then
        questions=("$DEFAULT_Q1" "$DEFAULT_Q2")
        break
      fi
      questions+=("$line")
      ((i++)) || true
    done
  else
    questions=("$DEFAULT_Q1" "$DEFAULT_Q2")
  fi

  if [[ ${#questions[@]} -eq 0 ]]; then
    questions=("$DEFAULT_Q1" "$DEFAULT_Q2")
  fi

  local timeout_s
  timeout_s="$(prompt_with_default "Max seconds to wait for call to complete" "$DEFAULT_TIMEOUT_CALL")"
  export MCP_CURL_MAX_TIME=$((timeout_s + 30))

  echo "Creating MCP session..."
  mcp_init_session
  mcp_notify_initialized
  echo "Calling call_user_await_response..."
  local questions_json
  questions_json="$(printf '%s\n' "${questions[@]}" | python3 -c "
import json, sys
qs = [line.strip() for line in sys.stdin if line.strip()]
print(json.dumps(qs))
")"
  local params
  params="$(python3 -c "
import json, sys
phone = sys.argv[1]
timeout_s = int(sys.argv[2])
questions = json.loads(sys.argv[3])
print(json.dumps({
    'name': 'call_user_await_response',
    'arguments': {
        'phone_number': phone,
        'questions': questions,
        'timeout_seconds': timeout_s,
    },
}))
" "$phone" "$timeout_s" "$questions_json")"
  mcp_tools_call "$params"
  echo
}

usage() {
  cat <<EOF
Usage: $(basename "$0") <telegram|call>

  telegram  — MCP tool send_message_await_response (prompts for message)
  call      — MCP tool call_user_await_response (prompts for phone + questions)

Env:
  MCP_URL   Base MCP endpoint (default: http://localhost:8001/mcp)
EOF
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    telegram)
      run_telegram
      ;;
    call)
      run_call
      ;;
    -h|--help|help|'')
      usage
      [[ -n "$cmd" ]] || exit 1
      exit 0
      ;;
    *)
      echo "Unknown command: $cmd" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
