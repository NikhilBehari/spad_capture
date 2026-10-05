#!/usr/bin/env bash
# One-command setup for spad_capture on macOS and Linux (Windows: install.ps1).
#
#   ./install.sh                  # env + package + Realsense binding
#   ./install.sh --no-camera      # skip the Realsense binding
#   ./install.sh --flash tmf      # also flash the Arduino + TMF8828 (board plugged in)
#
# Safe to re-run: an existing env is reused and the package reinstalled into it.
set -euo pipefail

ENV_NAME="spad_capture"
WITH_CAMERA=1
FLASH=""
ASSUME_YES=0
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINIFORGE_DIR="$HOME/miniforge3"
MINIFORGE_URL="https://github.com/conda-forge/miniforge/releases/latest/download"

usage() {
  cat <<EOF
Usage: ./install.sh [options]

  -n, --name NAME     conda env name (default: $ENV_NAME)
      --no-camera     skip the Realsense binding (pyrealsense2)
      --flash tmf|st  flash the dev board after installing (board must be plugged in)
  -y, --yes           don't prompt; install Miniforge if no conda is found
  -h, --help          show this help
EOF
}

step() { printf '\n\033[1;36m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      -n|--name)   [ $# -ge 2 ] || die "$1 needs a value"; ENV_NAME="$2"; shift ;;
      --no-camera) WITH_CAMERA=0 ;;
      --flash)
        [ $# -ge 2 ] || die "--flash needs a value: tmf or st"
        case "$2" in tmf|st) FLASH="$2" ;; *) die "--flash takes tmf or st, not '$2'" ;; esac
        shift ;;
      -y|--yes)    ASSUME_YES=1 ;;
      -h|--help)   usage; exit 0 ;;
      *)           usage >&2; die "unknown option: $1" ;;
    esac
    shift
  done
}

detect_platform() {
  case "$(uname -s)" in
    Darwin) PLATFORM="macos" ;;
    Linux)  PLATFORM="linux" ;;
    MINGW*|MSYS*|CYGWIN*)
            die "on Windows, run the PowerShell installer instead: powershell -ExecutionPolicy Bypass -File install.ps1" ;;
    *)      die "unsupported platform $(uname -s); see README for macOS, Linux and Windows" ;;
  esac
}

conda_candidates() {
  printf '%s\n' \
    "${CONDA_EXE:-}" \
    "$(type -P conda 2>/dev/null || true)" \
    "$HOME/miniforge3/bin/conda" \
    "$HOME/mambaforge/bin/conda" \
    "$HOME/miniconda3/bin/conda" \
    "$HOME/anaconda3/bin/conda" \
    "$HOME/opt/anaconda3/bin/conda" \
    "/opt/homebrew/Caskroom/miniforge/base/bin/conda" \
    "/opt/miniconda3/bin/conda" \
    "/opt/anaconda3/bin/conda" \
    "/opt/conda/bin/conda"
}

# conda's own interpreter. `conda` on PATH may be the condabin shim, so ask for
# the base prefix rather than looking beside the binary.
conda_python() {
  local base
  base="$("$1" info --base 2>/dev/null)" || return 1
  [ -x "$base/bin/python" ] || return 1
  printf '%s\n' "$base/bin/python"
}

# Whether this conda builds envs for the machine's own CPU. An Intel conda on
# Apple silicon works, but every env it makes runs under Rosetta.
conda_is_native() {
  local py arch
  py="$(conda_python "$1")" || return 1
  arch="$("$py" -c 'import platform; print(platform.machine())' 2>/dev/null)" || return 1
  [ "$arch" = "$(uname -m)" ]
}

# The conda binary itself, not the shell function `conda init` defines, so this
# works from a plain non-interactive shell. A native conda wins over the first one found.
find_conda() {
  local candidate fallback=""
  while IFS= read -r candidate; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    "$candidate" --version >/dev/null 2>&1 || continue
    if conda_is_native "$candidate"; then
      CONDA="$candidate"
      return 0
    fi
    [ -n "$fallback" ] || fallback="$candidate"
  done < <(conda_candidates)
  [ -n "$fallback" ] || return 1
  CONDA="$fallback"
  info "note: $CONDA is not a native $(uname -m) build; its envs will run under emulation"
}

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  [ -t 0 ] || die "$1 Re-run with --yes to allow it."
  local reply
  read -r -p "    $1 [y/N] " reply
  case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

