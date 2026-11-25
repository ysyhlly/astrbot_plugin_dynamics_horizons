"""Batch 2 storage & query-plan regressions for Dynamics Horizons.

Covers:
- P1-3: Secondary indexes on feedback_jobs eliminating full table scans for
  due-job polling, retention GC, and per-scope lookups (verified via EXPLAIN QUERY PLAN).
- P2: Immediate compaction/cleanup of bulky evidence_json on terminal reading_tasks
  and proposals, plus bounded retention GC in prune_terminal_records() while
  preserving active/pending/held rows and compact media metadata.
"""

from __future__ import annotations

import json
from pathlib import Path

from astrbot_plugin_dynamics_horizons.core.contract import make_identity
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore

IDENT = make_identity("alice")
UMO = "aiocqhttp:GroupMessage:555666"


# ---------------------------------------------------------------------------
# P1-3: feedback_jobs indexes eliminate full table scans
# ---------------------------------------------------------------------------


def test_p1_3_feedback_jobs_explain_query_plan_uses_indexes(tmp_path: Path):
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    try:
        # Seed rows so the query planner has realistic stats
        now = 1764000000.0
        with store.lock, store.db:
            for i in range(50):
                status = "pending" if i % 5 == 0 else "done"
                store.db.execute(
                    "INSERT INTO feedback_jobs(event_id,identity,umo,member_id,payload_json,status,attempts,next_at,created_at) "
                    "VALUES(?,?,?,?,?,?,0,?,?)",
                    (f"ev_{i}", IDENT, UMO, f"u{i}", "{}", status, now - 10 + i, now - 100 + i),
                )

        # 1) Main due-job polling query (core/feedback.py:251)
        due_plan = " | ".join(
            str(r[-1])
            for r in store.db.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM feedback_jobs WHERE status='pending' AND next_at<=? ORDER BY created_at LIMIT 8",
                (now,),
            ).fetchall()
        )
        assert "idx_feedback_jobs_due" in due_plan, f"Unexpected plan: {due_plan}"
        assert "SCAN feedback_jobs" not in due_plan

        # 2) Retention GC query (core/lifecycle.py prune_terminal_records)
        gc_plan = " | ".join(
            str(r[-1])
            for r in store.db.execute(
                "EXPLAIN QUERY PLAN SELECT event_id FROM feedback_jobs WHERE (status='done' AND created_at<?) OR (status='held' AND created_at<?) LIMIT 100",
                (now - 86400, now - 86400 * 2),
            ).fetchall()
        )
        assert "idx_feedback_jobs_status_created" in gc_plan, f"Unexpected plan: {gc_plan}"
        assert "SCAN feedback_jobs" not in gc_plan

        # 3) Scope lookup query (core/discussion_feedback.py)
        scope_plan = " | ".join(
            str(r[-1])
            for r in store.db.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM feedback_jobs WHERE identity=? AND umo=? AND created_at>=? AND created_at<=?",
                (IDENT, UMO, now - 3600, now),
            ).fetchall()
        )
        assert "idx_feedback_jobs_scope" in scope_plan, f"Unexpected plan: {scope_plan}"
        assert "SCAN feedback_jobs" not in scope_plan
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P2: Terminal evidence_json immediate compaction & retention GC
# ---------------------------------------------------------------------------


