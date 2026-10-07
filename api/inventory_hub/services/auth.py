"""Opaque persistent sessions; no password or session token is returned in JSON."""
import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from inventory_hub.auth_models import HubUser, HubSession, HubLoginLimit
from inventory_hub.settings import settings

COOKIE = '__Host-hub_session'
SESSION_SECONDS = 365 * 24 * 3600


def now():
    return datetime.now(timezone.utc)


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3, maxmem=128*1024*1024).hex()
    return 'scrypt$' + salt + '$' + digest


def password_matches(password, encoded):
    try:
        algorithm, salt, _ = encoded.split('$')
        return algorithm == 'scrypt' and secrets.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


DUMMY_HASH = password_hash('unused-timing-placeholder', '0' * 32)


def public_user(user):
    if user is None:
        return {'id': None, 'username': 'operator-token', 'display_name': 'Správca', 'role': 'admin', 'active': True, 'token_login': True}
    return {key: getattr(user, key) for key in ('id', 'username', 'display_name', 'role', 'active')}


def csrf(request):
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return
    origin = request.headers.get('origin')
    if (request.headers.get('x-hub-request') != '1'
            or request.headers.get('sec-fetch-site') == 'cross-site'
            or origin and urlsplit(origin).netloc != request.headers.get('host')):
        raise HTTPException(403, detail={'code': 'hub_origin_rejected', 'message': 'Use the Hub page for this operation'})


def set_cookie(response, token):
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, path='/', secure=True, httponly=True, samesite='strict')
    response.headers['Cache-Control'] = 'no-store'


async def new_session(db, response, user=None):
    token = secrets.token_urlsafe(32)
    db.add(HubSession(token_hash=fingerprint(token), user_id=user.id if user else None,
        operator_fingerprint=None if user else fingerprint(settings.AI_CONTENT_ACCESS_TOKEN.get_secret_value()),
        expires_at=now() + timedelta(seconds=SESSION_SECONDS)))
    await db.execute(delete(HubSession).where(HubSession.expires_at < now()))
    set_cookie(response, token)
    return public_user(user)


async def session_user(db, token):
    if not token or len(token) > 128:
        return None
    session = await db.get(HubSession, fingerprint(token))
    if not session or session.expires_at <= now():
        return None
    if session.user_id is None:
        configured = settings.AI_CONTENT_ACCESS_TOKEN.get_secret_value()
        if len(configured) < 24 or not secrets.compare_digest(session.operator_fingerprint or '', fingerprint(configured)):
            return None
        return public_user(None)
    user = await db.get(HubUser, session.user_id)
    return public_user(user) if user and user.active else None


async def login(db, username, password, address):
    # Persist both per-account and shared-client limits across API restarts.
    buckets = []
    for key, maximum in sorted([(fingerprint('user:' + username), 5), (fingerprint('client:' + address), 30)]):
        await db.execute(insert(HubLoginLimit).values(key=key, failures=0, window_start=now()).on_conflict_do_nothing())
        bucket = await db.scalar(select(HubLoginLimit).where(HubLoginLimit.key == key).with_for_update())
        if now() - bucket.window_start >= timedelta(minutes=5):
            bucket.failures, bucket.window_start = 0, now()
        buckets.append(bucket)
        if bucket.failures >= maximum:
            return None, 'hub_login_limited'
    user = await db.scalar(select(HubUser).where(HubUser.username == username))
    valid = await asyncio.to_thread(password_matches, password, user.password_hash if user else DUMMY_HASH)
    if not valid or not user or not user.active:
        for bucket in buckets:
            bucket.failures += 1
        return None, 'hub_login_invalid'
    for bucket in buckets:
        bucket.failures = 0
    return user, None
