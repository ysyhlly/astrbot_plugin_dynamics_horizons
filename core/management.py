"""Authenticated admin lifecycle operations (called only by HorizonsWebAPI)."""

from __future__ import annotations

from .io import run_io
from .lifecycle import ReviewConflict, check_review_target


def resolve_correction(store, correction_id, action, actor, identity, now):
    with store.lock, store.db:
        row = store.db.execute(
            "SELECT c.*,l.proposal_id FROM corrections c JOIN reading_logs l USING(log_id) WHERE correction_id=?",
            (correction_id,),
        ).fetchone()
        if not row:
            raise ValueError("纠错不存在")
        check_review_target(row, identity)
        if row["status"] != "pending":
            if row["status"] == action:
                return {"resolved": True}
            raise ReviewConflict("该纠错已有处置结果，请刷新。")
        if action == "revoke":
            store.revoke_proposal(
                row["proposal_id"],
                actor,
                "管理员确认纠错",
                now,
                expected_identity=row["identity"],
            )
        store.db.execute(
            "UPDATE corrections SET status=? WHERE correction_id=?",
            (action, correction_id),
        )
    return {"resolved": True}


async def operate(engine, body, actor):
    op = body.get("op") or body.get("action")
    store = engine.store
    identity = body.get("identity") or ""
    revision = body.get("revision")
    if op == "revoke_proposal":
        result = await run_io(
            store.revoke_proposal,
            str(body.get("proposal_id", "")),
            actor,
            str(body.get("reason") or "管理员撤销"),
            engine.clock(),
            expected_identity=identity,
            expected_revision=revision,
        )
        await engine.refresh_projection()
        return result
    if op == "retry_review":
        return await run_io(
            store.retry_review,
            str(body.get("proposal_id", "")),
            actor,
            engine.clock(),
            expected_identity=identity,
            expected_revision=revision,
        )
    if op == "candidate_update":
        cid = str(body.get("candidate_id", ""))
        with store.lock, store.db:
            row = store.db.execute(
                "SELECT identity FROM candidate_items WHERE candidate_id=?", (cid,)
            ).fetchone()
            if not row:
                raise ValueError("候选不存在")
            check_review_target(row, identity)
            if isinstance(body.get("pinned"), bool):
                store.db.execute(
                    "UPDATE candidate_items SET pinned=? WHERE candidate_id=?",
                    (int(body["pinned"]), cid),
                )
            if isinstance(body.get("archived"), bool):
                store.db.execute(
                    "UPDATE candidate_items SET archived=? WHERE candidate_id=?",
                    (int(body["archived"]), cid),
                )
        return {"updated": True}
    if op == "resolve_correction":
        cid = str(body.get("correction_id", ""))
        action = body.get("resolution")
        if action not in {"dismiss", "revoke"}:
            raise ValueError("请选择驳回纠错或撤销原手记")
        result = await run_io(
            resolve_correction,
            store,
            cid,
            action,
            actor,
            identity,
            engine.clock(),
        )
        await engine.refresh_projection()
        return result
    return None


def archive_page(store, identity, page=1, query="", archived=True):
    page = max(1, min(100000, int(page)))
    where = "identity=? AND archived=?"
    params = [identity, int(archived)]
    if query:
        where += " AND (instr(title,?)>0 OR instr(summary,?)>0)"
        params += [str(query)[:120]] * 2
    with store.lock:
        total = store.db.execute(
            f"SELECT count(*) FROM candidate_items WHERE {where}", params
        ).fetchone()[0]
        page = min(page, max(1, (total + 99) // 100))
        rows = store.db.execute(
            f"SELECT candidate_id,title,creator,summary,full_text,source_kind,status,created_at,content_origin,pinned,archived FROM candidate_items WHERE {where} ORDER BY created_at DESC, candidate_id DESC LIMIT 100 OFFSET ?",
            (*params, (page - 1) * 100),
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": 100,
    }
