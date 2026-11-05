"""Private, campaign-only Upgates observations. Never store hosts, SKU paths or query strings."""
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import json
from threading import Lock
from urllib.parse import urlsplit

from sqlalchemy import text

ACTIVE=ContextVar('atomer_catalog_meter',default=None)
RESOURCES={'config','languages','pricelists','categories','labels','availabilities','products',
           'products/simple','products/variants','products/parameters','products/labels','products/related',
           'products/images-queue','products/images'}


def resources(path):
    bits=path.lower().strip('/').split('/')
    if 'v2' in bits:
        bits=bits[bits.index('v2')+1:]
    if len(bits)>1 and bits[0]=='availabilities':
        return 'availabilities/by_id'
    resource='/'.join(bits)
    return resource if resource in RESOURCES else 'other'


def targets():
    from inventory_hub.services.order_collection import load_shop
    from inventory_hub.services.upgates_budget import target_key
    result={}
    for shop in ('xtrek','biketrek'):
        try:
            result[target_key((load_shop(shop) or {}).get('upgates_api_base_url',''))]=shop
        except Exception:
            continue
    return result


@contextmanager
def measure(campaign):
    events=Counter()
    label=targets()
    lock=Lock()
    token=ACTIVE.set((campaign,label,lock,events))
    try:
        yield events
    finally:
        ACTIVE.reset(token)


def record(key,method,url,event):
    active=ACTIVE.get()
    if active is None:
        return
    c,shops,lock,events=active
    try:
        parsed=urlsplit(url)
    except (TypeError,ValueError):
        return
    row=(c.get('phase','campaign'),shops.get(key,'unknown'),resources(parsed.path),method.upper(),event)
    with lock:
        events[row]+=1


async def persist(db,cid,events):
    if events:
        records=[dict(phase=p,shop=s,resource=r,method=m,event=e,n=n) for (p,s,r,m,e),n in events.items()]
        await db.execute(text("""INSERT INTO atomer_catalog_request_usage(campaign,bucket,phase,shop,resource,method,event,amount)
            SELECT :c,date_trunc('hour',now()),r->>'phase',r->>'shop',r->>'resource',r->>'method',r->>'event',(r->>'n')::int
            FROM jsonb_array_elements(CAST(:records AS jsonb)) r
            ON CONFLICT(campaign,bucket,phase,shop,resource,method,event) DO UPDATE SET amount=atomer_catalog_request_usage.amount+excluded.amount
            """),dict(c=cid,records=json.dumps(records)))


async def windows(db):
    keys=targets()
    if not keys:
        return {}
    rows=(await db.execute(text("""SELECT target_key,hour_remaining,day_remaining,blocked_until,observed_hour
             FROM upgates_api_budget WHERE target_key=ANY(CAST(:keys AS text[]))"""),{'keys':list(keys)})).mappings().all()
    return {keys[r['target_key']]:{k:v for k,v in r.items() if k!='target_key'} for r in rows}


async def summary(db,cid):
    rows=(await db.execute(text("""SELECT phase,shop,resource,method,event,amount FROM atomer_catalog_request_usage
             WHERE campaign=:c AND bucket=date_trunc('hour',now()) ORDER BY amount DESC,phase LIMIT 100"""),dict(c=cid))).mappings().all()
    return [dict(r) for r in rows]


def blocked_until(window,at=None):
    at=at or datetime.now(timezone.utc)
    until=window.get('blocked_until')
    if until and until>at:
        return until
    hour=at.replace(minute=0,second=0,microsecond=0)
    if window.get('observed_hour')==hour and window.get('hour_remaining')==window.get('day_remaining')==0:
        return hour+timedelta(hours=1,seconds=1)
    return None


async def defer_known_windows(db,c):
    """Quarantine only phases using the exhausted shop; DB card work is independent."""
    for shop,w in (await windows(db)).items():
        until=blocked_until(w)
        if not until:
            continue
        await db.execute(text("""UPDATE atomer_catalog_jobs j SET next_at=:n,problem=:p WHERE campaign=:c
            AND next_at<:n AND ((:shop='xtrek' AND stage IN ('prepared','sending','observe','related','fidelity','updating'))
                 OR (stage='cards' AND EXISTS(SELECT 1 FROM jsonb_each_text(j.guards->'scope') e
                     WHERE e.value<>:other)))"""),dict(c=c['id'],shop=shop,other='xtrek' if shop=='biketrek' else 'biketrek',
                          n=until,p='Limit Upgates · '+shop+' · čaká sa na známy termín uvoľnenia.'))
