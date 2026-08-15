#!/usr/bin/env bash
# Fail if public repository content contains known private-context or secret patterns.

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

for tool in find grep; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "error: required tool not found: $tool" >&2
    exit 1
  }
done

FILES=()
while IFS= read -r -d '' path; do
  FILES+=("$path")
done < <(
  find "$REPO_ROOT" \
    \( -path "$REPO_ROOT/.git" -o -path "$REPO_ROOT/.venv" \
       -o -path "$REPO_ROOT/build" -o -path "$REPO_ROOT/dist" \
       -o -path "$REPO_ROOT/.pytest_cache" -o -name __pycache__ \
       -o -name '*.egg-info' \) -prune -o \
    -type f -print0
)

if ((${#FILES[@]} == 0)); then
  echo "error: no repository files found" >&2
  exit 1
fi

scan() {
  local label=$1 pattern=$2
  if LC_ALL=C grep -IEn -- "$pattern" "${FILES[@]}"; then
    echo "error: $label found" >&2
    exit 1
  fi
}

personal_path_pattern='(/Use''rs/|/ho''me/[[:alnum:]_.-]+/)'
private_workspace_pattern='(chat-in-''claude|Tail''scale|tail''scale|host''name[[:space:]]*=)'
family_pattern='(^|[^[:alnum:]_])(아''내|남''편|배우''자|와이''프|wi''fe|hus''band|spou''se)([^[:alnum:]_]|$)'
private_title_pattern='(^|[^[:alnum:]_])(Ba''bel|SUN''SPOT|W''AF|2683''12468)([^[:alnum:]_]|$)|Fam''ily[[:space:]]+Shar''ing|이[[:space:]]+M''ac'
private_key_pattern='-----BE''GIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
real_name_pattern='(Young''ji[[:space:]]+K''im|김[[:space:]]*영''지)'
copyright_notice='Copyright (c) 2026 Young''ji K''im'

scan "personal filesystem path" "$personal_path_pattern"
scan "private workspace reference" "$private_workspace_pattern"
scan "family reference" "$family_pattern"
scan "known private title or identifier" "$private_title_pattern"
scan "email address" '[[:alnum:]._%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,}'
scan "private key header" "$private_key_pattern"
scan "Anthropic/OpenAI key" '(sk-ant-|sk-proj-)[[:alnum:]_-]{16,}'
scan "AWS key" '(AKIA|ASIA)[[:alnum:]]{16}'
scan "Google API key" 'AIza[[:alnum:]_-]{20,}'
scan "GitHub token" 'gh[pousr]_[[:alnum:]]{20,}'
scan "Slack token" 'xox[baprs]-[[:alnum:]-]{20,}'
scan "Stripe live key" '(sk|rk)_live_[[:alnum:]]{16,}'
scan "IPv4 address" '(^|[^0-9])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9]|$)'

# The copyright holder is intentionally present only in LICENSE.
NAME_FILES=()
for path in "${FILES[@]}"; do
  [[ "$path" == "$REPO_ROOT/LICENSE" ]] || NAME_FILES+=("$path")
done
if LC_ALL=C grep -IEn -- "$real_name_pattern" "${NAME_FILES[@]}"; then
  echo "error: personal name found outside the required LICENSE notice" >&2
  exit 1
fi
grep -Fxq "$copyright_notice" "$REPO_ROOT/LICENSE" || {
  echo "error: expected LICENSE copyright notice is missing" >&2
  exit 1
}

echo "hygiene check: PASS (${#FILES[@]} files scanned; LICENSE name allowlisted)"
