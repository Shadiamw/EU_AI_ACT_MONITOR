#!/bin/sh
# Single Render free web dyno: worker in background + API in foreground.
set -eu
python -m app.runtime prepare
python -m app.runtime worker &
exec python -m app.runtime api
