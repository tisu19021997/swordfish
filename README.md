# ★ swordfish

Track GitHub star history and recent stargazers for your repos.

## Features

- Star history chart (daily / weekly / monthly) with cumulative + new stars
- Recent stargazers in chronological order with avatar and profile link
- SQLite cache — only fetches new data on each sync
- Quick Sync (last ~300 stars) or Full Sync (complete history)

## Setup

```bash
git clone https://github.com/tisu19021997/swordfish.git
cd swordfish
uv sync
```

Create a `.env` file:

```
GITHUB_TOKEN=ghp_your_token_here
```

> A token with no scopes is enough. Without one you're limited to 60 req/hr, which makes Full Sync impractical for larger repos. Get one at **GitHub → Settings → Developer settings → Personal access tokens**.

## Run

```bash
uv run python app.py
```

Open [http://localhost:8765](http://localhost:8765).

Type `owner/repo` in the sidebar and hit **Add**.
