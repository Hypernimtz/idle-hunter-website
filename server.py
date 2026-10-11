"""Idle Hunter website and authenticated leaderboard receiver."""
import hmac
import json
import os
import tempfile
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, abort, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent / 'website'
KEYS = ('level', 'money', 'prestige', 'caught', 'lifetime_caught', 'myths', 'tracking', 'regions', 'tribes')
# Boards a bot must always send; anything else in KEYS may be missing (an older bot) and is published empty.
REQUIRED_KEYS = frozenset(KEYS) - {'lifetime_caught'}
# Every icon slug with real artwork under website/assets/badges/<tier>/ — an
# allow-list, not a trust-the-bot pass-through, so a bad payload can't point
# an <img> at an arbitrary path. Platinum excludes ammo_variety: it has no
# Platinum tier (see badges.html), so no platinum/ammo_variety.png exists.
BADGE_ICONS_GOLD = frozenset({
    'ammo_master', 'ammo_variety', 'blackjack_dealer', 'coinflip_tosser', 'crate_master',
    'daily_daily', 'events_completer', 'game_master', 'legendary_hunter', 'leveler',
    'lottery_winner', 'prestige_master', 'roulette_spinner', 'rps_npc',
    'slots_human_machine', 'xp_explosion',
})
BADGE_ICONS_PLATINUM = BADGE_ICONS_GOLD - {'ammo_variety'}
app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024
# One Gunicorn worker, multiple threads: a single shared, rebuildable snapshot.
_lock = threading.Lock()
_snapshot = {'updated_at': None, 'received_at': None, 'rankings': {k: [] for k in KEYS}}
_received = 0.0
_secret = os.environ.get('LEADERBOARD_PUSH_TOKEN', '')
if len(_secret) < 32 or not _secret.isascii():
    raise RuntimeError('Set LEADERBOARD_PUSH_TOKEN to a private random ASCII value of at least 32 characters.')

def clean_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {'rankings'}:
        raise ValueError('Expected rankings only')
    incoming = payload['rankings']
    if not isinstance(incoming, dict) or not REQUIRED_KEYS <= set(incoming) or set(incoming) - set(KEYS):
        raise ValueError('Missing ranking categories')
    result = {}
    for key in KEYS:
        entries = incoming.get(key, [])
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError('At most 100 entries per category')
        clean = []
        for entry in entries:
            if not isinstance(entry, dict) or not {'name', 'score'} <= set(entry) \
                    or set(entry) - {'name', 'score', 'badge', 'id'}:
                raise ValueError('Only public name, score, badge and id may be submitted')
            name, score = entry['name'], entry['score']
            if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
                raise ValueError('Invalid display name')
            if not isinstance(score, str) or not re.fullmatch(r'0|[1-9][0-9]{0,99}', score):
                raise ValueError('Score must be a nonnegative decimal string')
            clean_entry = {'name': name, 'score': score}
            if 'id' in entry:
                # Short non-reversible tag from the bot that tells same-named players apart; never a Discord id.
                if not isinstance(entry['id'], str) or not re.fullmatch(r'[0-9a-f]{4,16}', entry['id']):
                    raise ValueError('Invalid id')
                clean_entry['id'] = entry['id']
            if 'badge' in entry:
                badge = entry['badge']
                if not isinstance(badge, dict) or set(badge) != {'icon', 'tier'}:
                    raise ValueError('Invalid badge')
                icon, tier = badge.get('icon'), badge.get('tier')
                valid_icons = BADGE_ICONS_PLATINUM if tier == 'platinum' else BADGE_ICONS_GOLD
                if tier not in ('gold', 'platinum') or icon not in valid_icons:
                    raise ValueError('Invalid badge')
                clean_entry['badge'] = {'icon': icon, 'tier': tier}
            clean.append(clean_entry)
        # Preserve bot order for ties, enforce descending values otherwise.
        result[key] = sorted(clean, key=lambda row: int(row['score']), reverse=True)
    return result

