# Spotify Discord Bot

One Docker Compose stack runs the Discord bot, the Spotify Soloist player, and the pairing desktop. Discord commands, catalog search, and audio playback use the live integrations.

## Start locally

Use Docker Desktop in Linux-container mode on Windows. Create `.env` from `.env.example` and fill in the settings below.

| Setting | Value |
| --- | --- |
| `DISCORD_TOKEN` | The bot token from Discord. Install it in your one server and grant **View Channel**, **Connect**, and **Speak** in the voice channel. The bot detects that server automatically. |
| `SOLOIST_API_KEY` | The Soloist API key from Spotify for Developers. The player uses this key to start Soloist. |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | Credentials from a Spotify Web API app for title-and-artist search and metadata. Soloist handles playback of a known Spotify URI and does not expose catalog search. |

Docker downloads and installs the [official Linux x86-64 Soloist release](https://developer.spotify.com/documentation/soloist/reference/downloads-and-updates) when it builds the player image locally. Create `runtime/soloist-data` if needed for saved Connect sessions. On Linux, the player runs as UID 1000 and needs read/write access to that directory.

```powershell
docker compose up --build
```

The bot registers commands in the configured Discord server. Administration is at `http://127.0.0.1:3000`. The player and pairing desktop are reachable only on the Compose network.

The player exposes its PulseAudio monitor on a Unix socket in the `soloist-audio` volume. Python’s `discord.py` voice client reads the monitor through its built-in FFmpeg PCM source, encodes with native libopus, and sends the audio to Discord. The player’s HTTP interface carries pairing and playback controls.

## Pair an account and play music

1. In Discord, run `/music status` to create your profile. Open Administration. The profile displays your Discord user ID.
2. Click **Pair** next to your profile. Sign into Spotify in the embedded desktop and select `Discord Music <id>` from the device picker. If Spotify opens a sign-in browser, complete sign-in and reopen the desktop app there. When the pairing status says **Spotify paired**, return to Administration. The temporary desktop closes and Soloist keeps the saved session.
3. Join Discord voice and run `/music host`. This selects `Discord Music <id>` in Spotify and resumes paused playback or starts the first queued track if the player is idle. If Spotify has nothing to play yet, request a track with `/music play` or `/music search`.
4. Add a song from Spotify and another from Discord. Check both in Spotify's queue and with `/music queue`. Administration reads the same playback state from Soloist. To check Jam, start one from Spotify on the Soloist device and verify a Discord request also appears for the other participants.

| Command | Action |
| --- | --- |
| `/music status` | Creates your profile and reports its pairing status. |
| `/music host` | Starts a session in your voice channel using your paired profile. |
| `/music search query:<title or artist>` | Shows up to five tracks. Select one to play it or add it to the queue. |
| `/music play query:<title and artist or link>` | Plays or queues the first match or the track in a Spotify link. |
| `/music now` | Shows what is playing. |
| `/music queue [page]` | Opens a paginated menu with queue sources, playback controls, and a shortcut to Spotify’s queue. |
| `/music pause`, `/music resume` | Pause or resume Spotify playback in your voice channel. |
| `/music skip` | Advances the queue from your voice channel. |
| `/music stop` | Ends the Discord voice session and stops Soloist. Host only. |

Members in the active voice channel can request tracks, pause, resume, or skip. If the host leaves or moves channels, the session ends. A different member can start a new session with their own paired account.

Administration’s **Playback** section provides the same controls and shows which upcoming tracks were queued manually, came from the album or playlist, or were added by Autoplay. To clear added tracks, open **Clear queue in Spotify** and use the host’s Spotify account. Soloist’s API has no queue deletion command. Select **Refresh** to read the latest playback state.

## Local troubleshooting

```powershell
docker compose logs --tail=100 bot player pairing
```

If commands don’t appear, check the bot logs for registration and verify the installation and permissions in Discord. If the desktop doesn’t open, check `pairing` and `bot`. If the device picker is empty, check `player` for pairing errors and verify `SOLOIST_API_KEY` in `.env`. If the profile pairs but audio is silent, check `player` and `bot` immediately after `/music play`.

## Deploy on Proxmox

Use the same Git checkout, Compose file, and `.env` keys in the bot LXC. The admin port listens on IPv4 port `3000` so Caddy can reach it.

```bash
install -d -o 1000 -g 1000 runtime/soloist-data
docker compose up -d --build
```

Reserve `192.168.1.126` for the bot LXC and restrict access to TCP port `3000` to the Caddy LXC. Point Caddy at `192.168.1.126:3000`. Caddy handles authentication for the entire admin site, including `/api/*` and the WebSocket connection under `/pairing-desktop/*`. The player and pairing services stay on the Compose network.