install_miniforge() {
  [ ! -e "$MINIFORGE_DIR" ] || die "no working conda found, and $MINIFORGE_DIR already exists. Fix or remove it, then re-run."
  confirm "No conda found. Install Miniforge into $MINIFORGE_DIR?" \
    || die "conda is required. Install Miniforge (https://github.com/conda-forge/miniforge) and re-run."
  local installer
  installer="$(mktemp -t miniforge.XXXXXX)"
  trap 'rm -f "$installer"' EXIT
  info "downloading Miniforge3-$(uname -s)-$(uname -m).sh"
  curl -fsSL "$MINIFORGE_URL/Miniforge3-$(uname -s)-$(uname -m).sh" -o "$installer" \
    || die "could not download Miniforge; check the network and re-run."
  bash "$installer" -b -p "$MINIFORGE_DIR" || die "Miniforge installer failed."
  CONDA="$MINIFORGE_DIR/bin/conda"
  info "installed. To use conda in new shells: $CONDA init \"\$(basename \"\$SHELL\")\""
}

# Prefix of the env named $ENV_NAME, or nothing. Read from conda's JSON listing
# rather than `conda run`, whose behaviour varies across conda versions.
env_prefix() {
  local py
  py="$(conda_python "$CONDA")" || die "could not find the python inside $CONDA's install."
  "$CONDA" env list --json | "$py" -c '
import json, os, sys
envs = json.load(sys.stdin)["envs"]
print(next((p for p in envs if os.path.basename(p) == sys.argv[1]), ""))' "$ENV_NAME"
}

ensure_env() {
  ENV_PREFIX="$(env_prefix)"
  if [ -n "$ENV_PREFIX" ]; then
    info "reusing existing env '$ENV_NAME'"
  else
    "$CONDA" env create -n "$ENV_NAME" -f "$REPO_DIR/environment.yml" \
      || die "could not create env '$ENV_NAME' from environment.yml. If an earlier attempt left it half-made, remove it ($CONDA env remove -n $ENV_NAME) and re-run."
    ENV_PREFIX="$(env_prefix)"
    [ -n "$ENV_PREFIX" ] || die "env '$ENV_NAME' was created but conda does not list it."
  fi
  ENV_PY="$ENV_PREFIX/bin/python"
  [ -x "$ENV_PY" ] || die "env '$ENV_NAME' exists but has no python."
  info "env at $ENV_PREFIX"
}

# The Realsense binding is the one platform split: PyPI has no macOS build, and
# the conda-forge build cannot reach the camera on Linux.
install_package() {
  local target="."
  [ "$WITH_CAMERA" -eq 1 ] && [ "$PLATFORM" = "linux" ] && target=".[rgb]"
  (cd "$REPO_DIR" && "$ENV_PY" -m pip install --disable-pip-version-check -e "$target") \
    || die "pip install of spad_capture failed (see output above)."
}

install_camera_macos() {
  "$CONDA" install -y -p "$ENV_PREFIX" --override-channels -c conda-forge pyrealsense2 \
    || die "could not install pyrealsense2 from conda-forge. Re-run with --no-camera to skip it."
}

verify() {
  "$ENV_PREFIX/bin/spad" --help >/dev/null || die "'spad' was installed but does not run."
  info "spad CLI ok"
  if [ "$WITH_CAMERA" -eq 1 ]; then
    "$ENV_PY" -c 'import pyrealsense2' 2>/dev/null \
      || die "pyrealsense2 does not import. Re-run, or use --no-camera to skip it."
    info "pyrealsense2 ok"
  fi
}

main() {
  parse_args "$@"
  detect_platform

  step "Locating conda"
  find_conda || install_miniforge
  info "using $CONDA ($("$CONDA" --version))"

  step "Creating the '$ENV_NAME' environment"
  ensure_env

  step "Installing spad_capture"
  install_package

  if [ "$WITH_CAMERA" -eq 1 ] && [ "$PLATFORM" = "macos" ]; then
    step "Installing the Realsense binding (conda-forge)"
    install_camera_macos
  fi

  step "Checking the install"
  verify

  if [ -n "$FLASH" ]; then
    step "Flashing the $FLASH board"
    "$ENV_PREFIX/bin/spad" "$FLASH" flash || die "flashing failed; the env is installed, so fix the board and run: spad $FLASH flash"
  fi

  step "Done"
  info "Activate the env, then capture:"
  info ""
  info "    conda activate $ENV_NAME"
  info "    spad tmf capture        # or: spad st capture"
  [ -n "$FLASH" ] || info "    spad tmf flash          # first time with a board: flash it"
  [ "$WITH_CAMERA" -eq 1 ] && info "    spad camera check       # Realsense status"
  return 0
}

main "$@"
