#!/usr/bin/env bash
# Install every mod in this repo into a Hermes home (default ~/.hermes).
#
#   ./install.sh                 link + enable all plugins, desktop plugins and skills
#   ./install.sh --community     ...and install the pinned community plugins in hermes-pack.yaml
#   ./install.sh --extras        ...and the optional ones in hermes-pack-extras.yaml (Home Assistant, Spotify)
#   ./install.sh --copy          copy instead of symlink (freeze this version; `git pull` won't update it)
#   ./install.sh --tools         only install what this repo's pre-commit hook needs (gitleaks), then exit
#
# Symlinks mean `git pull` in this clone updates the mods in place. Re-running is safe.
# Core (Hermes source) changes are NOT installed by this script; see core/README.md.
set -euo pipefail

M="$(cd "$(dirname "$0")" && pwd)"
H="${HERMES_HOME:-$HOME/.hermes}"
mode=link community=0 extras=0 tools_only=0
for a in "$@"; do
  case "$a" in
    --tools) tools_only=1 ;;
    --copy) mode=copy ;;
    --community) community=1 ;;
    --extras) extras=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

# gitleaks: the pre-commit hook refuses commits without it (this repo is public). Same version as the GitHub check.
GITLEAKS_VERSION=8.28.0
install_gitleaks() {
  if command -v gitleaks >/dev/null 2>&1; then echo "gitleaks: $(command -v gitleaks)"; return 0; fi
  if [ "$(uname -s)" = Darwin ]; then
    command -v brew >/dev/null && brew install gitleaks && return 0
    echo "gitleaks: install Homebrew, then: brew install gitleaks" >&2; return 1
  fi
  local arch; case "$(uname -m)" in x86_64|amd64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) arch="" ;; esac
  [ -n "$arch" ] || { echo "gitleaks: no release build for $(uname -m); install it by hand" >&2; return 1; }
  mkdir -p "$HOME/.local/bin"
  curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${arch}.tar.gz" \
    | tar -xz -C "$HOME/.local/bin" gitleaks
  echo "gitleaks: installed $HOME/.local/bin/gitleaks ($("$HOME/.local/bin/gitleaks" version))"
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "  add ~/.local/bin to PATH so git hooks find it" >&2 ;; esac
}
git -C "$M" config core.hooksPath .githooks 2>/dev/null || true
if [ "$tools_only" = 1 ]; then install_gitleaks; exit $?; fi
install_gitleaks || echo "  (commits to $M will be refused until gitleaks is installed)"

command -v hermes >/dev/null || { echo "hermes not on PATH" >&2; exit 1; }
mkdir -p "$H/plugins" "$H/desktop-plugins"

place() { # src dst
  local src="$1" dst="$2"
  if [ -L "$dst" ]; then rm "$dst"
  elif [ -e "$dst" ]; then echo "  skip $(basename "$dst"): $dst exists and is not a link (move it away to install)"; return 1
  fi
  if [ "$mode" = copy ]; then cp -R "$src" "$dst"; else ln -s "$src" "$dst"; fi
}

echo "agent plugins -> $H/plugins"
for d in "$M"/plugins/*/; do
  n="$(basename "$d")"
  if grep -q '^retired:' "$d/plugin.yaml" 2>/dev/null; then
    echo "  $n: retired ($(sed -n 's/^retired: *//p' "$d/plugin.yaml")), not installed"; continue
  fi
  if place "${d%/}" "$H/plugins/$n"; then
    echo "  $n"; hermes plugins enable "$n" >/dev/null 2>&1 || echo "  (enable $n by hand: hermes plugins enable $n)"
  fi
done

echo "desktop plugins -> $H/desktop-plugins"
for d in "$M"/desktop-plugins/*/; do
  n="$(basename "$d")"
  place "${d%/}" "$H/desktop-plugins/$n" && echo "  $n"
done

echo "skills -> skills.external_dirs"
cur="$(hermes config get skills.external_dirs 2>/dev/null || true)"
if printf '%s' "$cur" | grep -q "hermes-mods/skills\|$M/skills"; then
  echo "  already set"
elif [ -z "$(printf '%s' "$cur" | tr -d '[:space:][]')" ] || printf '%s' "$cur" | grep -qi "none\|null"; then
  hermes config set skills.external_dirs "[\"$M/skills\"]" >/dev/null && echo "  set to $M/skills"
else
  echo "  skills.external_dirs already has other entries; add $M/skills to it yourself:"; echo "$cur" | sed 's/^/    /'
fi

[ "$community" = 1 ] && { echo "community plugins (hermes-pack.yaml)"; hermes plugins pack install "$M/hermes-pack.yaml"; }
[ "$extras" = 1 ] && { echo "extras (hermes-pack-extras.yaml)"; hermes plugins pack install "$M/hermes-pack-extras.yaml"; }

echo
echo "Done. Restart the gateway / Desktop backend so backend plugins load (desktop plugins hot-reload)."
echo "Shortcuts and core changes: $M/shortcuts.md and $M/core/README.md"
