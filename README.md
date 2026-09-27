# X Post Management

Desktop application to manage and schedule your X (Twitter) posts locally and securely.

> ### ✅ Up to date for 2026
>
> X rebuilt its sign-in and scheduling screens during 2026, and older builds can
> no longer sign in. **The current release works against the live site**, posts
> images and video, and no longer opens a blank window on Windows.
>
> [![Latest release](https://img.shields.io/github/v/release/LoicMeyerFrance/X-Post-Management?label=latest&color=2563eb)](https://github.com/LoicMeyerFrance/X-Post-Management/releases/latest)
>
> [**Download the latest release →**](https://github.com/LoicMeyerFrance/X-Post-Management/releases/latest)

## Features

- Create and publish posts with text, images or video
- Schedule posts in advance (uses X native scheduling)
- Manage your drafts
- View publication history
- Delete tweets directly from the app
- Calendar view of your posts
- **My Profile** page: followers/following stats, bio, growth chart, follower variations
- Auto-detect Chrome installation
- Persistent preferences (language, theme) across sessions
- Bilingual interface (English / French)
- Light and dark theme

## Download

Download the latest `X Post Management.exe` from the releases.

## First Use

On first launch, a **Setup Wizard** guides you through 4 steps:

1. **Welcome** — Choose your language (EN/FR) and click **Get started**
2. **Credentials** — Enter your X username (without @) and password. These are saved
   locally in a `.env` file — nothing is sent to any server besides X itself.
3. **Sign in** — Click **Connect to X**. A browser window opens and the app signs in
   with the credentials you just saved. If X asks for a code or a verification,
   answer it in that window and the sign-in finishes on its own. The session is
   then kept, so your credentials are not typed again.
4. **Import Profile** — Click **Import Profile** to fetch your profile picture,
   display name, bio and follower counts from X.

Once complete, you're ready to compose, schedule and manage your posts.

> **Tip:** leave everything else alone. The app finds Chrome by itself and keeps
> its own browser session — see *Advanced* below if you really need to point it
> somewhere specific.

## Configuration Options

Configuration is stored in a `.env` file (created automatically by the Setup Wizard). You can also edit it manually or via **Settings > Configuration** in the app.

| Key | Description | Default |
|-----|-------------|---------|
| `X_USERNAME` | Your X username (without @) | |
| `X_PASSWORD` | Your X password | |
| `HEADLESS` | `true` for invisible browser, `false` to see it | `true` |
| `CHECK_INTERVAL_SECONDS` | Check frequency for scheduled posts (5-3600) | `15` |
| `MAX_RETRIES` | Number of retries on failure (0-10) | `1` |
| `PORT` | Port of the local server (falls back automatically if busy) | `5000` |
| `XPM_HOME` | Where `data/`, `logs/` and `.env` live | next to the executable |

Values are validated when saved: a bad username, an out-of-range interval or a
non-numeric retry count is rejected with a clear message instead of failing
later. Quotes, spaces and backslashes in your password are preserved exactly.

### Advanced: forcing a specific Chrome

**You should not need these.** The app finds Chrome on its own and keeps its own
browser session. They exist only to point it at a particular installation, and
they are behind *Advanced options* in **Settings > Configuration**.

| Key | What it does | Left empty (recommended) |
|-----|--------------|--------------------------|
| `CHROME_PATH` | Use this Chrome executable | the app detects your installed Chrome, and falls back to the browser shipped with it |
| `CHROME_PROFILE_DIR` | Reuse this Chrome profile | the app keeps its own session in `data/chrome_profile` |

If you do set `CHROME_PROFILE_DIR` to one of your real Chrome profiles, **that
Chrome has to stay closed** while the app runs — a profile cannot be open in two
browsers at once.

## Troubleshooting

- **Sign-in failed**: use **Settings → Connect to X**. Set the browser to **Visible**
  so you can answer whatever X is asking for; the session is saved afterwards.
- **"X has temporarily limited sign-in"**: X throttles repeated attempts. Wait before
  trying again — the app will not retry on its own, because retrying makes it worse.
- **Blank white window**: the app needs the
  [Edge WebView2 runtime](https://developer.microsoft.com/microsoft-edge/webview2/).
  Without it the app now opens in your browser instead and says so in the log.
- **Post failed**: make sure the image is under 5 MB.
- **"X does not offer the requested minute"**: X's schedule dialog only lists certain
  minutes. Pick a time it offers — the app refuses to schedule at a time you did not
  choose rather than rounding silently.
- **Video rejected or stuck**: X takes MP4 and MOV (H.264 video, AAC audio). A
  standard account is limited to 512 MB and 2 min 20 s; Premium goes up to 16 GB
  and 4 hours. The app checks the size and the duration against *your* account
  before uploading anything, then waits while X transcodes, which can take
  several minutes for a large clip.

## Security

Everything (credentials, posts, images, session cookies) stays on your computer.
The only network destination is X itself - the interface bundles its own fonts
and assets, so the app makes no third-party requests and works offline.

The app serves its interface from a small web server on `127.0.0.1`. Because any
web page you visit can also reach that address, the server:

- **rejects cross-origin requests** (`Origin`, `Referer` and `Sec-Fetch-Site` are
  checked), so no website can publish, read or delete your posts behind your back;
- **rejects requests with a foreign `Host` header**, which blocks DNS rebinding;
- **sends a strict Content-Security-Policy** and never sets a wildcard CORS header;
- **never returns your password** to the interface - it is masked as `********`;
- **keeps your X password in the OS credential store** (Windows Credential
  Manager, macOS Keychain) rather than in a file, and never puts it in the
  environment the browser inherits;
- **stores `.env` and the saved session with owner-only permissions** where the
  filesystem supports it.

Uploads are checked by content, not just by file extension, capped at 5 MB, and
stored under generated names inside `data/uploads`.

One thing to keep in mind: the automation has to type your password into X, so
the app must be able to read it back in clear. The credential store means it is
no longer sitting in a file that can be copied, synced to a backup or caught in
a screenshot — but any program running under your own user account can still
ask for it. It is protection against accidental exposure, not against someone
who already has your session.

If no credential store is available (some Linux setups), the app says so in the
log and falls back to keeping the password in `.env`.

An existing install is upgraded on first launch: the password is moved out of
`.env` automatically, and nothing is asked of you.

## Files Created

The app creates these files next to the executable:

```
data/
  posts.db            - SQLite database (posts + followers history)
  profile_info.json   - Cached profile information
  profile_picture.jpg - Profile picture
  preferences.json    - UI preferences (language, theme)
  session.json        - Whether the last sign-in succeeded (no credentials)
  chrome_profile/     - Browser profile, when CHROME_PROFILE_DIR is empty
  uploads/            - Uploaded images
logs/
  app.log             - Activity logs
.env                  - Settings (no password: that lives in the OS credential store)
```

## Development

**Requirements:**
- Python 3.10+
- Node.js 18+

**Setup:**
```bash
pip install -r requirements.txt
playwright install chromium
cd ui && npm install && npm run build && cd ..
python server/app.py
```

**Tests and checks** — none of these need a browser, a network or an X account;
they run in a throwaway directory and never touch your real `data/` or `.env`:
```bash
python tests/test_api.py        # API, validation, queued publishing, recovery
python tests/test_schedule.py   # date mapping onto X's schedule dialog
python tests/test_security.py   # cross-origin guards, secrets, uploads
cd ui && npm run lint           # frontend lint
```

**Build executable:**
```bash
# Bundling the browser makes the app work on machines without Chrome
PLAYWRIGHT_BROWSERS_PATH=pw-browsers playwright install chromium
cd ui && npm run build && cd ..
pyinstaller "X Post Manager.spec" --distpath dist --clean
```

## Troubleshooting (development)

- **Port 5000 busy**: the server picks a free port automatically and logs it.
  Set `PORT` to pin a specific one.
- **A browser action hangs**: every browser operation times out (5 min) and
  reports an error rather than leaving the request stuck.

## Tech Stack

- **Backend**: Flask, SQLite, Playwright (browser automation)
- **Frontend**: React, TypeScript, Vite, TailwindCSS, Recharts
- **Desktop**: pywebview (EdgeChromium) / PyInstaller
- **Scheduling**: APScheduler

## Versioning

The version lives in a single `VERSION` file at the repository root. The interface,
`/api/health` and the startup log all read it, and the release tag should match it.
Bump that one file, tag `vX.Y.Z`, and the build workflow publishes the release.

## License

MIT

## Author

Developed by **Loic Meyer**

https://buymeacoffee.com/loicmeyer
