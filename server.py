"""Idle Hunter website and authenticated leaderboard receiver."""
import hmac
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, abort, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent / 'website'
KEYS = ('level', 'money', 'prestige', 'caught', 'myths', 'tracking', 'regions', 'tribes')
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
    if not isinstance(incoming, dict) or set(incoming) != set(KEYS):
        raise ValueError('Missing ranking categories')
    result = {}
    for key in KEYS:
        entries = incoming[key]
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError('At most 100 entries per category')
        clean = []
        for entry in entries:
            if not isinstance(entry, dict) or not {'name', 'score'} <= set(entry) \
                    or set(entry) - {'name', 'score', 'badge'}:
                raise ValueError('Only public name, score and badge may be submitted')
            name, score = entry['name'], entry['score']
            if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
                raise ValueError('Invalid display name')
            if not isinstance(score, str) or not re.fullmatch(r'0|[1-9][0-9]{0,99}', score):
                raise ValueError('Score must be a nonnegative decimal string')
            clean_entry = {'name': name, 'score': score}
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
