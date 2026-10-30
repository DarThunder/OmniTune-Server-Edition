# OmniTune

**Personal and offline music server.** FastAPI backend to build, organize, and play your own music library from your own infrastructure.

> Copyright (C) 2026-Present DarThunder
> License: [GNU AGPL-3.0-or-later](LICENSE)

---

## ⚠️ Important legal notice (read before use)

OmniTune is a **neutral software tool**. It does not include, distribute, or host any copyrighted content.

1. **User responsibility.** You are solely responsible for how you use this software and for the content you download, store, or play with it. You must ensure you have the right to do so under the laws of your jurisdiction (copyright, private copying, etc.) and under the terms of service of any third-party platform.
2. **Third-party terms of service.** Downloading content from platforms such as YouTube may violate their Terms of Service and/or applicable intellectual property laws. The author **does not** encourage or authorize such use. Use OmniTune only with content you own, that is in the public domain, that is licensed in a way that permits it, or for which you have express authorization.
3. **No affiliation.** OmniTune is an independent project. It is **not affiliated with, sponsored by, or endorsed by** YouTube, Google, Deezer, Apple, the MetaBrainz Foundation, Cloudflare, or any other company. All trademarks and names mentioned belong to their respective owners and are used for identification purposes only.
4. **No warranty.** The software is provided **"AS IS"**, without warranties of any kind, express or implied, including those of merchantability, fitness for a particular purpose, and non-infringement. To the maximum extent permitted by law, the author and contributors **will not be liable** for claims, damages, data loss, account or IP bans, penalties, or any other liability arising from the use of the software (see sections 15 and 16 of the AGPL-3.0).
5. **Personal use.** It is designed for personal/household use and small trusted groups. It is not intended to provide a public content distribution service.
6. **Metadata services.** Titles, artists, and cover art are looked up through public third-party APIs (Deezer, iTunes Search, MusicBrainz, and the Cover Art Archive). You are responsible for complying with their terms of use and rate limits. Cover art remains the property of its respective owners.
7. **Not legal advice.** This notice is informational and does not constitute legal advice. If you have doubts about your situation, consult a lawyer.

### Content removal (Notice & Takedown)

If you are a rights holder and believe that a public instance of OmniTune infringes your rights, note that **the software author does not operate or control instances deployed by third parties**. You must direct your request to the operator of the specific instance. For matters relating to the source code of this repository, write to: `xdarthunder@gmail.com`.

---

## Features

- **Music discovery** with pagination and Redis caching (`/discover`).
- **Background downloading** with progress tracking via `task_id`.
- **Automatic metadata matching** (title, artist, cover art) through Deezer, iTunes, and MusicBrainz / Cover Art Archive, with no API keys and results cached in Redis.
- **Local full-text search** (SQLite FTS5 + `bm25`) over your library.
- **Streaming** with `Range` support (seek) and `.opus` file download.
- **Favorites** and **playlists** (custom order, custom covers).
- Profile picture and covers processed to **WebP**.
- **JWT authentication** delegated to **OSIRIS** (validation via JWKS) with automatic user provisioning.
- **Audit log** of user actions.
- Decoupled blob storage (_holder_ service).

## Architecture

```
Client ──► OmniTune (FastAPI :8083)
              ├── OSIRIS (JWKS)      → authentication (JWT)
              ├── SQLite (+FTS5)     → metadata, users, playlists, audit
              ├── Redis              → search, metadata cache, and task progress
              ├── Holder (blobs)     → audio and thumbnails
              └── Metadata APIs      → Deezer, iTunes, MusicBrainz + Cover Art Archive
```

## Requirements

