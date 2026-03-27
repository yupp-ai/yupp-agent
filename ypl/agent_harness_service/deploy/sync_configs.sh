#!/bin/bash
#
# Sync agent and shared config files from the repo to /data/.
#
# Pulls /opt/yupp-agent first (unless --no-pull), then copies changed .md and
# config.json files to the live /data/ directories used by the service.
# Only files that differ are copied; unchanged files are skipped.
#
# Run as: sudo -u ahs bash sync_configs.sh
#
# Usage:
#   sync_configs.sh              # pull repo + sync all configs
#   sync_configs.sh --no-pull    # skip git pull (repo already up to date)
#   sync_configs.sh --shared     # sync shared/ only
#   sync_configs.sh --agents     # sync agent_configs/ only
#   sync_configs.sh --dry-run    # show what would be copied
#
set -euo pipefail

SERVICE_REPO="/opt/yupp-agent"
DEPLOY_DIR="${SERVICE_REPO}/ypl/agent_harness_service/deploy"
DATA_DIR="/data"
SERVICE_USER="ahs"
LOG_FILE="/data/session_logs/sync_configs.log"

SYNC_SHARED=true
SYNC_AGENTS=true
DO_PULL=true
DRY_RUN=false

TARGET_REF=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-pull) DO_PULL=false; shift ;;
        --shared)  SYNC_SHARED=true; SYNC_AGENTS=false; shift ;;
        --agents)  SYNC_AGENTS=true; SYNC_SHARED=false; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --ref)     TARGET_REF="$2"; shift 2 ;;
        *)
            echo "Usage: sudo -u ahs bash sync_configs.sh [--no-pull] [--shared|--agents] [--dry-run] [--ref <branch|tag|sha>]"
            exit 1
            ;;
    esac
done

# Log helper — writes to both stdout and the log file.
# In dry-run mode, only write to stdout.
log() {
    echo "$1"
    if [ "$DRY_RUN" = false ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
    fi
}

CHANGED=0

sync_file() {
    local src="$1" dst="$2"
    if [ ! -f "$src" ]; then
        return
    fi
    if [ "$DRY_RUN" = true ]; then
        if [ ! -f "$dst" ] || ! diff -q "$src" "$dst" > /dev/null 2>&1; then
            echo "  [would copy] $src -> $dst"
        else
            echo "  [unchanged]  $dst"
        fi
    else
        if [ ! -f "$dst" ]; then
            mkdir -p "$(dirname "$dst")"
            cp "$src" "$dst"
            log "  [new]     $dst"
            CHANGED=$((CHANGED + 1))
        elif ! diff -q "$src" "$dst" > /dev/null 2>&1; then
            cp "$src" "$dst"
            log "  [updated] $dst"
            CHANGED=$((CHANGED + 1))
        fi
    fi
}

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

log "--- sync_configs start ---"

# --- Step 1: Pull service repo ---
if [ "$DO_PULL" = true ]; then
    cd "$SERVICE_REPO"
    BEFORE=$(git rev-parse HEAD)

    # Discard any local changes (e.g. from a previous failed deploy)
    git reset --hard HEAD 2>&1
    git clean -fd 2>&1

    if [ -n "$TARGET_REF" ]; then
        # Deploy a specific branch/tag/sha: fetch and checkout
        log "Fetching and checking out ${TARGET_REF}..."
        git fetch origin 2>&1 || {
            log "ERROR: git fetch failed"
            exit 1
        }
        git checkout --force "$TARGET_REF" 2>&1 || {
            # If it's a remote branch, try origin/<ref>
            git checkout --force "origin/${TARGET_REF}" 2>&1 || {
                log "ERROR: Failed to checkout ${TARGET_REF}"
                exit 1
            }
        }
        # If on a branch (not detached HEAD), pull latest
        if git symbolic-ref -q HEAD > /dev/null 2>&1; then
            git pull --ff-only origin "$TARGET_REF" 2>&1 || {
                log "WARNING: git pull --ff-only failed for ${TARGET_REF}, continuing with checked-out version"
            }
        fi
    else
        # Default: pull main
        log "Pulling main..."
        git checkout main 2>&1 || {
            log "ERROR: Failed to checkout main"
            exit 1
        }
        git pull --ff-only origin main 2>&1 || {
            log "WARNING: git pull failed (non-fast-forward?), continuing with current HEAD"
        }
    fi

    AFTER=$(git rev-parse HEAD)
    if [ "$BEFORE" = "$AFTER" ]; then
        log "Service repo already up to date at ${AFTER:0:7}."
    else
        log "Service repo updated: ${BEFORE:0:7} -> ${AFTER:0:7}"
    fi
fi

# --- Step 2: Copy changed configs to /data/ ---

# Shared identity files (*.md)
if [ "$SYNC_SHARED" = true ]; then
    log "Syncing shared/ -> ${DATA_DIR}/shared/"
    for f in "${DEPLOY_DIR}/shared/"*.md; do
        [ -f "$f" ] || continue
        sync_file "$f" "${DATA_DIR}/shared/$(basename "$f")"
    done
    # Shared subdirectories (e.g. raw_executor/)
    for subdir in "${DEPLOY_DIR}/shared/"*/; do
        [ -d "$subdir" ] || continue
        subdir_name=$(basename "$subdir")
        for f in "${subdir}"*.md; do
            [ -f "$f" ] || continue
            sync_file "$f" "${DATA_DIR}/shared/${subdir_name}/$(basename "$f")"
        done
    done
    if [ "$DRY_RUN" = false ] && [ "$(id -un)" != "$SERVICE_USER" ]; then
        chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}/shared/"
    fi
