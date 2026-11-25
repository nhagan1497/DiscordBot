FROM python:3.13-slim-trixie AS base
WORKDIR /app
RUN useradd --uid 1000 --create-home app \
    && install -d -o app -g app /app/data \
    && pip install --no-cache-dir aiohttp
COPY src/common.py ./src/

FROM base AS bot
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 700 -o app -g app /run/soloist-audio \
    && pip install --no-cache-dir 'discord.py[voice]' 'davey==0.1.7' Jinja2
COPY src/bot.py src/admin.py ./src/
COPY web/ ./web/
USER app
EXPOSE 3000
CMD ["python", "-u", "src/bot.py"]

# The Soloist binary requires glibc 2.38 or newer and libatomic.
FROM base AS soloist
RUN apt-get update && apt-get install -y --no-install-recommends pulseaudio pulseaudio-utils ca-certificates curl libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && release_dir="$(mktemp -d)" \
    && curl -fsSL --retry 3 -o "$release_dir/soloist.tar.gz" https://soloist-builds.spotifycdn.com/soloist_release_x86_64.tar.gz \
    && tar -xzf "$release_dir/soloist.tar.gz" -C "$release_dir" soloist THIRD_PARTY_LICENSES.txt \
    && install -m 755 "$release_dir/soloist" /usr/local/bin/soloist \
    && install -D -m 644 "$release_dir/THIRD_PARTY_LICENSES.txt" /usr/local/share/doc/spotify-soloist/THIRD_PARTY_LICENSES.txt \
    && rm -f "$release_dir/soloist.tar.gz" "$release_dir/soloist" "$release_dir/THIRD_PARTY_LICENSES.txt" \
    && rmdir "$release_dir" \
    && /usr/local/bin/soloist --version \
    && install -d -m 700 -o app -g app /run/soloist-audio
COPY --chmod=755 docker/soloist-entrypoint.sh /usr/local/bin/soloist-entrypoint
COPY src/player.py ./src/
USER app
EXPOSE 3001
ENTRYPOINT ["/usr/local/bin/soloist-entrypoint"]
CMD ["python", "-u", "src/player.py"]

# The current Spotify desktop package needs glibc 2.39 or newer.
FROM base AS pairing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg novnc websockify xvfb x11vnc x11-utils openbox dbus-x11 \
    chromium xdg-utils fonts-liberation libnss3 \
    && curl -fsSLo /tmp/spotify.asc https://download.spotify.com/debian/pubkey_5384CE82BA52C83A.asc \
    && gpg --dearmor --output /usr/share/keyrings/spotify.gpg /tmp/spotify.asc \
    && printf '%s\n' 'deb [signed-by=/usr/share/keyrings/spotify.gpg] https://repository.spotify.com stable non-free' > /etc/apt/sources.list.d/spotify.list \
    && apt-get update && apt-get install -y --no-install-recommends spotify-client \
    && rm -rf /var/lib/apt/lists/* /tmp/spotify.asc \
    && install -d -m 1777 /tmp/.X11-unix
COPY --chmod=755 docker/pairing-desktop.sh docker/pairing-browser.sh /usr/local/bin/
COPY src/pairing.py ./src/
USER app
CMD ["python", "-u", "src/pairing.py"]
