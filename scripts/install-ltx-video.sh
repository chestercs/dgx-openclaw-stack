#!/usr/bin/env bash
# install-ltx-video.sh — one-shot LTX-Video 2.3 model bundle + node-pack
# installer for the operator's ComfyUI install. Run on the deploy host
# (the same machine that owns the ComfyUI basedir), NOT inside a Claude /
# MCP session.
#
# What it does, in order:
#   1. Validates the target ComfyUI basedir + writeability.
#   2. Verifies (or prompts to install) huggingface-cli with hf_transfer.
#   3. Verifies (or installs) HF_TOKEN / `hf auth login`. Aborts before
#      starting a 70+ GB download if auth is missing.
#   4. git-clones Lightricks/ComfyUI-LTXVideo into <basedir>/custom_nodes/
#      (skips if already present; pulls fresh if --update is passed).
#   5. Downloads the selected LTX-2.3 main checkpoint (~46 GB) +
#      Gemma 3 12B text encoder (~25 GB). Optional spatial / temporal
#      upscalers (~1.3 GB) with --with-upscalers.
#   6. Prints next-step instructions (restart ComfyUI, drop workflow
#      JSONs in place, enable LTX_VIDEO_ENABLED in .env).
#
# Idempotent: re-runs only download missing files (huggingface-cli does
# checksummed resume). Re-runs do not duplicate git clones.
#
# Usage:
#   ./scripts/install-ltx-video.sh --basedir /path/to/comfyui [options]
#
#   --basedir PATH         Operator's ComfyUI basedir (the dir that has
#                          `models/`, `custom_nodes/`, `output/` under
#                          it). Required unless COMFYUI_BASEDIR is set.
#   --variant V            Which main checkpoint to download.
#                          BF16 variants (Lightricks/LTX-2.3, ~46 GB each):
#                            distilled-1.1 (default) — latest distilled,
#                                                      fastest, recommended
#                            distilled     — earlier distilled checkpoint
#                            dev           — full 22B dev model, highest
#                                            quality, slowest
#                          FP8 variants (Lightricks/LTX-2.3-fp8, ~30 GB each):
#                            fp8-distilled — original distilled in fp8
#                                            (no -1.1 fp8 variant exists
#                                            on HF as of 2026-05-13)
#                            fp8-dev       — full 22B dev model in fp8
#   --with-upscalers       Also download the spatial (x2) + temporal (x2)
#                          upscaler weights (~1.3 GB total) for two-stage
#                          rendering. The bundled bridge workflows are
#                          single-stage so this is optional; flip it on
#                          if you author your own two-stage workflow.
#   --update               Re-pull the LTXVideo node-pack git clone even
#                          if it already exists in custom_nodes/.
#   --dry-run              Print what would be downloaded; don't actually
#                          fetch anything.
#   -h, --help             Show this help.
#
# Required env (or huggingface-cli login):
#   HF_TOKEN               HuggingFace access token. Gemma 3 12B is
#                          gated — accept the model card terms at
#                          https://huggingface.co/google/gemma-3-12b-it-qat-q4_0-unquantized
#                          before running.
#
# Cross-reference:
#   docs/reference/video-comfyui-bridge.md  — what each file is for
#   docs/reference/image-comfyui-bridge.md  — the same pattern, for images
set -uo pipefail

# ─── Setup ───────────────────────────────────────────────────────────
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

# ANSI colors (suppressed when not a TTY)
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; RESET='\033[0m'; BOLD='\033[1m'
else
    GREEN=''; RED=''; YELLOW=''; CYAN=''; RESET=''; BOLD=''
fi

info()  { printf "${CYAN}==>${RESET} %s\n" "$*"; }
ok()    { printf "${GREEN}✓${RESET}   %s\n" "$*"; }
warn()  { printf "${YELLOW}!${RESET}   %s\n" "$*" >&2; }
die()   { printf "${RED}✗${RESET}   %s\n" "$*" >&2; exit 1; }

usage() { sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0; }

