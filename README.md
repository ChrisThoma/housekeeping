<div align="center">

<img src="static/icon-192.png" width="72" height="72" alt="">

# Housekeeping

**A self-hosted home maintenance planner.** List your appliances, yard and cars, and get a short, fixed list of what to do each week, based on their manuals, your climate and what you've already done.

[![License: MIT](https://img.shields.io/badge/license-MIT-2f6b4f)](LICENSE)
[![Docker](https://img.shields.io/badge/deploy-docker%20compose-2b4c7e)](#quick-start-docker)

<img src="docs/screenshot-plan.png" alt="This week's maintenance plan: five tasks with due tags, one checked off" width="860">

</div>

## Features

- **Inventory of everything that needs care.** Appliances, home systems, outdoor features, plants and lawn, and vehicles with model year and odometer reading.
- **A plan with a fixed size.** Choose how many tasks you want per week (or per day). The scheduler picks the most urgent ones and keeps the list stable for the period. Snooze or pause a task to free its slot.
- **Three kinds of schedule.** Every N days (furnace filter, 90 days), seasonal windows (clean gutters, Oct–Nov), and every N miles with a time cap (oil change, 5,000 mi or 6 months).
- **Suggestions from Claude, approved by you.** Claude proposes a schedule for each item based on its make, model, year and your climate. Nothing reaches your plan until you approve it.
- **Reads the manuals.** *Find manuals* searches the web for an item's owner's manual and downloads it. You can also upload PDFs or photos of a data plate, or paste a link. Claude notes the maintenance intervals with page numbers, part numbers and filter sizes, and suggestions follow the manufacturer's intervals.
- **Imports your own checklist.** Paste it, or upload a PDF or photo, and each recurring item becomes a task.
- **A logbook.** Every completion is recorded with the date, odometer reading and notes.
- **Self-hosted and private.** One container and a SQLite file. No account and no telemetry. Fonts are bundled, so the app makes no third-party requests except to the Anthropic API.
- **Works on a phone.** Responsive layout with a bottom tab bar, light and dark themes, and it can be installed to your home screen.

<table>
<tr>
<td width="68%"><img src="docs/screenshot-dark.png" alt="Documents tab in dark mode, showing the notes Claude took from a furnace manual"></td>
<td width="32%"><img src="docs/screenshot-mobile.png" alt="The weekly plan on a phone"></td>
</tr>
</table>

## Quick start (Docker)

You need Docker with Compose v2.24 or later, and an [Anthropic API key](https://console.anthropic.com/settings/keys) for the Claude features.

```sh
git clone <this repository> housekeeping && cd housekeeping
cp .env.example .env          # then add your ANTHROPIC_API_KEY
docker compose up -d
```

Open <http://localhost:8000>. The first screen walks you through setup.

Your data lives in the `housekeeping-data` Docker volume: the SQLite database plus every downloaded or uploaded document. To update to a new version:

```sh
git pull && docker compose up -d --build
```

### Back up and restore

```sh
# Back up to ./housekeeping-backup.tar.gz
docker run --rm -v housekeeping_housekeeping-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/housekeeping-backup.tar.gz -C /data .

# Restore
docker run --rm -v housekeeping_housekeeping-data:/data -v "$PWD":/backup alpine \
  tar xzf /backup/housekeeping-backup.tar.gz -C /data
```

The volume name has the Compose project (the folder name) as a prefix. `docker volume ls` shows it.

### Running it on a server

- **Set `HOUSEKEEPING_PASSWORD`** whenever anyone other than you can reach the port. Without it, anyone who can open the page can also spend your API credit.
- The app is a single process with a SQLite database, so run one container. It isn't designed to scale horizontally.
- Behind a reverse proxy (Caddy, nginx, Traefik), point it at port 8000. The container already trusts `X-Forwarded-*` headers. Uploads can be up to 60 MB, so raise the proxy's body size limit (for nginx, `client_max_body_size 64m`).
- `GET /api/health` returns `{"ok": true}` without a password. The container's health check uses it, and you can point uptime monitors at it too.

## Configuration

All settings are environment variables. With Docker, put them in `.env`.

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Turns on task suggestions, Find manuals and document reading. Everything else works without it. |
| `HOUSEKEEPING_PASSWORD` | (none) | Turns on HTTP basic sign-in for the whole app, except the health check. |
| `HOUSEKEEPING_USERNAME` | `admin` | User name for sign-in. |
| `HOUSEKEEPING_MODEL` | `claude-opus-5-5` | Claude model to use. |
| `HOUSEKEEPING_DB` | `/data/housekeeping.db` in Docker, `./housekeeping.db` otherwise | Database path. Documents are stored in a `documents/` folder next to it. |
| `PORT` | `8000` | Port on the host. |

Location, climate notes, hemisphere and plan size are set in the app, under **Settings**.

**About Claude and your documents:**
- Find manuals uses Claude's web search and web fetch tools. These must be enabled for your organization in the Anthropic Console.
- Documents Claude reads are uploaded to your Anthropic account's file storage, so re-reading a document doesn't upload it again. Deleting a document in the app deletes that copy too.
- PDFs up to 100 pages are sent whole, so Claude sees tables and diagrams. Longer PDFs are sent as extracted text. Each document is read once and the notes are reused.
- Downloads only go to public websites, never addresses on your local network. Files can be up to 60 MB and must be PDFs, web pages, images or text.

## How scheduling works

| Type | Example | When it's due |
|---|---|---|
| Every N days | Furnace filter, 90 days | N days after you last did it |
| Seasonal | Clean gutters, Oct–Nov | Once per season window. Urgency rises as the window closes. |
| Every N miles | Oil change, 5,000 mi or 180 days | Whichever comes first. Logging the odometer updates the car. |

Each active task gets an urgency score: the share of its interval that has passed. Tasks scoring 0.85 or higher are due. High-priority tasks rank ahead of others with the same urgency, and tasks you've never logged count as due now. When a week or day starts, the plan is filled with the top tasks up to your plan size, and it stays fixed until the period ends or you rebuild it.

To preview the plan for another date, add `?as_of=2026-11-20` to the URL.

## Development

Requires Python 3.10 or later. No JavaScript build step: the frontend is plain HTML, CSS and JS in `static/`.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
./run.sh                          # http://localhost:8000, reloads .env
.venv/bin/python -m pytest -q     # tests, no API key or network needed
```

```
app/
  main.py        FastAPI routes, optional sign-in
  scheduler.py   urgency scores and weekly/daily plans
  ai.py          Claude calls: suggestions, guide import, reading and finding documents
  documents.py   safe downloads, PDF/HTML text extraction, file storage
  db.py          SQLite schema and settings
static/          the web app (index.html, app.js, style.css, bundled fonts)
tests/
```

The API is documented at `/docs` (OpenAPI) in a running instance.

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). Bundled fonts (Bricolage Grotesque, Public Sans, IBM Plex Mono) are under the SIL Open Font License 1.1.
