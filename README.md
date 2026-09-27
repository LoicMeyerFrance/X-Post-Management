# X Post Management

Desktop application to manage and schedule your X (Twitter) posts locally and securely.

## Features

- Create and publish posts with text and/or images
- Schedule posts in advance (uses X native scheduling)
- Manage your drafts
- View publication history
- Delete tweets directly from the app
- Calendar view of your posts
- **My Profile** page: followers/following stats, bio, growth chart, follower variations
- Google account connection for login
- Auto-detect Chrome installation
- Persistent preferences (language, theme) across sessions
- Bilingual interface (English / French)
- Light and dark theme

## Download

Download the latest `X Post Management.exe` from the releases.

## First Use

On first launch, a **Setup Wizard** guides you through 4 steps:

1. **Welcome** — Choose your language (EN/FR) and click **Get started**
2. **Credentials** — Enter your X username (without @) and password. These are saved locally in a `.env` file — nothing is sent to any server besides X itself.
3. **Google Connection** — Connect with Google (same email as your X account). A browser window opens for authentication. The wizard waits until the connection is confirmed.
4. **Import Profile** — Click **Import Profile** to fetch your profile picture, display name, bio and follower counts from X.

Once complete, you're ready to compose, schedule and manage your posts.

> **Tip:** Leave Chrome profile and Chrome path empty (default) to use the built-in Chromium browser.

## Configuration Options

Configuration is stored in a `.env` file (created automatically by the Setup Wizard). You can also edit it manually or via **Settings > Configuration** in the app.

| Key | Description | Default |
|-----|-------------|---------|
| `X_USERNAME` | Your X username (without @) | |
| `X_PASSWORD` | Your X password | |
| `CHROME_PROFILE_DIR` | Path to Chrome profile directory | empty (uses `data/chrome_profile`) |
| `CHROME_PATH` | Path to Chrome executable | empty (uses the bundled Chromium) |
| `HEADLESS` | `true` for invisible browser, `false` to see it | `true` |
| `CHECK_INTERVAL_SECONDS` | Check frequency for scheduled posts (5-3600) | `15` |
| `MAX_RETRIES` | Number of retries on failure (0-10) | `1` |
| `PORT` | Port of the local server (falls back automatically if busy) | `5000` |
| `XPM_HOME` | Where `data/`, `logs/` and `.env` live | next to the executable |

Values are validated when saved: a bad username, an out-of-range interval or a
non-numeric retry count is rejected with a clear message instead of failing
later. Quotes, spaces and backslashes in your password are preserved exactly.

## Troubleshooting

- **Connection failed**: Test connection in Settings. If X requires verification, set `HEADLESS=false` and log in manually.
- **Post failed**: Make sure the image is under 5 MB.
- **Videos not supported**: X blocks automated video uploads. Only images are accepted.
- **Google login**: Use the "Connect to Google" button in Settings to authenticate with your Google account (same email as your X account).

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
- **stores `.env` and the saved session with owner-only permissions** where the
  filesystem supports it.

Uploads are checked by content, not just by file extension, capped at 5 MB, and
stored under generated names inside `data/uploads`.

Two things to keep in mind: your X password is stored in clear text in `.env`
(the browser automation needs to type it), and anyone with access to your user
account on this machine can read `data/`. Keep both private.

## Files Created

The app creates these files next to the executable:

```
data/
  posts.db            - SQLite database (posts + followers history)
  profile_info.json   - Cached profile information
  profile_picture.jpg - Profile picture
  preferences.json    - UI preferences (language, theme)
  state.json          - Saved X/Google session (cookies)
  chrome_profile/     - Browser profile, when CHROME_PROFILE_DIR is empty
  uploads/            - Uploaded images
logs/
  app.log             - Activity logs
.env                  - Configuration file
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

**Tests and checks:**
```bash
python tests/test_api.py      # API, validation and security checks (no browser, no account)
cd ui && npm run lint         # frontend lint
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

## License

MIT

## Author

Developed by **Loic Meyer**

https://buymeacoffee.com/loicmeyer