# ─── Args ────────────────────────────────────────────────────────────
BASEDIR="${COMFYUI_BASEDIR:-}"
VARIANT="distilled-1.1"
WITH_UPSCALERS=0
UPDATE=0
DRY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --basedir)         BASEDIR="$2"; shift 2 ;;
        --variant)         VARIANT="$2";  shift 2 ;;
        --with-upscalers)  WITH_UPSCALERS=1; shift ;;
        --update)          UPDATE=1; shift ;;
        --dry-run)         DRY=1; shift ;;
        -h|--help)         usage ;;
        *)                 die "Unknown argument: $1 (use --help)" ;;
    esac
done

[[ -z "$BASEDIR" ]] && die "Missing --basedir (or set COMFYUI_BASEDIR in env)."
[[ ! -d "$BASEDIR" ]] && die "Basedir does not exist: $BASEDIR"

# Variant → (repo, filename) routing. FP8 variants live in a separate
# HuggingFace repo (`Lightricks/LTX-2.3-fp8`), not in the main `LTX-2.3`
# repo; without explicit routing the fp8 download would 404. ~30 GB per
# fp8 checkpoint vs ~46 GB for bf16 — meaningful when GB10's 128 GB
# unified memory is also serving Gemma 4 + bge-m3 + maybe a ComfyUI
# spatial upscaler. The fp8 repo has no `-1.1` distilled refresh as of
# 2026-05-13 — operators wanting that combo must keep bf16 distilled-1.1.
case "$VARIANT" in
    dev|distilled|distilled-1.1)
        CKPT_REPO="Lightricks/LTX-2.3"
        CKPT_FILE="ltx-2.3-22b-${VARIANT}.safetensors"
        ;;
    fp8-dev)
        CKPT_REPO="Lightricks/LTX-2.3-fp8"
        CKPT_FILE="ltx-2.3-22b-dev-fp8.safetensors"
        ;;
    fp8-distilled)
        CKPT_REPO="Lightricks/LTX-2.3-fp8"
        CKPT_FILE="ltx-2.3-22b-distilled-fp8.safetensors"
        ;;
    *)
        die "Unknown --variant '$VARIANT'. Use one of: dev | distilled | distilled-1.1 | fp8-dev | fp8-distilled"
        ;;
esac

# ─── Path layout ─────────────────────────────────────────────────────
# Match the directory layout the Lightricks/ComfyUI-LTXVideo node pack
# expects (verified against its README on 2026-05-13). The Gemma encoder
# lives under a model-named subdir so the LTXAVTextEncoderLoader's
# dropdown picks it up.
CUSTOM_NODES_DIR="$BASEDIR/custom_nodes"
CKPT_DIR="$BASEDIR/models/checkpoints"
ENCODER_DIR="$BASEDIR/models/text_encoders/gemma-3-12b-it-qat-q4_0-unquantized"
UPSCALER_DIR="$BASEDIR/models/latent_upscale_models"

run() {
    if [[ $DRY -eq 1 ]]; then
        echo "    [dry-run] $*"
    else
        eval "$@"
    fi
}

# ─── Step 1: directory + write checks ────────────────────────────────
info "Validating ComfyUI basedir: $BASEDIR"
for d in models custom_nodes output; do
    [[ -d "$BASEDIR/$d" ]] || die "Missing $BASEDIR/$d — is this really a ComfyUI basedir?"
done
[[ -w "$BASEDIR" ]] || die "No write permission on $BASEDIR (run as the user that owns the install)."
ok "Basedir layout looks like ComfyUI"

# ─── Step 2: HuggingFace CLI ─────────────────────────────────────────
# Prefer the new `hf` CLI from huggingface_hub 1.x. Fall back to the
# legacy `huggingface-cli` for older installs (still works on
# huggingface_hub 0.x; deprecated in 1.x and prints a warning on every
# invocation in some intermediate releases). Tracked via the HF_BIN
# variable used by every download below.
if command -v hf >/dev/null 2>&1; then
    HF_BIN="hf"
    HF_VER=$(hf --version 2>&1 | head -1)
elif command -v huggingface-cli >/dev/null 2>&1; then
    HF_BIN="huggingface-cli"
    HF_VER=$(huggingface-cli --version 2>&1 | head -1)
