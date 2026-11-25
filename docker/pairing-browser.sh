#!/bin/sh
exec chromium --no-sandbox --no-first-run --disable-sync --mute-audio "$@"