# -- Changelog (pushed by the bot to POST /api/changelog, newest first) --
CHANGELOG_LIMIT = 200
CHANGELOG_BODY_LIMIT = 2 * 1024 * 1024
_changelog = []
_changelog_file = Path(os.environ.get('CHANGELOG_FILE') or Path(tempfile.gettempdir()) / 'idle-hunter-changelog.json')
try:   # best effort: survive a worker restart; the bot re-pushes every 30 minutes anyway
    _loaded = json.loads(_changelog_file.read_text(encoding='utf-8'))
    if isinstance(_loaded, list):
        _changelog = _loaded
except (OSError, ValueError):
    pass

def clean_changelog(payload):
    if not isinstance(payload, dict) or set(payload) != {'changelog'} or not isinstance(payload['changelog'], list):
        raise ValueError('Expected changelog array')
    if len(payload['changelog']) > CHANGELOG_LIMIT:
        raise ValueError('At most %d changelog entries' % CHANGELOG_LIMIT)
    out = []
    for entry in payload['changelog']:
        if not isinstance(entry, dict):
            raise ValueError('Invalid changelog entry')
        version, title, message = entry.get('version'), entry.get('title', ''), entry.get('message', '')
        date, date_iso = entry.get('date', 0), entry.get('date_iso', '')
        if not isinstance(version, str) or not re.fullmatch(r'[0-9]{1,4}(\.[0-9]{1,6}){1,4}', version):
            raise ValueError('Invalid changelog version')
        if not isinstance(title, str) or len(title) > 200 or not isinstance(message, str) or len(message) > 4000:
            raise ValueError('Invalid changelog text')
        if isinstance(date, bool) or not isinstance(date, int) or not isinstance(date_iso, str) or len(date_iso) > 30:
            raise ValueError('Invalid changelog date')
        out.append({'version': version, 'title': title, 'message': message, 'date': date, 'date_iso': date_iso})
    return out

@app.route('/api/changelog', methods=['GET', 'POST'])
def changelog():
    global _changelog
    if request.method == 'GET':
        with _lock:
            return jsonify(changelog=list(_changelog))
    request.max_content_length = CHANGELOG_BODY_LIMIT
    supplied = request.headers.get('Authorization', '')
    if not hmac.compare_digest(supplied.encode('utf-8'), ('Bearer ' + _secret).encode('utf-8')):
        return jsonify(error='Unauthorized'), 401
    if not request.is_json:
        return jsonify(error='Use application/json'), 415
    try:
        entries = clean_changelog(request.get_json())
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    with _lock:
        _changelog = entries
    try:
        _changelog_file.write_text(json.dumps(entries), encoding='utf-8')
    except OSError:
        pass
    return jsonify(status='updated', entries=len(entries))

@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.get('/healthz')
def health():
    return jsonify(status='ok')

@app.route('/api/leaderboard', methods=['GET', 'POST'])
def leaderboard():
    global _snapshot, _received
    if request.method == 'GET':
        with _lock:
            result = dict(_snapshot)
            result['stale'] = not _received or time.time() - _received > 180
        return jsonify(result)
    supplied = request.headers.get('Authorization', '')
    if not hmac.compare_digest(supplied.encode('utf-8'), ('Bearer ' + _secret).encode('utf-8')):
        return jsonify(error='Unauthorized'), 401
    if not request.is_json:
        return jsonify(error='Use application/json'), 415
    try:
        rankings = clean_payload(request.get_json())
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    stamp = datetime.now(timezone.utc).isoformat()
    with _lock:
        _snapshot = {'updated_at': stamp, 'received_at': stamp, 'rankings': rankings}
        _received = time.time()
    return jsonify(status='updated', updated_at=stamp)

@app.get('/')
def home():
    return send_from_directory(ROOT, 'index.html', conditional=True)

@app.get('/<path:filename>')
def static_file(filename):
    # Serve only website assets, never Python, environment or integration files.
    parts = Path(filename).parts
    if any(part.startswith('.') for part in parts) or Path(filename).suffix.lower() not in {'.html','.css','.js','.json','.png','.jpg','.jpeg','.svg','.mp4','.webp','.woff','.woff2'}:
        abort(404)
    return send_from_directory(ROOT, filename, conditional=True)
