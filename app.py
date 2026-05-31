import sqlite3
import os
import requests
import json
from math import ceil
from dotenv import load_dotenv

load_dotenv()
from flask import Flask, render_template, jsonify, request, Response, stream_with_context

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'stars.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS repos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            name TEXT NOT NULL,
            total_stars INTEGER DEFAULT 0,
            last_synced TEXT,
            added_at TEXT DEFAULT (datetime('now')),
            UNIQUE(owner, name)
        );
        CREATE TABLE IF NOT EXISTS stars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            login TEXT NOT NULL,
            avatar_url TEXT,
            starred_at TEXT NOT NULL,
            followers INTEGER,
            following INTEGER,
            UNIQUE(repo_id, login),
            FOREIGN KEY(repo_id) REFERENCES repos(id)
        );
    ''')
    # Safe migration for existing DBs that don't have the new columns yet
    for col in ['followers INTEGER', 'following INTEGER']:
        try:
            conn.execute(f'ALTER TABLE stars ADD COLUMN {col}')
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


init_db()


def gh_headers(with_star=True):
    token = os.environ.get('GITHUB_TOKEN', '')
    h = {}
    if with_star:
        h['Accept'] = 'application/vnd.github.v3.star+json'
    if token:
        h['Authorization'] = f'token {token}'
    return h


def sse(data):
    return f'data: {json.dumps(data)}\n\n'


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    token = os.environ.get('GITHUB_TOKEN', '')
    if token:
        try:
            r = requests.get('https://api.github.com/rate_limit',
                             headers={'Authorization': f'token {token}'}, timeout=5)
            if r.ok:
                rl = r.json()['rate']
                return jsonify({'token': True, 'remaining': rl['remaining'], 'limit': rl['limit']})
        except Exception:
            pass
    return jsonify({'token': False, 'remaining': None, 'limit': 60})


@app.route('/api/repos', methods=['GET'])
def list_repos():
    conn = get_db()
    rows = conn.execute('''
        SELECT r.*,
               (SELECT COUNT(*) FROM stars s WHERE s.repo_id = r.id) AS cached_stars
        FROM repos r ORDER BY r.added_at DESC
    ''').fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route('/api/repos', methods=['POST'])
def add_repo():
    data = request.get_json() or {}
    slug = data.get('repo', '').strip()
    # Accept full GitHub URLs too
    for prefix in ('https://github.com/', 'http://github.com/', 'github.com/'):
        if slug.startswith(prefix):
            slug = slug[len(prefix):]
    slug = slug.strip('/')
    parts = slug.split('/')
    if len(parts) < 2:
        return jsonify({'error': 'Use owner/repo format'}), 400
    owner, name = parts[0], parts[1]

    r = requests.get(f'https://api.github.com/repos/{owner}/{name}',
                     headers=gh_headers(with_star=False), timeout=10)
    if r.status_code == 404:
        return jsonify({'error': f'Repository {owner}/{name} not found on GitHub'}), 404
    if not r.ok:
        msg = r.json().get('message', f'HTTP {r.status_code}')
        return jsonify({'error': f'GitHub API error: {msg}'}), 502

    info = r.json()
    owner = info['owner']['login']
    name = info['name']
    total_stars = info['stargazers_count']

    conn = get_db()
    try:
        conn.execute('INSERT INTO repos (owner, name, total_stars) VALUES (?, ?, ?)',
                     (owner, name, total_stars))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.execute('UPDATE repos SET total_stars=? WHERE owner=? AND name=?',
                     (total_stars, owner, name))
        conn.commit()
    repo = conn.execute('SELECT * FROM repos WHERE owner=? AND name=?', (owner, name)).fetchone()
    conn.close()
    return jsonify(dict(repo))


@app.route('/api/repos/<int:repo_id>', methods=['DELETE'])
def delete_repo(repo_id):
    conn = get_db()
    conn.execute('DELETE FROM stars WHERE repo_id=?', (repo_id,))
    conn.execute('DELETE FROM repos WHERE id=?', (repo_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/repos/<int:repo_id>/sync', methods=['POST'])
def sync_repo(repo_id):
    mode = request.args.get('mode', 'recent')

    @stream_with_context
    def generate():
        conn = get_db()
        repo = conn.execute('SELECT * FROM repos WHERE id=?', (repo_id,)).fetchone()
        conn.close()

        if not repo:
            yield sse({'error': 'Repository not found'})
            return

        owner, name = repo['owner'], repo['name']

        r = requests.get(f'https://api.github.com/repos/{owner}/{name}',
                         headers=gh_headers(with_star=False), timeout=10)
        if not r.ok:
            yield sse({'error': 'Failed to fetch repository info from GitHub'})
            return

        info = r.json()
        total_stars = info['stargazers_count']
        per_page = 100
        last_page = max(1, ceil(total_stars / per_page)) if total_stars > 0 else 1

        if mode == 'recent':
            # Grab the last 3 pages = most recent ~300 stargazers
            pages = list(range(max(1, last_page - 2), last_page + 1))
        else:
            pages = list(range(1, last_page + 1))

        headers = gh_headers(with_star=True)
        new_count = 0

        for i, page in enumerate(pages):
            yield sse({
                'progress': f'Fetching page {page} of {last_page}…',
                'current': i + 1,
                'total': len(pages)
            })

            r = requests.get(
                f'https://api.github.com/repos/{owner}/{name}/stargazers',
                headers=headers,
                params={'page': page, 'per_page': per_page},
                timeout=15
            )

            if r.status_code == 422:
                break  # GitHub caps pages around 400 for very large repos
            if not r.ok:
                yield sse({'error': f'GitHub API error on page {page}: HTTP {r.status_code}'})
                return

            stargazers = r.json()
            if not stargazers:
                break

            conn = get_db()
            for sg in stargazers:
                try:
                    cursor = conn.execute(
                        'INSERT OR IGNORE INTO stars (repo_id, login, avatar_url, starred_at) '
                        'VALUES (?, ?, ?, ?)',
                        (repo_id, sg['user']['login'], sg['user']['avatar_url'], sg['starred_at'])
                    )
                    if cursor.rowcount > 0:
                        new_count += 1
                except Exception:
                    pass
            conn.execute('UPDATE repos SET total_stars=?, last_synced=datetime("now") WHERE id=?',
                         (total_stars, repo_id))
            conn.commit()
            conn.close()

        conn = get_db()
        cached = conn.execute('SELECT COUNT(*) FROM stars WHERE repo_id=?', (repo_id,)).fetchone()[0]
        conn.close()
        yield sse({'done': True, 'new_count': new_count, 'total_stars': total_stars, 'cached_stars': cached})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/repos/<int:repo_id>/stargazers')
def get_stargazers(repo_id):
    limit = min(int(request.args.get('limit', 100)), 500)
    conn = get_db()
    rows = conn.execute(
        'SELECT login, avatar_url, starred_at, followers, following FROM stars WHERE repo_id=? '
        'ORDER BY starred_at DESC LIMIT ?',
        (repo_id, limit)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/repos/<int:repo_id>/enrich', methods=['POST'])
def enrich_profiles(repo_id):
    limit = min(int(request.args.get('limit', 100)), 200)

    @stream_with_context
    def generate():
        conn = get_db()
        rows = conn.execute(
            'SELECT login FROM stars WHERE repo_id=? AND followers IS NULL '
            'ORDER BY starred_at DESC LIMIT ?',
            (repo_id, limit)
        ).fetchall()
        conn.close()

        logins = [r['login'] for r in rows]
        if not logins:
            yield sse({'done': True, 'enriched': 0, 'message': 'All profiles already loaded'})
            return

        headers = gh_headers(with_star=False)
        enriched = 0

        for i, login in enumerate(logins):
            yield sse({'progress': f'Fetching @{login}…', 'current': i + 1, 'total': len(logins)})
            r = requests.get(f'https://api.github.com/users/{login}',
                             headers=headers, timeout=10)
            if r.ok:
                u = r.json()
                conn = get_db()
                conn.execute(
                    'UPDATE stars SET followers=?, following=? WHERE repo_id=? AND login=?',
                    (u.get('followers', 0), u.get('following', 0), repo_id, login)
                )
                conn.commit()
                conn.close()
                enriched += 1

        yield sse({'done': True, 'enriched': enriched})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/repos/<int:repo_id>/history')
def get_history(repo_id):
    granularity = request.args.get('granularity', 'week')

    conn = get_db()
    if granularity == 'week':
        # Return the Monday of each week as YYYY-MM-DD so the frontend gets real dates
        rows = conn.execute(
            "SELECT date(starred_at, '-' || CAST((CAST(strftime('%w', starred_at) AS INTEGER) + 6) % 7 AS TEXT) || ' days') AS period, "
            "COUNT(*) AS count "
            "FROM stars WHERE repo_id=? GROUP BY period ORDER BY period",
            (repo_id,)
        ).fetchall()
    else:
        fmt = {'day': '%Y-%m-%d', 'month': '%Y-%m'}.get(granularity, '%Y-%m-%d')
        rows = conn.execute(
            f"SELECT strftime('{fmt}', starred_at) AS period, COUNT(*) AS count "
            f"FROM stars WHERE repo_id=? GROUP BY period ORDER BY period",
            (repo_id,)
        ).fetchall()
    repo = conn.execute('SELECT total_stars, last_synced FROM repos WHERE id=?', (repo_id,)).fetchone()
    cached = conn.execute('SELECT COUNT(*) FROM stars WHERE repo_id=?', (repo_id,)).fetchone()[0]
    conn.close()

    cumulative = 0
    history = []
    for r in rows:
        cumulative += r['count']
        history.append({'period': r['period'], 'new': r['count'], 'cumulative': cumulative})

    return jsonify({
        'history': history,
        'total_stars': repo['total_stars'] if repo else 0,
        'cached_stars': cached,
        'last_synced': repo['last_synced'] if repo else None,
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8765))
    print(f'Starting Stars Watcher on http://localhost:{port}')
    if not os.environ.get('GITHUB_TOKEN'):
        print('Tip: add GITHUB_TOKEN to .env for 5000 req/hr instead of 60')
    app.run(debug=True, port=port)
