#!/usr/bin/env bash
# Wrapper around the real Homebrew binary that fixes two issues on this
# stack's aarch64 Linux containers:
#
# 1. Intel-prefix detection bug — Homebrew detects its installation prefix
#    from argv[0]. When invoked via the symlink `/usr/local/bin/brew`, it
#    sets HOMEBREW_PREFIX=/usr/local and aborts with "Cannot install in
#    Homebrew on ARM processor in Intel default prefix". Calling the
#    real binary at /home/linuxbrew/.linuxbrew/bin/brew with the env vars
#    explicitly set bypasses that check.
#
# 2. steipete/tap → openclaw/tap migration — many OpenClaw skill registrations
#    still hardcode `steipete/tap/<formula>` install paths, but the formulae
#    were migrated to `openclaw/tap`. Homebrew's tap_migrations.json honors
#    the redirect only for tap-prefix-less installs (`brew install <name>`),
#    not for explicit `brew install steipete/tap/<name>`. This wrapper
#    detects that exact failure mode and retries with `openclaw/tap/<name>`.
#
# All other brew invocations pass through unchanged.

set -u

REAL_BREW=/home/linuxbrew/.linuxbrew/bin/brew
export HOMEBREW_PREFIX=/home/linuxbrew/.linuxbrew
export HOMEBREW_CELLAR=/home/linuxbrew/.linuxbrew/Cellar
export HOMEBREW_REPOSITORY=/home/linuxbrew/.linuxbrew/Homebrew
export PATH="/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:${PATH}"
export HOMEBREW_NO_ANALYTICS=${HOMEBREW_NO_ANALYTICS:-1}
export HOMEBREW_NO_AUTO_UPDATE=${HOMEBREW_NO_AUTO_UPDATE:-1}
export HOMEBREW_NO_ENV_HINTS=${HOMEBREW_NO_ENV_HINTS:-1}

# Detect steipete-tap install attempts and capture the formula name so we
# can fall back to openclaw/tap if the original install fails.
STEIPETE_FORMULA=""
if [ "$#" -ge 2 ] && [ "$1" = "install" ]; then
    for arg in "$@"; do
        case "$arg" in
            steipete/tap/*)
                STEIPETE_FORMULA="${arg#steipete/tap/}"
                break
                ;;
        esac
    done
fi

# Tap the openclaw repo if we're about to need the fallback. Idempotent.
if [ -n "$STEIPETE_FORMULA" ]; then
    "$REAL_BREW" tap openclaw/tap >/dev/null 2>&1 || true
fi

# First attempt: run brew exactly as requested.
"$REAL_BREW" "$@"
EXIT_CODE=$?

# If the install failed and we detected a steipete-tap formula, try the
# openclaw/tap fallback before reporting failure.
if [ "$EXIT_CODE" -ne 0 ] && [ -n "$STEIPETE_FORMULA" ]; then
    echo ""
    echo "[brew-wrapper] steipete/tap/${STEIPETE_FORMULA} install failed; retrying with openclaw/tap/${STEIPETE_FORMULA}…"
    # Build a new arg list with the steipete prefix swapped for openclaw.
    NEW_ARGS=()
    for arg in "$@"; do
        case "$arg" in
            steipete/tap/*)
                NEW_ARGS+=("openclaw/tap/${arg#steipete/tap/}")
                ;;
            *)
                NEW_ARGS+=("$arg")
                ;;
        esac
    done
    "$REAL_BREW" "${NEW_ARGS[@]}"
    EXIT_CODE=$?
fi

exit "$EXIT_CODE"