def test_p2_terminal_transitions_clean_task_and_compact_proposal_evidence(tmp_path: Path):
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    now = 1764000000.0
    bulky_text = "超长原文内容" * 2000
    try:
        src = store.create_source(
            identity=IDENT,
            source_kind="bilibili",
            name="up",
            config={"mid": "12345"},
            now=now,
        )
        # 1) Approve path: reading_task evidence -> '{}', proposal evidence keeps compact media_meta only
        tid_ok = store.claim_reading(
            IDENT,
            {"source_id": src["source_id"], "candidate_id": "cand_ok"},
            {"text": bulky_text, "content_hash": "hash_ok"},
            quota=10,
            now=now,
        )
        assert tid_ok is not None
        prop_ok = store.create_proposal(
            identity=IDENT,
            source_kind="bilibili",
            title="视频摘要",
            bvid="BV1test0000001",
            digest="核对后的摘要",
            reflection="角色随感",
            task_id=tid_ok,
            evidence={
                "text": bulky_text,
                "chunks": [bulky_text],
                "content_hash": "hash_ok",
                "watch_mode": "keyframes_subtitle",
                "content_origin": "bilibili_vision_subtitle",
                "visual_observations": ["[00:10] 画面有猫"],
                "media_meta": {"watch_mode": "keyframes_subtitle", "frame_count": 4},
            },
            now=now,
        )
        committed = store.approve_proposal_atomic(prop_ok["proposal_id"], now=now + 10)
        assert committed["log"]["watch_mode"] == "keyframes_subtitle"

        task_row = store.db.execute(
            "SELECT status, evidence_json FROM reading_tasks WHERE task_id=?", (tid_ok,)
        ).fetchone()
        assert task_row["status"] == "approved"
        assert task_row["evidence_json"] == "{}"

        prop_row = store.db.execute(
            "SELECT state, evidence_json FROM proposals WHERE proposal_id=?",
            (prop_ok["proposal_id"],),
        ).fetchone()
        assert prop_row["state"] == "approved"
        compacted = json.loads(prop_row["evidence_json"])
        assert "text" not in compacted and "chunks" not in compacted
        assert compacted["watch_mode"] == "keyframes_subtitle"
        assert compacted["media_meta"]["frame_count"] == 4
        assert len(prop_row["evidence_json"]) < 500

        # 2) Revoke path clears proposal evidence completely
        store.revoke_proposal(prop_ok["proposal_id"], "admin", "撤回测试", now + 20)
        revoked_row = store.db.execute(
            "SELECT state, evidence_json FROM proposals WHERE proposal_id=?",
            (prop_ok["proposal_id"],),
        ).fetchone()
        assert revoked_row["state"] == "revoked"
        assert revoked_row["evidence_json"] == "{}"

        # 3) Reject path clears task evidence & strips bulky proposal evidence
        tid_rej = store.claim_reading(
            IDENT,
            {"source_id": src["source_id"], "candidate_id": "cand_rej"},
            {"text": bulky_text, "content_hash": "hash_rej"},
            quota=10,
            now=now + 30,
        )
        prop_rej = store.create_proposal(
            identity=IDENT,
            source_kind="feed",
            title="拒绝测试",
            digest="摘要",
            reflection="感想",
            task_id=tid_rej,
            evidence={"text": bulky_text, "raw_text": bulky_text},
            now=now + 30,
        )
        store.record_review_verdict(
            prop_rej["proposal_id"],
            state="rejected",
            verdict="reject",
            reason="不合适",
            now=now + 40,
        )
        assert (
            store.db.execute(
                "SELECT evidence_json FROM reading_tasks WHERE task_id=?", (tid_rej,)
            ).fetchone()[0]
            == "{}"
        )
        assert (
            store.db.execute(
                "SELECT evidence_json FROM proposals WHERE proposal_id=?",
                (prop_rej["proposal_id"],),
            ).fetchone()[0]
            == "{}"
        )

        # 4) finish_reading(read_no_note) clears task evidence immediately
        tid_no_note = store.claim_reading(
            IDENT,
            {"source_id": src["source_id"], "candidate_id": "cand_nn"},
            {"text": bulky_text, "content_hash": "hash_nn"},
            quota=10,
            now=now + 50,
        )
        store.finish_reading(tid_no_note, "read_no_note", now + 60)
        assert (
            store.db.execute(
                "SELECT evidence_json FROM reading_tasks WHERE task_id=?", (tid_no_note,)
            ).fetchone()[0]
            == "{}"
        )
    finally:
        store.close()