- [Nix](https://nixos.org/) with flakes enabled (the dev shell provides Python 3.12+, `ffmpeg`, `yt-dlp`, and all Python dependencies). The flake currently targets `x86_64-linux` only.
- Redis, running separately (the dev shell does not start it)
- An accessible **OSIRIS** server (JWT issuer and JWKS endpoint)
- A **holder** blob service at `http://127.0.0.1:8080`
- _(Optional)_ `warp-cli` (Cloudflare WARP) if you want to route outbound traffic through a tunnel; OmniTune only reports its status at startup

## Installation

```bash
git clone https://github.com/DarThunder/OmniTune-Server-Edition omnitune
cd omnitune
```

The repository ships a `flake.nix` and a pinned `flake.lock`, so the environment is reproducible:

```bash
nix develop
```

**Without Nix.** There is no `requirements.txt`; install the Python packages listed in `flake.nix` (plus `ffmpeg` and `yt-dlp` on your `PATH`) with your own tooling. Python 3.12+ is required. This path is not officially supported.

## Configuration

OmniTune reads its configuration from `~/.config/omnitune/.env`:

```env
OSIRIS_ISSUER=https://your-osiris-server
REDIS_PASSWORD=a_strong_password
# Optional: contact sent in the User-Agent to MusicBrainz (they ask for one)
OMNITUNE_CONTACT=you@example.com
```

| Variable           | Required | Description                                                 |
| ------------------ | -------- | ----------------------------------------------------------- |
| `OSIRIS_ISSUER`    | Yes      | Issuer (`iss`) expected in JWTs                             |
| `REDIS_PASSWORD`   | Yes      | Redis password                                              |
| `OMNITUNE_CONTACT` | No       | Contact (email/URL) sent to MusicBrainz in the `User-Agent` |

Current fixed values (in `core/configs.py`): port `8083`, JWT audience `omnitune`, JWKS at `http://127.0.0.1:8090/.well-known/jwks.json`, holder at `http://127.0.0.1:8080`, database at `~/.config/omnitune/omnitune.db`, and profiles at `~/omnitune/profiles`.

> **Never upload your `.env` to the repository** or share your credentials.

Place a `default.webp` file in `~/omnitune/profiles/` as the default profile picture.

## Running

```bash
nix develop
run                    # shortcut for: python src/main.py
```

The server listens on `0.0.0.0:8083`. Interactive documentation at `http://localhost:8083/docs`.

> ⚠️ Listening on `0.0.0.0` exposes the service to your network. Put it behind a reverse proxy with HTTPS and/or a firewall if you expose it outside your local network.

## Main endpoints

| Method | Route                       | Auth | Description                           |
| ------ | --------------------------- | ---- | ------------------------------------- |
| GET    | `/health`                   | No   | Service status                        |
| GET    | `/discover`                 | Yes  | Search external music (paginated)     |
| GET    | `/search`                   | No   | Full-text search in the local library |
| GET    | `/feed`                     | Yes  | Library feed                          |
| GET    | `/music/{hash}`             | Yes  | Audio streaming                       |
| GET    | `/music/{hash}/download`    | Yes  | Download `.opus`                      |
| POST   | `/music`                    | Yes  | Start background download             |
| GET    | `/music/progress/{task_id}` | Yes  | Download progress                     |
| GET    | `/thumbnail/{hash}`         | No   | Thumbnail                             |
| `*`    | `/users/me/profile`, `/pfp` | Yes  | Profile and picture                   |
| `*`    | `/users/me/favorites`       | Yes  | Favorites                             |
| `*`    | `/users/me/playlists`       | Yes  | Playlists, items, order, and covers   |

> Note: `/debug/tasks` is a debug endpoint **without authentication**. Remove it or protect it before deploying to production.

## Privacy and data

OmniTune stores locally: OSIRIS user identifier, username, biography, favorites, playlists, profile/cover images, and an **audit log** (actions such as searches, downloads, exports, and profile changes, along with their details). If you operate an instance for other people, **you are responsible** for informing them about this processing and for complying with applicable data protection regulations (for example, LFPDPPP in Mexico or GDPR in the EU).

## License

This program is free software: you can redistribute it and/or modify it under the terms of the **GNU Affero General Public License** as published by the Free Software Foundation, either version 3 of the License or (at your option) any later version.

This program is distributed in the hope that it will be useful, but **WITHOUT ANY WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU AGPL for more details. You should have received a copy in the [`LICENSE`](./LICENSE) file; if not, see <https://www.gnu.org/licenses/agpl-3.0.html>.

### Obligation to offer the source code (AGPL §13)

If you modify OmniTune and allow other people to interact with your version over a network, **you must offer them the corresponding source code** of your modified version, under the same license.

## Contributions

Contributions are welcome via _pull requests_. By contributing, you agree that your contribution is licensed under **AGPL-3.0-or-later** and you declare that you have the right to do so. Contributions that include copyrighted content without authorization, or credentials or personal data, will not be accepted.

## Security

If you find a vulnerability, please report it privately to `xdarthunder@gmail.com` instead of opening a public _issue_.

---

© 2026-Present DarThunder. All rights reserved except those granted by AGPL-3.0-or-later.
