#!/bin/sh
# Set up the tennis data on the shared server (ssh alias hoh). Run as root from /opt/rngn-tennis.
# It only creates its own user and rngn-tennis-* units, and starts nothing: the server records live
# matches and serves other projects, so everything here runs at low priority and on idle disk.
#
#   systemctl enable --now rngn-tennis-winline.timer rngn-tennis-parse.timer
#   systemctl enable --now rngn-tennis-collect.service   # only once no other collector is running
#   systemctl status 'rngn-tennis-*'; journalctl -u rngn-tennis-collect -n 20
set -eu

ROOT=/opt/rngn-tennis
PYTHON=/usr/bin/python3.14   # the system python3 here is 3.10; other units on this server use 3.14 too

id rngn-tennis >/dev/null 2>&1 || useradd --system --home-dir "$ROOT" --shell /usr/sbin/nologin rngn-tennis
mkdir -p "$ROOT/local-data/tennis/raw" "$ROOT/local-data/tennis/accounts"
chown -R rngn-tennis:rngn-tennis "$ROOT/local-data"
chmod 700 "$ROOT/local-data/tennis/accounts"

common() {
  cat <<EOF
User=rngn-tennis
WorkingDirectory=$ROOT
Nice=15
IOSchedulingClass=idle
Environment=PYTHONUNBUFFERED=1
EOF
}

{
  cat <<EOF
[Unit]
Description=Tennis: collect the WTA API into the local raw store
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON tools/tennis/wta_collect.py
# 2 means the API said stop (403 or 429): never retried by itself. Network failures are.
Restart=on-failure
RestartSec=300
RestartPreventExitStatus=2
EOF
  common
  cat <<EOF

[Install]
WantedBy=multi-user.target
EOF
} > /etc/systemd/system/rngn-tennis-collect.service

{
  cat <<EOF
[Unit]
Description=Tennis: record the Winline line
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$PYTHON tools/tennis/winline_snapshot.py
TimeoutStartSec=300
EOF
  common
} > /etc/systemd/system/rngn-tennis-winline.service

cat > /etc/systemd/system/rngn-tennis-winline.timer <<EOF
[Unit]
Description=Tennis: record the Winline line every 10 minutes

[Timer]
OnCalendar=*:03/10
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

{
  cat <<EOF
[Unit]
Description=Tennis: parse new WTA responses and refresh the coverage report

[Service]
Type=oneshot
ExecStart=$PYTHON tools/tennis/wta_parse.py
ExecStart=$PYTHON tools/tennis/wta_lines.py
ExecStart=$PYTHON tools/tennis/wta_report.py --out local-data/tennis/coverage.md
TimeoutStartSec=3000
EOF
  common
} > /etc/systemd/system/rngn-tennis-parse.service

cat > /etc/systemd/system/rngn-tennis-parse.timer <<EOF
[Unit]
Description=Tennis: parse new WTA responses every hour

[Timer]
OnCalendar=hourly
AccuracySec=5min

[Install]
WantedBy=timers.target
EOF

{
  cat <<EOF
[Unit]
Description=Tennis: the Matchday page, read-only, behind /matchday of the Больше address
After=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON tools/tennis/matchday_server.py --port 8107
Restart=always
RestartSec=5
User=rngn-tennis
WorkingDirectory=$ROOT
Nice=5
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=$ROOT
# accounts only: the page itself never writes to the data
ReadWritePaths=$ROOT/local-data/tennis/accounts
# the letters go through the same mailbox as the EPL site's
LoadCredential=smtp.env:/opt/hoh-epl/secrets/smtp.env

[Install]
WantedBy=multi-user.target
EOF
} > /etc/systemd/system/rngn-tennis-matchday.service

systemctl daemon-reload
echo installed