else
    warn "Neither `hf` nor `huggingface-cli` found in PATH."
    cat >&2 <<'EOF'

  Install with:
      pip install --user 'huggingface_hub[hf_transfer]'
      pipx install 'huggingface_hub[hf_transfer]'        # alternative

  The hf_transfer extra is strongly recommended — it parallelizes the
  ~30 GB (fp8) or ~46 GB (bf16) checkpoint download. Without it expect
  3-5x slower transfers.

EOF
    die "Cannot proceed without HuggingFace CLI."
fi
ok "HuggingFace CLI: ${HF_BIN} (${HF_VER})"

# hf_transfer presence is best-effort detected; we still set the env
# var unconditionally so a future pip-installed hf_transfer kicks in.
export HF_HUB_ENABLE_HF_TRANSFER=1

# ─── Step 3: auth ────────────────────────────────────────────────────
# Gemma 3 12B is a gated repo. The CLI's `whoami` returns non-zero when
# not logged in, which is the gate we want. `hf auth whoami` is the
# new (huggingface_hub 1.x) form; `huggingface-cli whoami` is the
# legacy form. We probe both.
if [[ -z "${HF_TOKEN:-}" ]] && ! hf auth whoami >/dev/null 2>&1 && ! huggingface-cli whoami >/dev/null 2>&1; then
    warn "Not logged in to HuggingFace and HF_TOKEN is not set."
    cat >&2 <<'EOF'

  Two ways to authenticate:
      1. export HF_TOKEN=<your-token-from-https://huggingface.co/settings/tokens>
      2. huggingface-cli login    (interactive)

  AND accept the Gemma 3 12B model card terms at:
      https://huggingface.co/google/gemma-3-12b-it-qat-q4_0-unquantized

  Skipping this would mean the Gemma download fails after the 46 GB
  LTX checkpoint completed. Aborting now to save you the bandwidth.

EOF
    die "Auth missing — see hints above."
fi
ok "HuggingFace auth looks good"

# ─── Step 4: Node-pack git clone ─────────────────────────────────────
LTX_NODE_REPO="https://github.com/Lightricks/ComfyUI-LTXVideo.git"
LTX_NODE_DIR="$CUSTOM_NODES_DIR/ComfyUI-LTXVideo"

if [[ -d "$LTX_NODE_DIR/.git" ]]; then
    if [[ $UPDATE -eq 1 ]]; then
        info "Updating existing $LTX_NODE_DIR (--update passed)"
        run "git -C \"$LTX_NODE_DIR\" pull --ff-only"
    else
        ok "ComfyUI-LTXVideo already cloned (use --update to refresh)"
    fi
else
    info "Cloning ComfyUI-LTXVideo into $LTX_NODE_DIR"
    run "git clone \"$LTX_NODE_REPO\" \"$LTX_NODE_DIR\""
    # The node pack ships a requirements.txt — pip-install it into
    # ComfyUI's venv. We can't know the operator's venv from here, so
    # we just print the command; running it ourselves risks polluting
    # the wrong Python environment.
    if [[ -f "$LTX_NODE_DIR/requirements.txt" ]]; then
        warn "Run the following inside ComfyUI's venv before restarting:"
        echo "    pip install -r $LTX_NODE_DIR/requirements.txt"
    fi
fi
ok "Node pack in place: $LTX_NODE_DIR"

# ─── Step 5: model downloads ─────────────────────────────────────────
mkdir -p "$CKPT_DIR" "$ENCODER_DIR"

CKPT_SIZE_GB=$([[ "$VARIANT" == fp8-* ]] && echo "30" || echo "46")
info "Downloading main checkpoint from ${CKPT_REPO}: ${CKPT_FILE} (~${CKPT_SIZE_GB} GB)"
run "$HF_BIN download \"$CKPT_REPO\" \"$CKPT_FILE\" --local-dir \"$CKPT_DIR\""
ok "Main checkpoint ready"

