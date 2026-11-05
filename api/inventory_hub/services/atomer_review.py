"""Durable per-product work, exact deletion decisions and an operator-accessible AI queue."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from inventory_hub.services.atomer_review_targets import ReviewError

Kind = Literal["family","item","extra"]
Status = Literal["open","queued","working","blocked","done"]
Scope = Literal["default","both","xtrek","biketrek"]
Decision = Literal["undecided","yes","no"]
Command = Literal["save","done","reopen","claim","complete","block","retry"]
EMPTY = dict(revision=0,status="open",scope="default",decision="undecided",note="",instruction="",result="",signature="")


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    key: str = Field(min_length=1,max_length=260)
    revision: int = Field(ge=0)
    run_id: int = Field(gt=0)
    command: Command
    scope: Scope | None = None
    decision: Decision | None = None
    note: str | None = Field(default=None,max_length=4000)
    instruction: str | None = Field(default=None,max_length=4000)
    result: str | None = Field(default=None,max_length=4000)


class KeepSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: int = Field(gt=0)
    revisions: dict[Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}/[1-9][0-9]{0,19}$")],
                    Annotated[int, Field(ge=0, strict=True)]] = Field(min_length=1, max_length=10_000)


async def _record(db,kind,key):
    row = (await db.execute(text("SELECT * FROM atomer_review_work WHERE kind=:kind AND key=:key"),
                            {"kind":kind,"key":key})).mappings().first()
    return dict(row) if row else dict(EMPTY)


async def _audit(db,kind,key,command,actor):
    await db.execute(text("""
        INSERT INTO atomer_review_events(kind,key,revision,command,actor,detail)
        SELECT kind,key,revision,:command,:actor,to_jsonb(w) FROM atomer_review_work w
        WHERE kind=:kind AND key=:key
    """),dict(kind=kind,key=key,command=command,actor=actor))


async def invalidate(db,run):
    # A changing identity, source match, quantity or card voids permission for that exact snapshot.
    await db.execute(text("""
        WITH changes AS (
            UPDATE atomer_review_work w SET revision=w.revision+1, decision='undecided',
              status='blocked', result='target_recheck_required', signature='',
              updated_at=now(), updated_by='catalog'
            FROM atomer_review_checks c
            WHERE c.run_id=:run AND w.kind='extra' AND w.key=c.key
              AND w.signature<>'' AND w.signature<>c.detail->>'signature'
              AND (w.decision='yes' OR w.status IN ('working','queued'))
              AND NOT (w.decision='yes' AND w.status IN ('working','done') AND c.detail->>'check'='missing')
            RETURNING w.*
        )
        INSERT INTO atomer_review_events(kind,key,revision,command,actor,detail)
        SELECT kind,key,revision,'recheck','catalog',to_jsonb(changes) FROM changes
    """),{"run":run})


async def _lock_current_run(db, expected_run):
    if not await db.scalar(text("SELECT pg_try_advisory_xact_lock_shared(691433137)")):
        raise ReviewError("refresh_in_progress")
    await db.execute(text("SELECT pg_advisory_xact_lock(691433138)"))
    run = await db.scalar(text("SELECT max(id) FROM atomer_progress_runs"))
    if expected_run != run:
        raise ReviewError("run_changed")
    return run


async def keep_selection(db, selection: KeepSelection, actor):
    """Record keep decisions and their audit events together for the supplied native IDs."""
    run = await _lock_current_run(db, selection.run_id)
    params = {"run": run, "keys": list(selection.revisions), "actor": actor}
    rows = (await db.execute(text("""
        SELECT c.key, w.revision, w.status, w.decision, w.instruction FROM atomer_review_checks c
        LEFT JOIN atomer_review_work w ON w.kind='extra' AND w.key=c.key
        WHERE c.run_id=:run AND c.key=ANY(CAST(:keys AS text[]))
    """), params)).mappings().all()
    if len(rows) != len(selection.revisions):
        raise ReviewError("subject_missing", 404)
    pending = []
    for row in rows:
        if row["decision"] == "no" and row["status"] == "done":
            continue
        if (selection.revisions[row["key"]] != (row["revision"] or 0) or row["status"] not in (None, "open")
                or row["decision"] not in (None, "undecided") or row["instruction"]):
            raise ReviewError("review_changed")
        pending.append(row["key"])
    if pending:
        await db.execute(text("""
            WITH saved AS (
                INSERT INTO atomer_review_work AS w
                    (kind,key,revision,status,scope,decision,note,instruction,result,signature,updated_by)
                SELECT 'extra',c.key,1,'done','default','no','','','',c.detail->>'signature',:actor
                FROM atomer_review_checks c WHERE c.run_id=:run AND c.key=ANY(CAST(:keys AS text[]))
                ON CONFLICT (kind,key) DO UPDATE SET revision=w.revision+1, status='done', decision='no',
                    signature=excluded.signature, updated_at=now(), updated_by=excluded.updated_by
                RETURNING w.*
            )
            INSERT INTO atomer_review_events(kind,key,revision,command,actor,detail)
            SELECT kind,key,revision,'keep',:actor,to_jsonb(saved) FROM saved
        """), dict(params, keys=pending))
    await db.commit()
    return {"kept": len(rows), "changed": len(pending)}


async def edit(db,change: Edit,actor):
    run = await _lock_current_run(db, change.run_id)
    kind, key, command = change.kind,change.key,change.command
    params = dict(run=run,key=key)
    target = None
    if kind == "extra":
        target = await db.scalar(text("SELECT detail FROM atomer_review_checks WHERE run_id=:run AND key=:key"),params)
        family = (target or {}).get("family_key")
    elif kind == "item":
        family = await db.scalar(text("SELECT family_key FROM atomer_progress_items WHERE run_id=:run AND code=:key"),params)
    else:
        family = await db.scalar(text("SELECT family_key FROM atomer_progress_items WHERE run_id=:run AND family_key=:key AND kind='variant' LIMIT 1"),params)
    if not family:
        raise ReviewError("subject_missing",404)
    current = await _record(db,kind,key)
    if current["revision"] != change.revision:
        raise ReviewError("review_changed")

    values = {k:current[k] for k in EMPTY if k != "revision"}
    if command in ("save","done"):
        for field in ("scope","decision","note","instruction"):
            value = getattr(change,field)
            if value is not None:
                values[field] = value.strip()
    elif any(getattr(change,k) is not None for k in ("scope","decision","note","instruction")):
        raise ReviewError("invalid_transition",422)

    status = current["status"]
    decision = values["decision"]
    if kind != "extra" and decision != "undecided":
        raise ReviewError("exact_variant_required",422)
    if command in ("save","claim","retry") and decision=="yes" and target and (target["check"]!="eligible" or
            (current["decision"]=="yes" and current["signature"]!=target["signature"])):
        raise ReviewError("target_recheck_required")
    if command in ("claim","complete","block","retry","reopen"):
        allowed = {"claim":("queued",),"complete":("working",),"block":("working",),
                   "retry":("blocked","open"),"reopen":("done","blocked","queued","working")}
        if status not in allowed[command]:
            raise ReviewError("invalid_transition")
        if command=="complete" and decision=="yes" and target and target["check"]!="missing":
            raise ReviewError("verify_removal_first")
        if command in ("complete","block") and not (change.result or "").strip():
            raise ReviewError("result_required",422)
        if command=="retry" and not (values["instruction"] or decision=="yes"):
            raise ReviewError("instruction_required",422)
        status = {"claim":"working","complete":"done","block":"blocked","retry":"queued","reopen":"open"}[command]
        if command in ("complete","block"):
            values["result"] = change.result.strip()
        elif command in ("retry","reopen"):
            values["result"] = ""
        if command=="reopen" and decision=="yes":
            values["decision"] = "undecided"
    elif command == "done":
        if decision=="yes" and status!="done":
            raise ReviewError("verify_removal_first")
        status = "done"
        if current["status"]!="done":
            values["result"] = ""
    else:
        changed = current["instruction"]!=values["instruction"] or current["decision"]!=decision
        if current["status"]=="working" and (changed or values["scope"]!=current["scope"]):
            raise ReviewError("working_first_reopen")
        if changed:
            status = "queued" if values["instruction"] or decision=="yes" else ("done" if decision=="no" else "open")
            values["result"] = ""

    if target and values["decision"]=="yes" and status in ("queued","working") and "admin_absence_unknown" in target["warnings"]:
        status = "blocked"
        values["result"] = "admin_source_required"
    if kind=="item":
        parent = await _record(db,"family",family)
        if parent["status"] in ("working","queued","done") and command!="reopen":
            raise ReviewError("family_manages_work")
    elif kind=="family" and status in ("working","queued","done"):
        active_child = await db.scalar(text("""
            SELECT EXISTS(SELECT 1 FROM atomer_review_work w JOIN atomer_progress_items i ON i.code=w.key
              WHERE w.kind='item' AND w.status IN ('queued','working') AND i.run_id=:run AND i.family_key=:key)
        """),params)
        if active_child:
            raise ReviewError("active_child_work")
    if target:
        values["signature"] = target["signature"]
    values.update(kind=kind,key=key,revision=change.revision+1,status=status,actor=actor)
    await db.execute(text("""
        INSERT INTO atomer_review_work(kind,key,revision,status,scope,decision,note,instruction,result,signature,updated_by)
        VALUES(:kind,:key,:revision,:status,:scope,:decision,:note,:instruction,:result,:signature,:actor)
        ON CONFLICT(kind,key) DO UPDATE SET revision=excluded.revision, status=excluded.status,
          scope=excluded.scope, decision=excluded.decision, note=excluded.note, instruction=excluded.instruction,
          result=excluded.result, signature=excluded.signature, updated_by=excluded.updated_by, updated_at=now()
    """),values)
    await _audit(db,kind,key,command,actor)
    result = await _record(db,kind,key)
    await db.commit()
    return result


async def decorate(db,groups):
    if not groups:
        return
    rows = await db.execute(text("""
        SELECT kind,key,to_jsonb(w) FROM atomer_review_work w WHERE
          (kind='family' AND key=ANY(CAST(:families AS text[]))) OR
          (kind='item' AND key=ANY(CAST(:items AS text[])))
    """),dict(families=list(groups),items=[r["code"] for g in groups.values() for r in g["items"]]))
    saved = {(k,key):r for k,key,r in rows}
    for key,group in groups.items():
        group["work"] = saved.get(("family",key),EMPTY)
        for row in group["items"]:
            row["work"] = saved.get(("item",row["code"]),EMPTY)


async def extras(db,run,search,decision,work,offset,limit):
    params = dict(run=run,search=search,decision=decision,work=work,limit=limit,offset=offset)
    clause = """ FROM atomer_review_checks c LEFT JOIN atomer_review_work w ON w.kind='extra' AND w.key=c.key
      WHERE c.run_id=:run AND (:decision='' OR coalesce(w.decision,'undecided')=:decision)
        AND (:work='all' OR (:work='done')=(coalesce(w.status,'open')='done'))
        AND (:search='' OR strpos(lower(concat_ws(' ', c.detail->>'name', c.detail->>'code',c.detail->>'ean',
          c.detail->>'parent', c.detail->>'source_parent', c.detail->>'product_id', c.detail->>'variant_id', c.detail->>'axes')),lower(:search))>0)"""
    total = await db.scalar(text("SELECT count(*)"+clause),params)
    rows = await db.execute(text("SELECT c.detail,to_jsonb(w)"+clause+" ORDER BY lower(c.detail->>'name'),c.detail->>'code',c.key LIMIT :limit OFFSET :offset"),params)
    selection = {}
    if decision == "undecided" and work == "all" and not search and offset == 0 and total <= 10_000:
        eligible = await db.execute(text("SELECT c.key,coalesce(w.revision,0)" + clause +
            " AND coalesce(w.status,'open')='open' AND coalesce(w.instruction,'')=''"), params)
        selection = dict(eligible.all())
    return {"total":total,"items":[dict(r,work=w or EMPTY) for r,w in rows],"run_id":run,"keep_selection":selection}


async def queue(db,run,status,offset,limit):
    # Only return identities still present in the currently published catalog snapshot.
    clause = """ FROM atomer_review_work w
      LEFT JOIN atomer_progress_items i ON w.kind='item' AND i.run_id=:run AND i.code=w.key
      LEFT JOIN LATERAL (SELECT ii.family_key,ii.detail->>'family_name' AS name FROM atomer_progress_items ii
          WHERE w.kind='family' AND ii.run_id=:run AND ii.family_key=w.key LIMIT 1) f ON true
      LEFT JOIN atomer_review_checks c ON w.kind='extra' AND c.run_id=:run AND c.key=w.key
      WHERE w.status=:status AND coalesce(i.code,f.family_key,c.key) IS NOT NULL
        AND (w.instruction<>'' OR w.decision='yes' OR w.status='blocked')"""
    params = dict(run=run,status=status,limit=limit,offset=offset)
    total = await db.scalar(text("SELECT count(*)"+clause),params)
    result = await db.execute(text("""SELECT w.kind,w.key,coalesce(i.detail->>'name',f.name,c.detail->>'name'),
       coalesce(i.family_key,f.family_key,c.family_key),to_jsonb(w),c.detail """+clause+
       " ORDER BY w.updated_at,w.kind,w.key LIMIT :limit OFFSET :offset"),params)
    return {"run_id":run,"total":total,"items":[dict(kind=k,key=key,name=name,family=family,work=w,target=c) for k,key,name,family,w,c in result]}
