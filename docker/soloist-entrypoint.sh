#!/bin/sh
set -eu

# Keep PulseAudio's runtime private and share only its audio socket with the bot.
export PULSE_RUNTIME_PATH=/tmp/soloist-pulse
export PULSE_SINK=soloist
mkdir -p "$PULSE_RUNTIME_PATH"
chmod 700 "$PULSE_RUNTIME_PATH"
pulseaudio --daemonize=yes --exit-idle-time=-1 --disable-shm=yes --log-target=stderr \
  --load='module-null-sink sink_name=soloist rate=48000 channels=2' \
  --load='module-native-protocol-unix socket=/run/soloist-audio/native'
test -S /run/soloist-audio/native
pactl set-default-sink soloist
exec "$@"
