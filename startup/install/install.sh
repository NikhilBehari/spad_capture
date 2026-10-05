#!/usr/bin/env bash
# Sets up spad_capture on macOS and Linux.
set -euo pipefail

ENV_NAME=spad_capture
CAMERA=1
FLASH=
YES=0
PREFIX_ONLY=0
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FORGE="$HOME/miniforge3"
FORGE_URL=https://github.com/conda-forge/miniforge/releases/latest/download

step() { printf '\n\033[1;36m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: startup/install/install.sh [options]

  -n, --name NAME     conda env name (default: $ENV_NAME)
      --no-camera     skip the Realsense binding (pyrealsense2)
      --flash tmf|st  flash the dev board after installing
  -y, --yes           don't prompt; install Miniforge if no conda is found
      --prefix        print the env's path (empty if not installed), then exit
  -h, --help          show this help
EOF
}

while [ $# -gt 0 ]; do
  case $1 in
    -n|--name)   [ $# -ge 2 ] || die "--name needs a value"; ENV_NAME=$2; shift ;;
    --no-camera) CAMERA=0 ;;
    --flash)     [ $# -ge 2 ] || die "--flash needs a value: tmf or st"
                 case $2 in tmf|st) FLASH=$2 ;; *) die "--flash takes tmf or st, not '$2'" ;; esac
                 shift ;;
    -y|--yes)    YES=1 ;;
    --prefix)    PREFIX_ONLY=1 ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage >&2; die "unknown option: $1" ;;
  esac
  shift
done

case "$(uname -s)" in
  Darwin) PLATFORM=macos ;;
  Linux)  PLATFORM=linux ;;
  MINGW*|MSYS*|CYGWIN*) die "on Windows: powershell -ExecutionPolicy Bypass -File startup\\install\\install.ps1" ;;
  *)      die "unsupported platform $(uname -s)" ;;
esac

# On Apple silicon, prefer a native conda: an Intel one builds emulated envs.
find_conda() {
  local candidate base fallback=
  for candidate in "${CONDA_EXE:-}" "$(type -P conda || true)" \
      "$HOME"/{miniforge3,mambaforge,miniconda3,anaconda3}/bin/conda \
      /opt/{miniforge3,miniconda3,anaconda3,conda}/bin/conda \
      /opt/homebrew/Caskroom/miniforge/base/bin/conda; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    [ "$PLATFORM" = macos ] || { CONDA=$candidate; return 0; }
    base=$("$candidate" info --base 2>/dev/null) || continue
    if [ "$("$base/bin/python" -c 'import platform; print(platform.machine())' 2>/dev/null)" = "$(uname -m)" ]; then
      CONDA=$candidate
      return 0
    fi
    : "${fallback:=$candidate}"
  done
  [ -n "$fallback" ] || return 1
  CONDA=$fallback
  info "note: $CONDA is not a native $(uname -m) build; its envs will run emulated"
}

install_miniforge() {
  [ -e "$FORGE" ] && die "no working conda found, and $FORGE already exists. Remove it and re-run."
  if [ "$YES" -eq 0 ]; then
    [ -t 0 ] || die "no conda found. Re-run with --yes to install Miniforge into $FORGE."
    read -r -p "    No conda found. Install Miniforge into $FORGE? [y/N] " reply
    case $reply in y|Y|yes|YES) ;;
      *) die "conda is required. Install Miniforge (https://github.com/conda-forge/miniforge) and re-run." ;;
    esac
  fi
  local installer
  installer=$(mktemp)
  trap 'rm -f "$installer"' RETURN
  info "downloading Miniforge3-$(uname -s)-$(uname -m).sh"
  curl -fsSL "$FORGE_URL/Miniforge3-$(uname -s)-$(uname -m).sh" -o "$installer" \
    || die "could not download Miniforge; check the network and re-run."
  bash "$installer" -b -p "$FORGE" || die "Miniforge installer failed."
  CONDA="$FORGE/bin/conda"
  info "installed. For new shells: $CONDA init \"\$(basename \"\$SHELL\")\""
}

env_prefix() { "$CONDA" env list | awk -v n="$ENV_NAME" '$1 == n { print $NF }'; }

ensure_env() {
  PREFIX=$(env_prefix)
  if [ -n "$PREFIX" ]; then
    info "reusing existing env '$ENV_NAME'"
  else
    "$CONDA" env create -n "$ENV_NAME" -f "$REPO/environment.yml" \
      || die "could not create env '$ENV_NAME'. If an earlier attempt left it half-made: $CONDA env remove -n $ENV_NAME"
    PREFIX=$(env_prefix)
  fi
  PY="$PREFIX/bin/python"
  [ -x "$PY" ] || die "env '$ENV_NAME' has no python."
  info "env at $PREFIX"
}

if [ "$PREFIX_ONLY" -eq 1 ]; then
  find_conda >/dev/null && env_prefix
  exit 0
fi

step "Locating conda"
find_conda || install_miniforge
info "using $CONDA ($("$CONDA" --version))"

step "Creating the '$ENV_NAME' environment"
ensure_env

step "Installing spad_capture"
TARGET=.
[ "$CAMERA" -eq 1 ] && [ "$PLATFORM" = linux ] && TARGET='.[rgb]'
(cd "$REPO" && "$PY" -m pip install --disable-pip-version-check -e "$TARGET") \
  || die "pip install of spad_capture failed (see output above)."

if [ "$CAMERA" -eq 1 ] && [ "$PLATFORM" = macos ]; then
  step "Installing the Realsense binding (conda-forge)"
  "$CONDA" install -y -p "$PREFIX" --override-channels -c conda-forge pyrealsense2 \
    || die "could not install pyrealsense2 from conda-forge. Re-run with --no-camera to skip it."
fi

step "Checking the install"
"$PREFIX/bin/spad" --help >/dev/null || die "'spad' was installed but does not run."
info "spad CLI ok"
if [ "$CAMERA" -eq 1 ]; then
  "$PY" -c 'import pyrealsense2' 2>/dev/null \
    || die "pyrealsense2 does not import. Re-run, or use --no-camera to skip it."
  info "pyrealsense2 ok"
fi

if [ -n "$FLASH" ]; then
  step "Flashing the $FLASH board"
  "$PREFIX/bin/spad" "$FLASH" flash \
    || die "flashing failed; the env is installed, so fix the board and run: spad $FLASH flash"
fi

step "Done"
info "Activate the env, then capture:"
info ""
info "    conda activate $ENV_NAME"
info "    spad tmf capture        # or: spad st capture"
[ -n "$FLASH" ] || info "    spad tmf flash          # first time with a board: flash it"
[ "$CAMERA" -eq 1 ] && info "    spad camera check       # Realsense status"
exit 0
