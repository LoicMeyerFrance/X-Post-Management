# X Post Management

Desktop application to manage and schedule your X (Twitter) posts locally and
securely — with an assistant that writes and schedules them for you, running on
your own Claude, OpenAI or Google account.

> ### ✅ Up to date for 2026
>
> X rebuilt its sign-in and scheduling screens during 2026, and older builds can
> no longer sign in. **The current release works against the live site**, posts
> images and video, and no longer opens a blank window on Windows.
>
> **New:** an [Assistant](#assistant) tab — ask for three posts about your launch
> and it drafts them, schedules them and fills your calendar. Choose
> [Claude Code](https://code.claude.com), [Codex](https://developers.openai.com/codex/cli)
> or [Gemini CLI](https://github.com/google-gemini/gemini-cli): it drives the one
> *you* already pay for, so there is nothing extra to buy and the app never sees
> your credentials. It can do what this app does and nothing else — and it can
> read your own published timeline, so it knows what you have already said.
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
- **Everything on X**: read your whole timeline back from your profile, including
  posts published from your phone or the website, with their view and like counts
- **Assistant** tab: describe what you want in plain words and it writes, schedules
  and fills your calendar — it runs *your own* Claude Code, Codex or Gemini CLI, so
  there is no extra subscription (see [Assistant](#assistant))
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

## Everything on X

**History → Everything on X** reads your profile in the browser and mirrors it
locally. Until now the app only knew the posts it had sent itself; this picks up
everything else — posts made from your phone, from the website, before you
installed the app — and shows each one tagged *via the app* or *elsewhere*, with
its view and like counts.

The calendar can show it too: **All of X** in the calendar toolbar adds the posts
this app never sent, as hollow dots, so a day shows everything that actually went
out. A post sent from here already has its own entry, so it is never counted
twice. Those entries are read-only — there is nothing to reschedule about a tweet
that is already published.

It is a **read-only mirror**, kept in its own `x_posts` table. Nothing there is
scheduled, retried or published: mixing it into your real posts would put rows in
front of the scheduler that it has no business touching. Re-reading refreshes the
counts and never duplicates a tweet, so "new since last time" stays meaningful.

Two limits worth knowing:

- **X decides how far back you can scroll.** The read stops when the timeline
  stops growing, capped at 800 tweets; a long history may not come back whole,
  and the app says so when it hits the ceiling.
- **A count of zero comes back empty**, because X renders the button with no
  number at all rather than a `0`.

## Assistant

The **Assistant** tab is a chat: ask for what you want and it writes the posts,
schedules them and reads the calendar back to you. *"Prepare three posts about
the 1.5 release, one a day at 9am"* is a complete instruction.

It does not embed a model. The app runs a command-line agent already installed on
your machine as a child process, and gives it a small set of tools over
[MCP](https://modelcontextprotocol.io) — `create_post`, `list_posts`,
`update_post`, `get_limits` and so on, all of which go through the same local API
and the same validation as the rest of the interface.

**Three to choose from.** Open the Assistant tab and you get three cards:

| | Runs on | Confined to this app's tools |
|---|---|---|
| **Claude Code** | Claude Pro, Max, Team or Enterprise | yes |
| **Gemini CLI** | a Google account, or `GEMINI_API_KEY` | yes |
| **Codex** | a ChatGPT plan, or an OpenAI key | partly — see below |

The app checks your machine before you choose, so each card says whether that
agent is already installed and, if not, gives the command to install it. Click a
card to pick it; your choice is remembered, and **Change MCP** at the top right
switches later. Switching starts a fresh conversation, since the agents do not
share one.

**Codex is confined less far than the other two, and its card says so.** Its
shell and file tools are core to it and have no disable switch; the app runs it
read-only and approval-free so its MCP calls work at all, but it can still read
files on your computer. Claude Code and Gemini CLI can both be stripped to this
app's tools alone. If that matters to you, pick one of those.

**Setup: two buttons, no terminal.** Once a card is picked, the tab shows what is
missing and how to fix it. For Claude Code that is:

1. **Install** runs Anthropic's official installer for you. No Node.js, nothing
   to download by hand, and the exact command is shown before it runs. (It is
   `irm https://claude.ai/install.ps1 | iex` on Windows,
   `curl -fsSL https://claude.ai/install.sh | bash` elsewhere — run it yourself if
   you would rather.)
2. **Sign in** opens the Claude sign-in page in your browser.

Each step shows a tick once it is done, read from `claude auth status` rather than
guessed, so the tab never sends you into a chat that fails on its first message.
Gemini and Codex are installed with one `npm` command, shown on their card, and
signed into by running them once in a terminal — after which the tab picks the
sign-in up by itself.
Because the CLI holds your account, the app never asks for your Claude
credentials and never handles them.

**Claude Code needs a Claude Pro, Max, Team or Enterprise plan.** The free plan
does not include it; without one, put an Anthropic API key in
**Settings → Assistant** instead.

**It can only do what this app does.** The session is given the app's tools and
nothing else — no shell, no filesystem. `--allowedTools` alone would not achieve
that: it pre-approves tools rather than restricting them, and Claude Code's
read-only Bash commands run without a prompt *in every permission mode*. Two
flags close it: `--tools ""` drops every built-in tool, and
`--strict-mcp-config` ignores any MCP server configured elsewhere on the machine.
Asked to read a file or run a command, the agent answers that it has no tool for
it — and it has none.

**Internet — on, and one toggle away from off.** The agent has exactly two
read-only tools for it, `WebSearch` and `WebFetch`, so it can verify a claim and
cite where it came from before the text goes into a post. Nothing else comes with
them — still no shell, no files — and `WebFetch` cannot reach a private address,
so it cannot be turned back on this app's own API.

Switch it off and the agent cannot look anything up, and is told not to state an
outside fact as if it had checked one: ask for a post about news it cannot confirm
and it says so instead of inventing.

**Approval — one tick per post.** Each post the agent creates appears in the
conversation as a card with a ✓ and a ✗. Nothing reaches X until you press ✓,
which runs exactly the same publish path as the calendar; ✗ deletes the draft.

| | Manual (default) | Automatic |
|---|---|---|
| Write drafts, schedule, edit, delete in the app | yes | yes |
| Publish to X, schedule inside X | only when you press ✓ | the agent may do it itself |
| Shell and filesystem access | **none** | **none** |
| Search and read the web | yes, unless you switch Internet off | yes, unless you switch Internet off |

In manual mode the publish tools are not even in the agent's tool list, and the
MCP server refuses them if called anyway — hiding a tool is a hint to the model,
not an access control, so both are in place. Automatic mode lets the agent
publish without asking; the cards then report what it did rather than asking you.

**It can read what you have already published.** `list_published` returns the
posts already on X for this account — mirrored from your profile, so it includes
everything sent from your phone or the website, going back years — and
`get_stats` reports how they did. Both are read-only and available in either
approval mode: knowing what is out there is how it avoids saying it twice. Ask
*"have I posted about this before?"* or *"what did best?"* and it looks rather
than guesses.

X shows a view count on some posts and not others. A blank count is reported as
unknown rather than zero, and every figure says how many posts it could measure,
so an average never quietly pretends to cover the lot.

**Give it a document.** Point it at a file or a folder and it reads from it to
write your posts — `list_documents` and `read_document`, that folder and nothing
outside it. It cannot see anything else on your computer.

**Edit before it goes.** In manual mode a post card can be edited in place before
you press ✓, so a good draft with one wrong word does not need to be thrown away
and asked for again.

**Media.** Give it a path and it attaches the file: images and video both work,
with the same per-account size limits as the composer (5 MB images, 512 MB video,
16 GB on Premium).

**Which account pays.** By default, the agent signed in on this machine — your
own subscription, nothing extra to buy. Leave the key field in
**Settings → Assistant** empty for that. Enter an Anthropic API key there instead
and the run is billed to that API account; the key is stored in the OS credential
store, never in a file.

> Anthropic does not allow third-party apps to offer claude.ai sign-in to *their*
> users. That is why the subscription path only works for whoever is already
> signed in on the machine, and why the API key field exists for everyone else.

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
- **Assistant: "Claude Code is not installed"**: press **Install** in the
  Assistant tab and it runs the official installer for you. If you would rather
  do it by hand, the commands are in [Assistant](#assistant). A packaged app
  started from Explorer can have a narrower `PATH` than your terminal, so the app
  also looks in the native-installer, WinGet and npm locations.
- **Assistant: "not signed in" for Gemini or Codex**: run `gemini` or `codex` once
  in a terminal and sign in there. The app never handles those credentials, so it
  cannot sign in for you — but it reads the result and the tab updates.
- **Assistant: signed in but the tab still asks**: press *Check again*. The state
  comes from `claude auth status`, which the app re-reads rather than caching.
- **Assistant: "the app's tools could not be loaded"**: the MCP server did not
  start. The log has the reason — the most common one is the app's own API not
  answering, which the assistant reports rather than guessing at an answer.
- **Assistant refuses to publish**: that is manual approval doing its job. Switch
  *Approval* to **Automatic** in the Assistant tab, or publish from the calendar.

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

The **Assistant** adds no network surface: its MCP server talks over a pipe, not a
port, and it calls the same loopback API with the same validation as the
interface. Three further precautions:

- **it has no tools but this app's.** The session runs with `--tools ""` and
  `--strict-mcp-config`, so it carries no shell, no file access and no MCP server
  from anywhere else — only `create_post`, `list_posts` and the rest, plus
  `WebSearch` and `WebFetch` while the Internet toggle is on. Those two are
  read-only, and nothing else comes with them;
- **it cannot publish unless you say so.** In manual mode the publish tools are
  absent from its tool list, denied at the client, *and* refused by the server if
  called anyway — hiding a tool is a hint to the model, not an access control;
- **the Anthropic API key lives in the OS credential store**, is never written to
  a file, and is injected into the Claude Code child process only — never into
  this process's environment, which the browser would inherit;
- **the agent runs in a directory the app owns**, so hooks or extra MCP servers
  sitting in whatever project folder you happen to be in are never loaded.

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

## Where your data lives

Everything the app keeps — posts, uploads and a browser profile that runs to a
few hundred megabytes — goes in one folder:

| | |
|---|---|
| Windows | `Documents\X Post Management\` |
| macOS | `~/Documents/X Post Management/` |
| If Documents is synced to OneDrive, Dropbox or iCloud | the platform's app-data folder instead |

Documents because it is somewhere you can find, back up and delete on purpose. Not
a synced Documents, though: a sync client copying a live SQLite database and a
browser profile out from under the app means conflict copies, gigabytes of upload
and a database that can be corrupted mid-write — so the app checks, and steps
aside when it finds one.

**Settings → Your data** shows the exact path, how much room it takes, and opens
the folder.

Two deliberate exceptions:

- **An install that already keeps its data beside the executable keeps doing so.**
  Earlier versions wrote there, and an app that silently changes folder looks
  exactly like one that lost every post. Settings says so, and tells you where to
  move it if you want to.
- **A `portable.txt` file next to the executable** forces everything beside it —
  for a USB stick, or to leave no trace elsewhere. `XPM_HOME` overrides the lot.

```
data/
  posts.db            - SQLite database (posts, the X mirror, followers history)
  profile_info_<handle>.json   - Cached profile, one file per X account
  profile_picture_<handle>.jpg - Profile picture
  preferences.json    - UI preferences (language, theme)
  session.json        - Whether the last sign-in succeeded (no credentials)
  chrome_profile/     - Browser profile, when CHROME_PROFILE_DIR is empty
  uploads/            - Uploaded images
  agent_session.json  - Assistant conversation id, so it remembers the thread
  agent/              - Working directory the assistant runs in (kept empty)
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
python tests/test_agent.py      # MCP protocol, the CLI bridge, publish gating
python tests/test_timeline.py   # timeline scraping, pagination, the X mirror
cd ui && npm run lint           # frontend lint
```

`test_agent.py` also runs the MCP server as a real child process against a live
local server, which is the path that ships — but it never calls a model and
never sends a tweet.

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
- **Assistant**: the Claude Code CLI, driven over MCP (stdio, standard library only)

## Versioning

The version lives in a single `VERSION` file at the repository root. The interface,
`/api/health` and the startup log all read it, and the release tag should match it.
Bump that one file, tag `vX.Y.Z`, and the build workflow publishes the release.

## License

MIT

## Author

Developed by **Loic Meyer**

- GitHub — [@LoicmeyerMMI](https://github.com/LoicmeyerMMI)
- LinkedIn — [loic-meyer](https://www.linkedin.com/in/loic-meyer/)
- This repository — [LoicMeyerFrance/X-Post-Management](https://github.com/LoicMeyerFrance/X-Post-Management)

If the app saves you time: [buymeacoffee.com/loicmeyer](https://buymeacoffee.com/loicmeyer)
