#!/usr/bin/env bash
# Dispatch a prompt to a named agent role, in a fresh context, from any runtime.
#
# This is the portability seam. Skills name a ROLE (REVIEWER, ESCALATOR,
# EXPLORER) and never a model; the role → command mapping lives in one place
# (config/agent-roles.conf) so a model generation change is a one-line edit.
#
# A runtime with its own fresh-context sub-agent primitive (e.g. Claude Code's
# Agent tool) may use that instead, provided it honours the role's _MODEL. This
# script is the fallback that works everywhere, including runtimes with no
# sub-agent primitive at all -- and it is the canonical definition of what a
# role dispatch means.
#
# Usage:
#   scripts/agent-dispatch.sh REVIEWER prompt.txt      # prompt from a file
#   echo "..." | scripts/agent-dispatch.sh REVIEWER -  # prompt from stdin
#   scripts/agent-dispatch.sh --probe                  # check every role
#   scripts/agent-dispatch.sh --probe REVIEWER         # check one role
#
# Exit codes:
#   0  success
#   2  usage error
#   3  ROLE UNAVAILABLE -- not configured, or its binary is missing. This is a
#      hard stop, never a licence to self-review: see docs/agents/runtime.md
#      § Degraded mode.
#   *  otherwise the dispatched command's own exit code

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="${AGENT_ROLES_CONF:-$REPO_ROOT/config/agent-roles.conf}"
ROLES="REVIEWER ESCALATOR EXPLORER"

die() { echo "agent-dispatch: $*" >&2; exit 2; }

if [ ! -f "$CONF" ]; then
    die "no role mapping at $CONF (see docs/agents/runtime.md)"
fi
# shellcheck disable=SC1090
. "$CONF"

# Resolve a role to its command, or fail with code 3.
resolve() {
    local role="$1" cmd
    cmd="$(eval "printf '%s' \"\${${role}_CMD:-}\"")"
    if [ -z "$cmd" ]; then
        cat >&2 <<MSG
agent-dispatch: role $role is not configured.

  ${role}_CMD is unset in $CONF.
  Fill it in, then re-run: scripts/agent-dispatch.sh --probe $role
MSG
        return 3
    fi
    # First word of _CMD is the binary.
    local bin="${cmd%% *}"
    if ! command -v "$bin" >/dev/null 2>&1; then
        cat >&2 <<MSG
agent-dispatch: role $role is configured but unreachable.

  ${role}_CMD = $cmd
  '$bin' is not on PATH. Install or authenticate it, or point $role at a
  runtime you do have (config/agent-roles.conf).
MSG
        return 3
    fi
    printf '%s' "$cmd"
}

# --- probe mode --------------------------------------------------------------
if [ "${1:-}" = "--probe" ]; then
    shift
    targets="${*:-$ROLES}"
    failed=0
    for role in $targets; do
        if cmd="$(resolve "$role" 2>/dev/null)"; then
            echo "ok        $role -> $cmd"
        else
            echo "UNUSABLE  $role"
            failed=1
        fi
    done
    impl="${IMPLEMENTER_LABEL:-<unset>}"
    echo "implementer: $impl"
    # Same vendor on both sides of the review is legal but weaker -- flag it.
    rev="$(eval "printf '%s' \"\${REVIEWER_CMD:-}\"")"
    if [ -n "$rev" ] && [ "$impl" != "<unset>" ] && \
       [ "${impl%%:*}" = "claude-code" ] && [ "${rev%% *}" = "claude" ]; then
        echo "note: implementer and reviewer are the same vendor -- acceptable," \
             "but a cross-vendor reviewer catches more (docs/agents/runtime.md)."
    fi
    if [ "$failed" -eq 1 ]; then
        echo "" >&2
        echo "One or more roles are unusable. Work that needs them must stop at" >&2
        echo "In Review for a human -- see docs/agents/runtime.md § Degraded mode." >&2
        exit 3
    fi
    exit 0
fi

# --- dispatch mode -----------------------------------------------------------
[ "$#" -ge 2 ] || die "usage: agent-dispatch.sh <ROLE> <prompt-file|-> | --probe [ROLE]"

ROLE="$1"; PROMPT_SRC="$2"; shift 2

case " $ROLES " in
    *" $ROLE "*) ;;
    *) die "unknown role '$ROLE' (known: $ROLES, and IMPLEMENTER which is never dispatched)" ;;
esac

if [ "$PROMPT_SRC" != "-" ] && [ ! -f "$PROMPT_SRC" ]; then
    die "prompt file not found: $PROMPT_SRC"
fi

CMD="$(resolve "$ROLE")" || exit 3

# Word-split _CMD deliberately: it is an operator-supplied command line.
# shellcheck disable=SC2086
if [ "$PROMPT_SRC" = "-" ]; then
    exec $CMD "$@"
else
    exec $CMD "$@" < "$PROMPT_SRC"
fi
