"""Resume a read-only, account-bound native catalog scan across API budget windows."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from inventory_hub.services import atomer_catalog_observation as observe, migration_upgates_export as export
from inventory_hub.services.product_publication_source import plain


def _now():
    return datetime.now(timezone.utc)


def _page(client,page,filters):
    data=export._read(client,'products',{**filters,'page':page,'current_page_items':100})
    count=export._integer(data,'number_of_items')
    pages=export._integer(data,'number_of_pages')
    rows=data.get('products')
    if (not isinstance(rows,list) or export._integer(data,'current_page')!=page or
            pages not in ({0,1} if count==0 else {(count+99)//100}) or pages>export.MAX_PAGES or
            export._integer(data,'current_page_items')!=len(rows) or
            len(rows)!=max(0,min(100,count-(page-1)*100))):
        raise export.ExportError('upgates_export_invalid_pagination')
    return observe.indexed(rows,'product_id'),count,page>=pages


def _save_rows(db,rows):
    values=[]
    for pid,row in rows.items():
        try:
            created=datetime.fromisoformat(row['creation_time'])
            if created.utcoffset() is None:
                raise ValueError
        except (KeyError,TypeError,ValueError):
            raise export.ExportError('upgates_export_creation_time_unknown') from None
        values.append((pid,created.timestamp(),json.dumps(plain(row),ensure_ascii=False,separators=(',',':'))))
    db.executemany('INSERT OR REPLACE INTO parents VALUES(?,?,?)',values)


def _state(db,state):
    db.execute('INSERT OR REPLACE INTO cursor VALUES(1,?)',(json.dumps(state),))


def _begin_delta(db,state):
    db.execute('DELETE FROM witnessed')
    state.update(phase='delta',page=1,delta_count=None,delta_started=_now().isoformat())


def _prepare(db,state,client):
    phase=state['phase']
    if phase=='pages':
        rows,count,last=_page(client,state['page'],{})
        if state['page']==1:
            state['base_count']=count
        _save_rows(db,rows)
        state['page']+=1
        if last:
            _begin_delta(db,state)
    elif phase=='validate':
        # Recheck sparse native IDs when there is evidence of a deletion. Delete
        # only a stale observation from this journal, never a remote or physical row.
        ids=[t[0] for t in db.execute('SELECT id FROM parents WHERE id>? ORDER BY id LIMIT 100',(state['after'],))]
        if ids:
            rows=observe.indexed(observe.page_rows(client,'products',{'product_ids':';'.join(map(str,ids))}),'product_id')
            if set(rows)-set(ids):
                raise export.ExportError('upgates_export_unrequested_identity')
            db.executemany('DELETE FROM parents WHERE id=?',[(x,) for x in set(ids)-set(rows)])
            _save_rows(db,rows)
            state['after']=ids[-1]
        if len(ids)<100:
            _begin_delta(db,state)
    else:
        since=datetime.fromisoformat(state['base_started'])-timedelta(seconds=2)
        rows,count,last=_page(client,state['page'],{'last_update_time_from':since.isoformat(timespec='seconds')})
        if state['delta_count'] is not None and state['delta_count']!=count:
            _begin_delta(db,state)
            return
        _save_rows(db,rows)
        db.executemany('INSERT OR IGNORE INTO witnessed VALUES(?)',[(i,) for i in rows])
        state.update(delta_count=count,page=state['page']+1)
        if last:
            if db.execute('SELECT count(*) FROM witnessed').fetchone()[0]!=count:
                _begin_delta(db,state)
            else:
                state['phase']='certify'


def _certify(db,state,client):
    state['final_started']=_now().isoformat()
    count=observe.total(client,'products','products')
    try:
        observe.verify_read_window(client,datetime.fromisoformat(state['delta_started']))
    except export.ExportError as exc:
        if str(exc)!='upgates_export_catalog_changed':
            raise
        _begin_delta(db,state)
        return False
    size=db.execute('SELECT count(*) FROM parents').fetchone()[0]
    # Beyond count equality, exclude stale records that could hide an omitted
    # live ID. Initial cardinality + every distinct new ID - live count must be
    # zero. When a deletion was possible, re-read membership of every candidate.
    threshold=datetime.fromisoformat(state['base_started']).replace(microsecond=0).timestamp()
    born=db.execute('SELECT count(*) FROM parents WHERE created>=?',(threshold,)).fetchone()[0]
    if size==count and state['base_count']+born==count:
        return True
    now=_now().isoformat()
    if size<count or state['phase_before_delta']=='validate' and size!=count:
        db.execute('DELETE FROM parents')
        state.update(phase='pages',phase_before_delta='pages',page=1,base_started=now)
    else:
        state.update(phase='validate',phase_before_delta='validate',after=0,base_count=count,base_started=now)
    return False


def collect(client,directory,fp):
    """Only a complete, freshly fenced index is returned; each successful page has its own commit."""
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(fp+'.sqlite3')
    with closing(sqlite3.connect(path,timeout=30)) as db:
        path.chmod(0o600)
        db.execute('CREATE TABLE IF NOT EXISTS cursor(id INTEGER PRIMARY KEY, document TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS parents(id INTEGER PRIMARY KEY,created REAL NOT NULL,payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS witnessed(id INTEGER PRIMARY KEY)')
        db.commit()
        # Avoid an unbounded retry loop when another process continuously edits the catalog.
        for _ in range(export.MAX_PAGES*3+20):
            db.execute('BEGIN IMMEDIATE')
            saved=db.execute('SELECT document FROM cursor WHERE id=1').fetchone()
            state=json.loads(saved[0]) if saved else None
            if not state:
                state={'phase':'pages','page':1,'base_started':_now().isoformat(),'phase_before_delta':'pages'}
            elif state['phase']=='ready':
                # Start a new observation window on use, not when a previous
                # reader closed the database (which could be hours ago).
                _begin_delta(db,state)
            if state['phase']=='certify':
                if _certify(db,state,client):
                    result=[json.loads(r[0]) for r in db.execute('SELECT payload FROM parents ORDER BY id')]
                    # Leave a complete source-pinned checkpoint in place through cache-save or process loss.
                    state.update(base_started=state['final_started'],base_count=len(result),
                                 phase='ready',phase_before_delta='validate')
                    _state(db,state)
                    db.commit()
                    return result
            else:
                _prepare(db,state,client)
            _state(db,state)
            db.commit()
    raise export.ExportError('upgates_export_catalog_changed')


def archived_parents(directory,fp,ids=None,previous=None,identities_only=False):
    """Read only the last certified local observations. Never triggers a shop request."""
    path=directory/(fp+'.sqlite3') if fp else None
    if not path or not path.is_file() or ids is not None and not ids:
        return {},None
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        db.execute('BEGIN')
        stored=db.execute('SELECT document FROM cursor WHERE id=1').fetchone()
        if not stored:
            return {},None
        state=json.loads(stored[0]); checked=state.get('final_started')
        before_delta=(state.get('phase')=='delta' and state.get('page')==1 and state.get('delta_count') is None
                      and state.get('phase_before_delta')=='validate' and state.get('base_started')==checked)
        if not checked or state.get('phase')!='ready' and not before_delta:
            return {},None
        if previous==checked:
            return None,checked
        rows=(db.execute('SELECT id,payload FROM parents') if ids is None else
              db.execute('SELECT id,payload FROM parents WHERE id IN ('+','.join('?' for _ in ids)+')',list(ids)))
        parents={}
        for pid,payload in rows:
            parent=json.loads(payload)
            if identities_only:
                fields=('code','product_id','product_code','variant_id','ean')
                variants=parent.get('variants') or []
                parent={k:parent.get(k) for k in (*fields,'set_yn','variants_exists_yn')}
                parent['variants']=[{k:v.get(k) for k in fields} for v in variants]
            parents[str(pid)]=parent
        return parents,checked
