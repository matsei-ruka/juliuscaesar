#!/usr/bin/env bash
# install.sh — install JuliusCaesar binaries.
#
# Creates a dedicated venv at ~/.local/share/juliuscaesar/venv, installs
# Python deps, and symlinks the binaries into ~/.local/bin/.
#
# Idempotent: safe to re-run. Removes any stale symlinks before linking.
#
# Usage:
#   ./install.sh
#
# Uninstall:
#   rm ~/.local/bin/jc-memory && rm -rf ~/.local/share/juliuscaesar

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SHARE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/juliuscaesar"
BIN_DIR="$HOME/.local/bin"
VENV_DIR="$SHARE_DIR/venv"
DEPS=(pyyaml python-dotenv dashscope requests)
BINARIES=(jc jc-memory jc-heartbeat jc-voice jc-watchdog jc-init jc-doctor)

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

# --- Checks ------------------------------------------------------------------

command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }
mkdir -p "$SHARE_DIR" "$BIN_DIR"

case ":${PATH:-}:" in
    *":$BIN_DIR:"*) ;;
    *) echo "warning: $BIN_DIR is not on your PATH. Add it to your shell config."
       echo "  For bash:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
       echo "  For zsh:   echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
       ;;
esac

# --- venv --------------------------------------------------------------------

if [[ ! -d "$VENV_DIR" ]]; then
    log "Creating venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

log "Installing Python deps: ${DEPS[*]}"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet --upgrade "${DEPS[@]}"

# --- binary shims ------------------------------------------------------------
#
# We write shim scripts to ~/.local/bin/ that invoke the binaries using the
# venv's python, with the repo's lib/ on PYTHONPATH. This way the installed
# binaries track the repo's HEAD — `git pull` updates them without reinstall.

for bin in "${BINARIES[@]}"; do
    source_path="$HERE/bin/$bin"
    shim="$BIN_DIR/$bin"

    if [[ ! -f "$source_path" ]]; then
        log "skip $bin — source missing at $source_path"
        continue
    fi

    # Detect shebang: python scripts get the venv wrapper; everything else
    # (bash, etc.) is invoked directly.
    first_line="$(head -n1 "$source_path")"
    if [[ "$first_line" =~ python ]]; then
        log "Installing $bin (python) → $shim"
        cat > "$shim" <<EOF
#!/usr/bin/env bash
# Auto-generated shim for JuliusCaesar.
# Source: $source_path
# venv:   $VENV_DIR
exec "$VENV_DIR/bin/python" "$source_path" "\$@"
EOF
    else
        log "Installing $bin (native) → $shim"
        cat > "$shim" <<EOF
#!/usr/bin/env bash
# Auto-generated shim for JuliusCaesar.
# Source: $source_path
exec "$source_path" "\$@"
EOF
    fi
    chmod +x "$shim"
done

log "Done."
log "Verify:  jc-memory --help"
