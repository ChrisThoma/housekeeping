# Contributing

Thanks for helping out. Bug reports, fixes and small features are all welcome.

## Getting set up

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
./run.sh                          # http://localhost:8000
.venv/bin/python -m pytest -q
```

The tests don't call the Anthropic API or the network, so they run without an API key. To try the Claude features by hand, put `ANTHROPIC_API_KEY` in `.env`. `run.sh` loads it.

To check the Docker image:

```sh
docker compose up --build
```

## Guidelines

- **Keep it simple to host.** One process, SQLite and no JavaScript build step. A change that needs a new service or a bundler needs a strong reason.
- **Match the code around you.** The frontend is plain JS in `static/app.js`. Colors are CSS variables at the top of `static/style.css`, with a value for each theme, light and dark.
- **Test scheduler and document changes.** `tests/test_scheduler.py` covers due dates and plans. `tests/test_documents.py` mocks Claude and the network.
- **Never send personal data anywhere new.** The only outside service the app talks to is the Anthropic API, plus the public URLs a user asks it to download.
- **Keep the UI quiet.** Use rows in a list rather than cards, status as colored text rather than badges, and icons only where they replace a label. Animations use the easing variables in `style.css`, stay under 300 ms, and exits are faster than entrances. Hover effects go inside `@media (hover: hover) and (pointer: fine)`. Nothing animates on tab switches.
- **Check the UI at phone width** (about 390px) and in both themes.

## Pull requests

1. Fork and create a branch.
2. Make your change, adding tests where it makes sense.
3. Run `python -m pytest -q`.
4. Open a pull request saying what changed and why. Include a screenshot for UI changes.

## Releases

Bump `__version__` in `app/__init__.py`, then push a tag such as `v0.2.0`. The release workflow publishes a multi-arch image to the GitHub Container Registry.