info "Downloading Gemma 3 12B text encoder (~25 GB, gated repo)"
# The LTXAVTextEncoderLoader walks the directory at runtime and picks
# up every file, so we mirror the upstream repo wholesale. Trying to
# cherry-pick subfiles risks missing a tokenizer config or assistant
# template.
run "$HF_BIN download google/gemma-3-12b-it-qat-q4_0-unquantized --local-dir \"$ENCODER_DIR\""
ok "Text encoder ready"

if [[ $WITH_UPSCALERS -eq 1 ]]; then
    info "Downloading optional spatial + temporal upscalers (~1.3 GB)"
    mkdir -p "$UPSCALER_DIR"
    run "$HF_BIN download Lightricks/LTX-2.3 ltx-2.3-spatial-upscaler-x2-1.1.safetensors --local-dir \"$UPSCALER_DIR\""
    run "$HF_BIN download Lightricks/LTX-2.3 ltx-2.3-temporal-upscaler-x2-1.0.safetensors --local-dir \"$UPSCALER_DIR\""
    ok "Upscalers ready (operator must wire them into a two-stage workflow manually)"
else
    info "Skipping upscalers (use --with-upscalers to fetch the ~1.3 GB optional set)"
fi

# ─── Step 6: next steps ──────────────────────────────────────────────
# Variable must be assigned OUTSIDE the heredoc so the heredoc expands
# `${TOTAL_GB}` instead of printing the assignment literally.
TOTAL_GB=$([[ "$VARIANT" == fp8-* ]] && echo "~55 GB" || echo "~71 GB")
cat <<EOF

${BOLD}${GREEN}Done.${RESET} Total LTX-2.3 footprint on this machine: ${TOTAL_GB}.

${BOLD}Next steps:${RESET}

  1. Restart ComfyUI so the LTXVideo node pack loads.
     Verify with:
         curl -fsS \$COMFYUI_URL/object_info | grep -o '"EmptyLTXVLatentVideo"' | head -1

     Expect: ${GREEN}"EmptyLTXVLatentVideo"${RESET}. Empty output means the node pack
     did not register — check ComfyUI's startup log for import errors.

  2. Verify ComfyUI core is ${BOLD}>= 0.17.0${RESET}:
         curl -fsS \$COMFYUI_URL/system_stats | grep -o '"comfyui_version":"[^"]*"'

     LTX-2.3 day-0 nodes landed in 0.16.x, but the official reference
     workflows from Comfy-Org/workflow_templates use \`ComfyMathExpression\`
     from 0.17.0+ — loading them on an older install warns
     "Some nodes may not work correctly" and the upscaler chain breaks.
     0.16.x works only for hand-built single-stage workflows.

  3. In ${REPO_ROOT}/.env, enable the bridge tool surface:
         LTX_VIDEO_ENABLED=1
         # Pick defaults — see .env.example "LTX video" section for the
         # full set with explanations.
         LTX_VIDEO_DEFAULT_LENGTH_FRAMES=96
         LTX_VIDEO_DEFAULT_FPS=24
         LTX_VIDEO_DEFAULT_AUDIO=on

  4. Re-run the bridge image build + recreate the openclaw-image-comfyui
     container so the new generate_video tool registers:
         docker compose -f openclaw-image-comfyui/docker-compose.yml \\
             --env-file .env --profile image-gen up -d --build

  5. Re-apply the OpenClaw patcher so the AGENTS.md cheatsheet for the
     discord-friend agent gains the video workflow picker:
         docker compose up -d --force-recreate openclaw-config-init \\
             openclaw-gateway openclaw-cli

  6. Smoke test from the host:
         docker exec \${PROJ:-dgx-}openclaw-cli openclaw agent --agent main \\
             --message "Use comfyui_image__generate_video to make a 4-second clip of a red panda." \\
             --thinking minimal --json --timeout 900

     Cold first run: 3-10 minutes (ComfyUI loads ~71 GB into VRAM
     for the first time). Subsequent runs reuse the cache.

If anything goes sideways, ${BOLD}docs/reference/video-comfyui-bridge.md${RESET}
has the troubleshooting matrix.
EOF
