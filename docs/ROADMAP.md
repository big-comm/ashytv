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

## Next

- [ ] Flatpak manifest and Flathub submission
- [ ] Reorder / hide channel groups
- [ ] Movie details through `get_vod_info` (plot, cast, duration)
- [ ] Catch-up / timeshift for providers that support archives
- [ ] Multiple guide sources merged (several playlists at once)
- [ ] Remote-control friendly navigation (D-pad focus, big-screen mode)
- [ ] CI (lint, tests, translation checks)
