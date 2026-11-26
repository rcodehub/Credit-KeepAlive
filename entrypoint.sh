#!/bin/bash
# Runs the ntfy server and the Credit-KeepAlive app side by side in one container.
# If either one stops, the container stops and Docker restarts it.
mkdir -p /data/ntfy

# Lets iPhones receive instant push. Only poll requests go to ntfy.sh; message content stays here.
# The phone server address is set in the app's Settings page, so nothing needs to be set here.
export NTFY_UPSTREAM_BASE_URL="https://ntfy.sh"

trap 'kill $(jobs -p) 2>/dev/null' TERM INT

# The ntfy account tool only works once the server has created its auth file, which happens on
# first start. So: start the server, wait for that file, create the admin, phone and publisher
# logins, then restart the server so it loads them. Nobody can read or publish on the phone
# server without a login (NTFY_AUTH_DEFAULT_ACCESS=deny-all).
ntfy serve &
first_start=$!
for _ in $(seq 1 30); do
  [ -s /data/ntfy/auth.db ] && break
  sleep 1
done
sleep 2
if ! python3 /app/ntfy_accounts.py; then
  echo "The phone server logins could not be set up. Phone alerts won't work until the container restarts cleanly." >&2
fi
kill "$first_start" 2>/dev/null
wait "$first_start" 2>/dev/null

ntfy serve &
gunicorn --workers 1 --threads 4 --bind 0.0.0.0:8080 app:app &

wait -n
status=$?
kill $(jobs -p) 2>/dev/null
exit $status