fi

# Agent configs (config.json + *.md)
if [ "$SYNC_AGENTS" = true ]; then
    log "Syncing agent_configs/ -> ${DATA_DIR}/agents/"
    for agent_dir in "${DEPLOY_DIR}/agent_configs/"*/; do
        [ -d "$agent_dir" ] || continue
        agent_name=$(basename "$agent_dir")

        sync_file "${agent_dir}config.json" "${DATA_DIR}/agents/${agent_name}/config.json"

        for f in "${agent_dir}"*.md; do
            [ -f "$f" ] || continue
            sync_file "$f" "${DATA_DIR}/agents/${agent_name}/$(basename "$f")"
        done

        # Migration: remove legacy core/ directory now that ROLE.md lives at agent level.
        legacy_core="${DATA_DIR}/agents/${agent_name}/core"
        if [ -d "$legacy_core" ] && [ "$DRY_RUN" = false ]; then
            log "  Removing legacy core/ dir: ${legacy_core}"
            rm -rf "$legacy_core"
            CHANGED=$((CHANGED + 1))
        elif [ -d "$legacy_core" ] && [ "$DRY_RUN" = true ]; then
            log "  [DRY RUN] Would remove legacy core/ dir: ${legacy_core}"
        fi
    done
    if [ "$DRY_RUN" = false ] && [ "$(id -un)" != "$SERVICE_USER" ]; then
        chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}/agents/"
    fi
fi

# --- Step 2b: Sync AHS-only hooks into the repo's .claude/hooks/ ---
# These hooks are deployed only on the VM (into /data/repos/yupp-agent/.claude/hooks/),
# not checked into the repo's .claude/ directory, so local dev is unaffected.
HOOKS_SRC="${DEPLOY_DIR}/shared/hooks"
HOOKS_DST="${DATA_DIR}/repos/yupp-agent/.claude/hooks"
SETTINGS_FILE="${DATA_DIR}/repos/yupp-agent/.claude/settings.json"

