# Roadmap

AshyTV is a player and organizer for playlists the user provides. It does not
ship, link to or recommend any content.

## Done

- GTK4 / libadwaita shell with adaptive sidebar, dark/light themes, ten accent
  colors and translucent panels
- M3U / M3U8 import (file or URL) with robust parsing and series grouping
- Xtream Codes through `player_api.php` (catalog, account status, episodes on
  demand), passwords in the system keyring
- Background import, SQLite storage with migrations, stable favorites and
  watch history, continue watching and resume
- XMLTV program guide with disk cache, time grid and per-channel schedule
- libmpv player: zapping, seeking, tracks, subtitles, aspect ratio, speed,
  picture-in-picture, recording, loading/error states
- Parental controls (hide adult categories, PIN)
- Translations (29 languages), test suite, fictional demo server
- Quality variants grouped into one channel, automatic quality (adaptive)
- TiviMate-style channel list over the video, number tuning, last channel
- Timeshift (pause / rewind live TV, "Go live")
- Guide merged into Channels (List | Guide), icons-only sidebar
- Keyboard, TV-remote and game-controller navigation (libmanette)
- Hide / reorder channel groups, programme reminders

## Next

- [ ] Flatpak manifest and Flathub submission
- [ ] Movie details through `get_vod_info` (plot, cast, duration)
- [ ] Catch-up for providers that keep an archive
- [ ] Multiple guide sources merged (several playlists at once)
- [ ] Big-screen ("10-foot") mode with larger fonts
- [ ] CI (lint, tests, translation checks)
