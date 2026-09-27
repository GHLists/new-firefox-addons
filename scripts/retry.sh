#!/usr/bin/env bash

RETRY_MAX_ATTEMPTS="${RETRY_MAX_ATTEMPTS:-3}"
RETRY_INITIAL_DELAY="${RETRY_INITIAL_DELAY:-10}"
RETRY_MAX_DELAY="${RETRY_MAX_DELAY:-30}"

retry() {
  local attempt=1 delay
  while [ "$attempt" -le "$RETRY_MAX_ATTEMPTS" ]; do
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -lt "$RETRY_MAX_ATTEMPTS" ]; then
      delay=$((RETRY_INITIAL_DELAY * (2 ** (attempt - 1))))
      if [ "$delay" -gt "$RETRY_MAX_DELAY" ]; then
        delay="$RETRY_MAX_DELAY"
      fi
      echo "Attempt ${attempt}/${RETRY_MAX_ATTEMPTS} failed, retrying in ${delay}s..." >&2
      sleep "$delay"
    fi
    attempt=$((attempt + 1))
  done
  echo "All ${RETRY_MAX_ATTEMPTS} attempts failed: $*" >&2
  return 1
}