if [ "$SYNC_SHARED" = true ] && [ -d "$HOOKS_SRC" ]; then
    log "Syncing AHS hooks -> ${HOOKS_DST}/"
    mkdir -p "$HOOKS_DST"
    for f in "${HOOKS_SRC}"/*.sh; do
        [ -f "$f" ] || continue
        sync_file "$f" "${HOOKS_DST}/$(basename "$f")"
        if [ "$DRY_RUN" = false ]; then
            chmod +x "${HOOKS_DST}/$(basename "$f")"
        fi
    done

    # Ensure block-gh-pr-create hook is registered in the repo's settings.json.
    # This is idempotent — only patches if the hook entry is missing.
    HOOK_CMD_NEEDLE="block-gh-pr-create.sh"
    if [ -f "$SETTINGS_FILE" ] && ! grep -q "$HOOK_CMD_NEEDLE" "$SETTINGS_FILE" 2>/dev/null; then
        if command -v jq &> /dev/null && [ "$DRY_RUN" = false ]; then
            HOOK_ENTRY='{"type":"command","command":"\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/block-gh-pr-create.sh"}'
            # Append to the existing Bash matcher's hooks array
            jq --argjson hook "$HOOK_ENTRY" '
              .hooks.PreToolUse |= map(
                if .matcher == "Bash"
                then .hooks += [$hook]
                else .
                end
              )
            ' "$SETTINGS_FILE" > "${SETTINGS_FILE}.tmp" && mv "${SETTINGS_FILE}.tmp" "$SETTINGS_FILE"
            log "  [patched] settings.json: added block-gh-pr-create hook"
            CHANGED=$((CHANGED + 1))
        elif [ "$DRY_RUN" = true ]; then
            echo "  [would patch] settings.json: add block-gh-pr-create hook"
        fi
    fi

    if [ "$DRY_RUN" = false ] && [ "$(id -un)" != "$SERVICE_USER" ]; then
        chown -R "${SERVICE_USER}:${SERVICE_USER}" "$HOOKS_DST"
    fi
fi

# --- Step 3: Symlink skills into ~/.codex/skills/ ---
SKILLS_SRC="${SERVICE_REPO}/.agents/skills"
SKILLS_DST="/home/${SERVICE_USER}/.codex/skills"

if [ -d "$SKILLS_SRC" ]; then
    log "Syncing skill symlinks -> ${SKILLS_DST}/"
    mkdir -p "$SKILLS_DST"
    for skill_dir in "${SKILLS_SRC}"/*/; do
        [ -d "$skill_dir" ] || continue
        skill_name=$(basename "$skill_dir")
        link_path="${SKILLS_DST}/${skill_name}"
        if [ "$DRY_RUN" = true ]; then
            if [ -L "$link_path" ]; then
                current_target=$(readlink "$link_path")
                if [ "$current_target" = "$skill_dir" ] || [ "$current_target" = "${skill_dir%/}" ]; then
                    echo "  [unchanged]  ${link_path} -> ${skill_dir}"
                else
                    echo "  [would update] ${link_path} -> ${skill_dir} (was ${current_target})"
                fi
            else
                echo "  [would link] ${link_path} -> ${skill_dir}"
            fi
        else
            if [ -L "$link_path" ]; then
                current_target=$(readlink "$link_path")
                if [ "$current_target" = "$skill_dir" ] || [ "$current_target" = "${skill_dir%/}" ]; then
                    continue
                fi
                rm "$link_path"
                ln -s "$skill_dir" "$link_path"
                log "  [updated] ${link_path} -> ${skill_dir}"
                CHANGED=$((CHANGED + 1))
            elif [ -e "$link_path" ]; then
                log "  [skipped] ${link_path} exists and is not a symlink"
            else
                ln -s "$skill_dir" "$link_path"
                log "  [linked]  ${link_path} -> ${skill_dir}"
                CHANGED=$((CHANGED + 1))
            fi
        fi
    done
    # Remove stale symlinks pointing to skills that no longer exist in the repo.
    # Use /* (no trailing slash) so broken symlinks are included — */  only matches
    # entries that currently resolve as directories and would silently skip dangling links.
    for link in "${SKILLS_DST}"/*; do
        [ -L "$link" ] || continue
        link_target=$(readlink "$link")
        if [[ "$link_target" == "${SKILLS_SRC}"* ]] && [ ! -d "$link_target" ]; then
            if [ "$DRY_RUN" = true ]; then
                echo "  [would remove stale] ${link} -> ${link_target}"
            else
                rm "$link"
                log "  [removed stale] ${link} -> ${link_target}"
                CHANGED=$((CHANGED + 1))
            fi
        fi
    done
    if [ "$DRY_RUN" = false ] && [ "$(id -un)" != "$SERVICE_USER" ]; then
        chown -h -R "${SERVICE_USER}:${SERVICE_USER}" "$SKILLS_DST"
    fi
fi

if [ "$DRY_RUN" = false ]; then
    log "Done. ${CHANGED} file(s) updated."
else
    echo "Done (dry run)."
fi
