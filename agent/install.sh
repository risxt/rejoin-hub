#!/data/data/com.termux/files/usr/bin/bash
# Rejoin Hub agent installer / updater for Termux.
#   First install:  curl -fsSL <raw url>/install.sh | bash -s -- <pairing code> [phone name]
#   Update:         curl -fsSL <raw url>/install.sh | bash
set -euo pipefail

REPO="${RH_REPO:-risxt/rejoin-hub}"
BRANCH="${RH_BRANCH:-main}"
HOME_DIR="$HOME/.rejoinhub"
APP_DIR="$HOME_DIR/app"
CODE="${1:-}"
NAME="${2:-}"

say()  { printf '\033[1;32m›\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*"; exit 1; }

[ -n "${PREFIX:-}" ] && [ -d "$PREFIX" ] || die "Run this inside Termux (the F-Droid build)."

say "Rejoin Hub agent installer"

curl_ok() { curl --version >/dev/null 2>&1; }
if ! command -v python3 >/dev/null 2>&1 || ! curl_ok || ! command -v tar >/dev/null 2>&1; then
  say "Updating Termux and installing python (one time, a few minutes)…"
  export DEBIAN_FRONTEND=noninteractive
  # A FULL upgrade first: installing python on an old Termux upgrades libcurl without openssl and breaks curl
  # ("cannot locate symbol SSL_set_quic_tls_early_data_enabled").
  # apt, not pkg: pkg's mirror check itself needs a working curl.
  apt-get update -y >/dev/null 2>&1 || true
  apt-get full-upgrade -y -o Dpkg::Options::=--force-confnew >/dev/null 2>&1 || true
  apt-get install -y -o Dpkg::Options::=--force-confnew python curl tar openssl >/dev/null 2>&1 \
    || die "Installing python failed. Run: apt update && apt full-upgrade -y   then paste the install command again."
fi
curl_ok || die "curl is broken (half-updated Termux). Run: apt update && apt full-upgrade -y   then paste the install command again."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Python 3.10+ needed. Run: pkg upgrade python"

say "Downloading the agent…"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" -o "$TMP/src.tgz" || die "Download failed. Check the internet connection."
tar -xzf "$TMP/src.tgz" -C "$TMP"
SRC="$(find "$TMP" -maxdepth 3 -type d -name rejoinhub -path '*agent/rejoinhub' | head -1)"
[ -n "$SRC" ] && [ -f "$SRC/__main__.py" ] || die "The download looks wrong (no agent inside)."

mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/rejoinhub.new"
cp -r "$SRC" "$APP_DIR/rejoinhub.new"
rm -rf "$APP_DIR/rejoinhub"
mv "$APP_DIR/rejoinhub.new" "$APP_DIR/rejoinhub"

# keep-alive loop: restarts the agent if it ever exits, until `rh stop`
cat > "$HOME_DIR/keepalive.sh" <<EOF
#!$PREFIX/bin/bash
cd "$APP_DIR"
echo \$\$ > "$HOME_DIR/keepalive.pid"
while [ ! -f "$HOME_DIR/stopped" ]; do
  PYTHONPATH="$APP_DIR" python3 -m rejoinhub run >/dev/null 2>&1
  sleep 5
done
EOF
chmod 700 "$HOME_DIR/keepalive.sh"

# `rh` command
cat > "$PREFIX/bin/rh" <<EOF
#!$PREFIX/bin/bash
if [ "\${1:-}" = "start" ]; then
  rm -f "$HOME_DIR/stopped"
  command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true
  if [ -f "$HOME_DIR/keepalive.pid" ] && kill -0 "\$(cat "$HOME_DIR/keepalive.pid")" 2>/dev/null; then echo "Agent already running."; exit 0; fi
  nohup setsid "$HOME_DIR/keepalive.sh" >/dev/null 2>&1 &
  echo "Agent started in the background."
  exit 0
fi
if [ "\${1:-}" = "log" ]; then exec tail -n 60 -f "$HOME_DIR/agent.log"; fi
PYTHONPATH="$APP_DIR" exec python3 -m rejoinhub "\$@"
EOF
chmod 755 "$PREFIX/bin/rh"

# start on boot when the Termux:Boot add-on is installed
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/rejoinhub.sh" <<EOF
#!$PREFIX/bin/bash
sleep 20
"$PREFIX/bin/rh" start
EOF
chmod 700 "$HOME/.termux/boot/rejoinhub.sh"

if [ -n "$CODE" ]; then
  say "Pairing this phone (allow root when asked)…"
  if [ -n "$NAME" ]; then rh pair "$CODE" --name "$NAME"; else rh pair "$CODE"; fi
elif [ ! -f "$HOME_DIR/agent.json" ]; then
  die "Not paired. Copy the install command (with its code) from the dashboard: Settings → Connection."
fi

# restart with the new files: if the keep-alive loop runs, stopping the agent makes it reload within 5 s
rm -f "$HOME_DIR/stopped"
if [ -f "$HOME_DIR/keepalive.pid" ] && kill -0 "$(cat "$HOME_DIR/keepalive.pid")" 2>/dev/null; then
  [ -f "$HOME_DIR/agent.pid" ] && kill "$(cat "$HOME_DIR/agent.pid")" 2>/dev/null || true
else
  rh start >/dev/null
fi
say "Done. This phone shows up in the dashboard within 30 seconds."
say "Commands: rh status · rh log · rh doctor · rh stop · rh start"