def test_p2_prune_terminal_records_clears_aged_evidence_and_preserves_active(tmp_path: Path):
    store = HorizonsStore(tmp_path / "horizons_gc.sqlite3")
    now = 1764000000.0
    old = now - 45 * 86400
    recent = now - 3600
    big_ev = json.dumps({"text": "LEGACY_BLOB_" * 500, "watch_mode": "text_only"})
    try:
        with store.lock, store.db:
            # Aged legacy terminal reading_task (should be cleared)
            store.db.execute(
                "INSERT INTO reading_tasks(task_id,identity,source_id,candidate_id,book_id,chapter_index,content_hash,status,evidence_json,created_at,updated_at) "
                "VALUES('t_old_done',?,'s1','c1','',NULL,'h1','approved',?,?,?)",
                (IDENT, big_ev, old, old),
            )
            # Active running reading_task (MUST be preserved)
            store.db.execute(
                "INSERT INTO reading_tasks(task_id,identity,source_id,candidate_id,book_id,chapter_index,content_hash,status,evidence_json,created_at,updated_at) "
                "VALUES('t_active',?,'s1','c2','',NULL,'h2','running',?,?,?)",
                (IDENT, big_ev, old, old),
            )
            # Aged terminal proposal (should be cleared)
            store.db.execute(
                "INSERT INTO proposals(proposal_id,identity,candidate_id,book_id,chapter_index,source_kind,state,title,creator,bvid,chapter_label,raw_url,digest,reflection,staged_interest_json,questions_json,review_mode,review_verdict,review_reason,review_risks_json,reviewed_by,evidence_json,created_at,updated_at) "
                "VALUES('p_old_done',?,'c1','',NULL,'feed','approved','old','','','','','d','r','{}','[]','llm','approve','','[]','llm',?,?,?)",
                (IDENT, big_ev, old, old),
            )
            # Active pending_review proposal (MUST be preserved even if old)
            store.db.execute(
                "INSERT INTO proposals(proposal_id,identity,candidate_id,book_id,chapter_index,source_kind,state,title,creator,bvid,chapter_label,raw_url,digest,reflection,staged_interest_json,questions_json,review_mode,review_verdict,review_reason,review_risks_json,reviewed_by,evidence_json,created_at,updated_at) "
                "VALUES('p_pending',?,'c2','',NULL,'feed','pending_review','pend','','','','','d','r','{}','[]','llm','pending','','[]','',?,?,?)",
                (IDENT, big_ev, old, old),
            )
            # Recent approved proposal within retention window (preserved until retention cutoff)
            store.db.execute(
                "INSERT INTO proposals(proposal_id,identity,candidate_id,book_id,chapter_index,source_kind,state,title,creator,bvid,chapter_label,raw_url,digest,reflection,staged_interest_json,questions_json,review_mode,review_verdict,review_reason,review_risks_json,reviewed_by,evidence_json,created_at,updated_at) "
                "VALUES('p_recent_done',?,'c3','',NULL,'feed','approved','rec','','','','','d','r','{}','[]','llm','approve','','[]','llm',?,?,?)",
                (IDENT, big_ev, recent, recent),
            )

        stats = store.prune_terminal_records(now, retention_days=30, attempt_days=30)
        assert stats["reading_task_evidence"] == 1
        assert stats["proposal_evidence"] == 1

        assert (
            store.db.execute(
                "SELECT evidence_json FROM reading_tasks WHERE task_id='t_old_done'"
            ).fetchone()[0]
            == "{}"
        )
        assert (
            store.db.execute(
                "SELECT evidence_json FROM reading_tasks WHERE task_id='t_active'"
            ).fetchone()[0]
            == big_ev
        )
        assert (
            store.db.execute(
                "SELECT evidence_json FROM proposals WHERE proposal_id='p_old_done'"
            ).fetchone()[0]
            == "{}"
        )
        assert (
            store.db.execute(
                "SELECT evidence_json FROM proposals WHERE proposal_id='p_pending'"
            ).fetchone()[0]
            == big_ev
        )
        assert (
            store.db.execute(
                "SELECT evidence_json FROM proposals WHERE proposal_id='p_recent_done'"
            ).fetchone()[0]
            == big_ev
        )
    finally:
        store.close()
