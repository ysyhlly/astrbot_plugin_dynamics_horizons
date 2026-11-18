"""Durable reading, audit and delivery state. All writes share HorizonsStore's lock."""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from .contract import (
    chat_umo_kind,
    encode_json,
    normalize_identity,
    shanghai_day_bounds,
)


class ReviewConflict(ValueError):
    """The proposal or its owner changed since the action was rendered."""


class SourcePollConflict(ValueError):
    """The source changed while its poll was in progress."""


def check_review_target(row, expected_identity="", expected_revision=None):
    if expected_identity and row["identity"] != normalize_identity(expected_identity):
        raise ReviewConflict("review_identity_mismatch")
    if expected_revision is not None and row["revision"] != expected_revision:
        raise ReviewConflict("stale_review_revision")


class LifecycleStore:
    def migrate_lifecycle(self):
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS hz_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS reading_tasks(
                    task_id TEXT PRIMARY KEY, identity TEXT NOT NULL, source_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL, book_id TEXT NOT NULL, chapter_index INTEGER,
                    content_hash TEXT NOT NULL, status TEXT NOT NULL, proposal_id TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL, context_json TEXT NOT NULL DEFAULT '{}',
                    attempts INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS hz_tasks_book ON reading_tasks(book_id,chapter_index,status);
                CREATE TABLE IF NOT EXISTS reading_attempts(attempt_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,identity TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS model_calls(call_id TEXT PRIMARY KEY,identity TEXT NOT NULL,
                    role TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS hz_calls_day ON model_calls(identity,created_at);
                CREATE TABLE IF NOT EXISTS review_audit(audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL,from_state TEXT NOT NULL,to_state TEXT NOT NULL,
                    actor TEXT NOT NULL,reason TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS review_jobs(proposal_id TEXT PRIMARY KEY,kind TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS world_outbox(log_id TEXT PRIMARY KEY,identity TEXT NOT NULL,
                    revision INTEGER NOT NULL,operation TEXT NOT NULL,payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS feedback_jobs(event_id TEXT PRIMARY KEY,identity TEXT NOT NULL,
                    umo TEXT NOT NULL,member_id TEXT NOT NULL,payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0,created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS feedback_receipts(event_id TEXT PRIMARY KEY,created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS scope_bindings(umo TEXT PRIMARY KEY,identity TEXT NOT NULL,
                    managed INTEGER NOT NULL DEFAULT 0,updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS member_topic_prefs(identity TEXT NOT NULL,umo TEXT NOT NULL,
                    member_id TEXT NOT NULL,topic TEXT NOT NULL,blocked INTEGER NOT NULL,
                    source_event_id TEXT NOT NULL,updated_at REAL NOT NULL,
                    PRIMARY KEY(identity,umo,member_id,topic));
                CREATE TABLE IF NOT EXISTS corrections(correction_id TEXT PRIMARY KEY,identity TEXT NOT NULL,
                    umo TEXT NOT NULL,member_id TEXT NOT NULL,log_id TEXT NOT NULL,statement TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS discussion_reflections(
                    reflection_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    umo TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    log_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    anchor_event_id TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    member_quote TEXT NOT NULL DEFAULT '',
                    reflection TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    source_log_revision INTEGER NOT NULL,
                    UNIQUE(identity, umo, member_id, log_id, source_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_discussion_reflections_scope
                    ON discussion_reflections(identity, umo, member_id, occurred_at DESC);
                CREATE INDEX IF NOT EXISTS idx_discussion_reflections_log
                    ON discussion_reflections(identity, log_id, occurred_at DESC);
            """)
            additions = {
                "candidate_items": {
                    "archived": "INTEGER NOT NULL DEFAULT 0",
                    "pinned": "INTEGER NOT NULL DEFAULT 0",
                    "content_origin": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
                    "last_offered_at": "REAL NOT NULL DEFAULT 0",
                },
                "proposals": {
                    "revision": "INTEGER NOT NULL DEFAULT 0",
                    "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
                    "task_id": "TEXT NOT NULL DEFAULT ''",
                },
                "reading_logs": {
                    "status": "TEXT NOT NULL DEFAULT 'active'",
                    "revision": "INTEGER NOT NULL DEFAULT 1",
                },
                "books": {"progress_verified": "INTEGER NOT NULL DEFAULT 1"},
            }
            for table, columns in additions.items():
                present = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                for name, ddl in columns.items():
                    if name not in present:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            if not self.db.execute(
                "SELECT 1 FROM hz_migrations WHERE version=2"
            ).fetchone():
                # Old cursors are not evidence that preceding chapters were actually read.
                for b in self.db.execute(
                    "SELECT book_id,chapter_cursor FROM books"
                ).fetchall():
                    proven = {
                        r[0]
                        for r in self.db.execute(
                            "SELECT DISTINCT chapter_index FROM proposals WHERE book_id=? AND state='approved'",
                            (b["book_id"],),
                        )
                    }
                    cursor = 0
                    while cursor in proven:
                        cursor += 1
                    self.db.execute(
                        "UPDATE books SET progress_verified=? WHERE book_id=?",
                        (int(cursor >= b["chapter_cursor"]), b["book_id"]),
                    )
                self.db.execute("INSERT INTO hz_migrations VALUES(2,?)", (time.time(),))
            self.db.execute(
                "UPDATE reading_tasks SET status='interrupted' WHERE status='running'"
            )
            self.db.execute(
                "UPDATE feedback_jobs SET status='pending' WHERE status='running'"
            )
            self.db.execute(
                "UPDATE horizon_intents SET status='uncertain' WHERE status='claimed'"
            )
            self.db.execute(
                "INSERT OR IGNORE INTO review_jobs(proposal_id,kind,attempts,next_at) SELECT proposal_id,CASE WHEN review_mode='human' THEN 'human' ELSE 'retry' END,0,created_at+60 FROM proposals WHERE state='pending_review'"
            )
            self.db.execute(
                "UPDATE review_jobs SET kind='done' WHERE proposal_id IN (SELECT proposal_id FROM proposals WHERE state IN ('approved','rejected','revoked'))"
            )
            self.db.execute(
                "UPDATE review_jobs SET kind='held' WHERE proposal_id IN (SELECT proposal_id FROM proposals WHERE state='held')"
            )
            self.db.execute(
                "UPDATE reading_tasks SET status='pending_review' WHERE status='interrupted' AND task_id IN (SELECT task_id FROM proposals WHERE state='pending_review')"
            )

    def defer_review(self, proposal_id, now, reason, *, expected_revision=None, delay=300):
        with self.lock, self.db:
            if not self.db.execute(
                "SELECT 1 FROM proposals WHERE proposal_id=? AND state='pending_review' AND (? IS NULL OR revision=?)",
                (proposal_id, expected_revision, expected_revision),
            ).fetchone():
                return
            self.db.execute(
                "INSERT INTO review_jobs(proposal_id,kind,attempts,next_at,last_error) VALUES(?,'retry',0,?,?) ON CONFLICT(proposal_id) DO UPDATE SET kind='retry',next_at=excluded.next_at,last_error=excluded.last_error WHERE review_jobs.kind!='exhausted'",
                (proposal_id, now + delay, str(reason)[:200]),
            )

    def reserve_call(self, identity, role, limit, now, *, lane="reading"):
        start, end = shanghai_day_bounds(now)
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT role FROM model_calls WHERE identity=? AND created_at>=? AND created_at<?",
                (identity, start, end),
            ).fetchall()
            used = len(rows)
            if used >= limit:
                raise ValueError("daily_model_call_quota_reached")
            if lane == "feedback" and limit >= 6:
                reading_reserved = max(2, limit // 2)
                feedback_used = sum(
                    1 for r in rows if str(r["role"] or "").endswith(":feedback")
                )
                if feedback_used >= limit - reading_reserved:
                    raise ValueError("feedback_model_call_quota_reached")
            cid = uuid.uuid4().hex
            stored_role = f"{role}:{lane}" if lane != "reading" else role
            self.db.execute(
                "INSERT INTO model_calls VALUES(?,?,?,?,?)",
                (cid, identity, stored_role, "reserved", now),
            )
            return cid

    def finish_call(self, call_id, status):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE model_calls SET status=? WHERE call_id=?", (status, call_id)
            )

    def claim_reading(self, identity, unit, evidence, quota, now):
        key = [
            identity,
            unit.get("source_id", ""),
            unit.get("book_id", "") or unit.get("candidate_id", ""),
            unit.get("chapter_index"),
            evidence["content_hash"],
        ]
        tid = hashlib.sha256(encode_json(key).encode()).hexdigest()
        start, end = shanghai_day_bounds(now)
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM reading_tasks WHERE task_id=?", (tid,)
            ).fetchone()
            if row and row["status"] not in {"failed", "interrupted"}:
                return None
            used = self.db.execute(
                "SELECT count(*) FROM reading_attempts WHERE identity=? AND created_at>=? AND created_at<?",
                (identity, start, end),
            ).fetchone()[0]
            if used >= quota:
                return None
            if row:
                self.db.execute(
                    "UPDATE reading_tasks SET status='running',attempts=attempts+1,updated_at=? WHERE task_id=?",
                    (now, tid),
                )
            else:
                self.db.execute(
                    "INSERT INTO reading_tasks(task_id,identity,source_id,candidate_id,book_id,chapter_index,content_hash,status,evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tid,
                        identity,
                        unit.get("source_id", ""),
                        unit.get("candidate_id", ""),
                        unit.get("book_id", ""),
                        unit.get("chapter_index"),
                        evidence["content_hash"],
                        "running",
                        encode_json(evidence),
                        now,
                        now,
                    ),
                )
            self.db.execute(
                "INSERT INTO reading_attempts VALUES(?,?,?,?)",
                (uuid.uuid4().hex, tid, identity, now),
            )
            return tid

    def finish_reading(self, task_id, status, now, proposal_id="", context=None):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM reading_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row or row["status"] == "cancelled":
                return
            self.db.execute(
                "UPDATE reading_tasks SET status=?,proposal_id=?,context_json=?,updated_at=? WHERE task_id=?",
                (status, proposal_id, encode_json(context or {}), now, task_id),
            )
            if status == "read_no_note":
                if row["candidate_id"]:
                    self.db.execute(
                        "UPDATE candidate_items SET status='digested' WHERE candidate_id=?",
                        (row["candidate_id"],),
                    )
                self.advance_completed_chapters(row["book_id"], now)

    def advance_completed_chapters(self, book_id, now):
        """Caller holds the transaction. A cursor never implies unobserved chapters."""
        if not book_id:
            return
        book = self.db.execute(
            "SELECT * FROM books WHERE book_id=?", (book_id,)
        ).fetchone()
        if not book or not book["progress_verified"]:
            return
        done = {
            r[0]
            for r in self.db.execute(
                "SELECT chapter_index FROM reading_tasks WHERE book_id=? AND status IN ('approved','rejected','read_no_note')",
                (book_id,),
            )
        }
        done |= {
            r[0]
            for r in self.db.execute(
                "SELECT chapter_index FROM proposals WHERE book_id=? AND state='approved'",
                (book_id,),
            )
        }
        cursor = int(book["chapter_cursor"])
        while cursor in done:
            cursor += 1
        status = book["status"]
        if (
            status == "reading"
            and book["total_chapters"] > 0
            and cursor >= book["total_chapters"]
        ):
            status = "finished"
        self.db.execute(
            "UPDATE books SET chapter_cursor=?,status=?,updated_at=? WHERE book_id=?",
            (cursor, status, now, book_id),
        )

    def chapter_available(self, book_id, chapter_index):
        with self.lock:
            book = self.db.execute(
                "SELECT * FROM books WHERE book_id=?", (book_id,)
            ).fetchone()
            if (
                not book
                or book["status"] != "reading"
                or not book["progress_verified"]
                or book["chapter_cursor"] != chapter_index
            ):
                return False
            return not self.db.execute(
                "SELECT 1 FROM proposals WHERE book_id=? AND chapter_index=? AND state IN ('pending_review','held')",
                (book_id, chapter_index),
            ).fetchone()

    def continuity(self, book_id, before):
        with self.lock:
            rows = self.db.execute(
                "SELECT chapter_index,digest,reflection FROM proposals WHERE book_id=? AND chapter_index<? AND state='approved' ORDER BY chapter_index DESC LIMIT 3",
                (book_id, before),
            ).fetchall()
            contexts = self.db.execute(
                "SELECT t.context_json FROM reading_tasks t JOIN proposals p ON p.proposal_id=t.proposal_id WHERE t.book_id=? AND t.chapter_index<? AND p.state='approved' ORDER BY t.chapter_index DESC LIMIT 1",
                (book_id, before),
            ).fetchone()
        return {
            "recent_chapters": [dict(r) for r in reversed(rows)],
            "cumulative": json.loads(contexts[0]) if contexts else {},
        }

    def cancel_source_work(self, source_id, now):
        # Called in the source update transaction.
        self.db.execute(
            "UPDATE reading_tasks SET status='cancelled',updated_at=? WHERE source_id=? AND status IN ('running','pending_review','failed','interrupted')",
            (now, source_id),
        )
        self.db.execute(
            """
            UPDATE proposals
            SET state='held',revision=revision+1,review_reason='source_changed',updated_at=?
            WHERE state IN ('pending_review','held') AND review_reason!='source_changed'
            AND (candidate_id IN (SELECT candidate_id FROM candidate_items WHERE source_id=?)
                OR task_id IN (SELECT task_id FROM reading_tasks WHERE source_id=? AND status='cancelled'))
            """,
            (now, source_id, source_id),
        )
        self.db.execute(
            "UPDATE review_jobs SET kind='held' WHERE proposal_id IN (SELECT proposal_id FROM proposals WHERE review_reason='source_changed')"
        )

    def retry_review(
        self,
        proposal_id,
        actor,
        now,
        *,
        expected_identity="",
        expected_revision=None,
    ):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise ValueError("提案不存在")
            check_review_target(row, expected_identity, expected_revision)
            if row["state"] not in {"pending_review", "held"}:
                raise ReviewConflict("该提案不能重试审核")
            self.check_review_source(row)
            self.record_review_verdict(
                proposal_id,
                state="pending_review",
                verdict="pending",
                reason="管理员请求重新审核",
                reviewed_by=actor,
                now=now,
                expected_revision=row["revision"],
            )
            self.db.execute(
                """
                INSERT INTO review_jobs(proposal_id,kind,attempts,next_at,last_error)
                VALUES(?,'retry',0,?,'')
                ON CONFLICT(proposal_id) DO UPDATE SET
                    kind='retry',attempts=0,next_at=excluded.next_at,last_error=''
                """,
                (proposal_id, now),
            )
        return {"scheduled": True}

    def check_review_source(self, row):
        if row["review_reason"] == "source_changed":
            raise ReviewConflict("reading_task_cancelled")
        if (
            row["task_id"]
            and self.db.execute(
                "SELECT 1 FROM reading_tasks WHERE task_id=? AND status='cancelled'",
                (row["task_id"],),
            ).fetchone()
        ):
            raise ReviewConflict("reading_task_cancelled")

    def schedule_review(self, proposal_id, kind, now, error=""):
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT attempts FROM review_jobs WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            attempts = (int(old[0]) if old else 0) + (1 if kind == "retry" else 0)
            delays = (60, 300, 1800)
            next_at = (
                now + delays[attempts - 1] if kind == "retry" and attempts <= 3 else 0
            )
            if kind == "retry" and attempts > 3:
                kind = "exhausted"
            self.db.execute(
                "INSERT OR REPLACE INTO review_jobs VALUES(?,?,?,?,?)",
                (proposal_id, kind, attempts, next_at, error[:200]),
            )

    def due_reviews(self, now):
        with self.lock:
            return [
                r[0]
                for r in self.db.execute(
                    "SELECT j.proposal_id FROM review_jobs j JOIN proposals p USING(proposal_id) WHERE j.kind='retry' AND j.next_at<=? AND p.state='pending_review' ORDER BY j.next_at LIMIT 10",
                    (now,),
                )
            ]

    def queue_world(self, log_id, identity, operation, payload):
        # Caller holds transaction. Never reset a newer pending operation to an old write.
        old = self.db.execute(
            "SELECT revision FROM world_outbox WHERE log_id=?", (log_id,)
        ).fetchone()
        revision = int(old[0]) + 1 if old else 1
        self.db.execute(
            "INSERT OR REPLACE INTO world_outbox(log_id,identity,revision,operation,payload_json) VALUES(?,?,?,?,?)",
            (log_id, identity, revision, operation, encode_json(payload)),
        )

    def due_world(self, now):
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM world_outbox WHERE status='pending' AND next_at<=? ORDER BY next_at LIMIT 20",
                    (now,),
                )
            ]

    def finish_world(self, row, ok, reason, now):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE world_outbox SET status=?,attempts=attempts+1,next_at=?,last_error=? WHERE log_id=? AND revision=?",
                (
                    "done" if ok else "pending",
                    now + min(3600, 60 * 2 ** min(row["attempts"], 6)),
                    reason[:200],
                    row["log_id"],
                    row["revision"],
                ),
            )

    def revoke_proposal(
        self,
        proposal_id,
        actor,
        reason,
        now,
        *,
        expected_identity="",
        expected_revision=None,
    ):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if not row:
                raise ValueError("提案不存在")
            check_review_target(row, expected_identity, expected_revision)
            if row["state"] == "revoked":
                return {"revoked": True}
            if row["state"] != "approved":
                raise ReviewConflict("只有已通过的提案可以撤销")
            self.db.execute(
                "UPDATE proposals SET state='revoked',revision=revision+1,updated_at=? WHERE proposal_id=?",
                (now, proposal_id),
            )
            self.db.execute(
                "UPDATE reading_logs SET status='revoked',revision=revision+1 WHERE proposal_id=?",
                (proposal_id,),
            )
            self.db.execute(
                "INSERT INTO review_audit(proposal_id,from_state,to_state,actor,reason,created_at) VALUES(?,?,?,?,?,?)",
                (proposal_id, "approved", "revoked", actor, reason[:500], now),
            )
            self.db.execute(
                "UPDATE discussion_questions SET status='archived' WHERE log_id=?",
                (row["applied_log_id"],),
            )
            self.db.execute(
                "UPDATE horizon_intents SET status='cancelled',updated_at=? WHERE log_id=? AND status IN ('pending','claimed')",
                (now, row["applied_log_id"]),
            )
            self.db.execute(
                "UPDATE reading_tasks SET context_json='{}' WHERE book_id=? AND chapter_index>=?",
                (row["book_id"], row["chapter_index"] or 0),
            )
            iid = row["applied_interest_id"]
            if iid:
                others = self.db.execute(
                    "SELECT applied_log_id,reflection FROM proposals WHERE applied_interest_id=? AND state='approved' ORDER BY updated_at DESC",
                    (iid,),
                ).fetchall()
                self.db.execute(
                    "UPDATE staged_interests SET status=?,heat_score=?,summary=?,evidence_log_ids_json=?,updated_at=? WHERE interest_id=?",
                    (
                        "active" if others else "revoked",
                        min(2, 0.55 * len(others)),
                        others[0]["reflection"] if others else "",
                        encode_json([r["applied_log_id"] for r in others]),
                        now,
                        iid,
                    ),
                )
            self.queue_world(
                row["applied_log_id"],
                row["identity"],
                "revoke",
                {"log_id": row["applied_log_id"]},
            )
            return {"revoked": True, "log_id": row["applied_log_id"]}

    def bind_scope(self, umo, identity, managed, now):
        with self.lock, self.db:
            prev = self.db.execute(
                "SELECT identity,managed FROM scope_bindings WHERE umo=?", (umo,)
            ).fetchone()
            if prev is not None and prev["identity"] == identity and int(prev["managed"]) == int(managed):
                return False
            self.db.execute(
                "INSERT OR REPLACE INTO scope_bindings VALUES(?,?,?,?)",
                (umo, identity, int(managed), now),
            )
            return True

    def known_managed_scopes(self, identity: str = ""):
        with self.lock:
            rows = self.db.execute(
                "SELECT umo,identity FROM scope_bindings WHERE managed=1"
                + (" AND identity=?" if identity else ""),
                (identity,) if identity else (),
            ).fetchall()
            return [dict(r) for r in rows if chat_umo_kind(r["umo"])]

    def outreach_binding_valid(self, umo: str, identity: str) -> bool:
        kind = chat_umo_kind(umo)
        if not kind:
            return False
        binding = self.db.execute(
            "SELECT identity,managed FROM scope_bindings WHERE umo=?", (umo,)
        ).fetchone()
        if binding is None:
            # Legacy explicit groups can predate persisted scope bindings.
            return kind == "groupmessage"
        return bool(binding["managed"] and binding["identity"] == identity)

    def archive_candidates(self, now, days=90, limit=5000):
        with self.lock, self.db:
            eligible = "archived=0 AND pinned=0 AND NOT EXISTS(SELECT 1 FROM proposals p WHERE p.candidate_id=candidate_items.candidate_id) AND NOT EXISTS(SELECT 1 FROM reading_tasks t WHERE t.candidate_id=candidate_items.candidate_id)"
            self.db.execute(
                f"UPDATE candidate_items SET archived=1, full_text='' WHERE {eligible} AND created_at<?",
                (now - days * 86400,),
            )
            for r in self.db.execute(
                "SELECT identity,count(*) n FROM candidate_items WHERE archived=0 GROUP BY identity"
            ).fetchall():
                excess = max(0, r["n"] - limit)
                if excess:
                    self.db.execute(
                        f"UPDATE candidate_items SET archived=1, full_text='' WHERE candidate_id IN (SELECT candidate_id FROM candidate_items WHERE identity=? AND {eligible} ORDER BY created_at LIMIT ?)",
                        (r["identity"], excess),
                    )
            # Clear already-archived full_text unless referenced by an active/held proposal
            self.db.execute(
                "UPDATE candidate_items SET full_text='' WHERE archived=1 AND full_text!='' AND NOT EXISTS(SELECT 1 FROM proposals p WHERE p.candidate_id=candidate_items.candidate_id AND p.state IN ('pending_review','held'))"
            )

    def prune_terminal_records(
        self,
        now,
        *,
        retention_days: float = 30.0,
        attempt_days: float = 30.0,
        batch_limit: int = 2000,
    ) -> dict[str, int]:
        """Bounded retention GC for completed/expired terminal rows to prevent unbounded SQLite growth."""
        cutoff = float(now) - max(1.0, float(retention_days)) * 86400.0
        held_cutoff = float(now) - max(60.0, float(retention_days)) * 86400.0
        # Never prune records within the recent 7 Shanghai days so daily quota counts stay exact.
        attempt_cutoff = float(now) - max(7.0, float(attempt_days)) * 86400.0
        lim = max(1, min(10000, int(batch_limit)))
        removed: dict[str, int] = {}
        with self.lock, self.db:
            cur = self.db.execute(
                """
                DELETE FROM feedback_jobs
                WHERE event_id IN (
                    SELECT event_id FROM feedback_jobs
                    WHERE (status='done' AND created_at<?)
                       OR (status='held' AND created_at<?)
                    LIMIT ?
                )
                """,
                (cutoff, held_cutoff, lim),
            )
            removed["feedback_jobs"] = int(cur.rowcount or 0)
            cur = self.db.execute(
                """
                DELETE FROM feedback_receipts
                WHERE event_id IN (
                    SELECT event_id FROM feedback_receipts
                    WHERE created_at<?
                    LIMIT ?
                )
                """,
                (cutoff, lim),
            )
            removed["feedback_receipts"] = int(cur.rowcount or 0)
            cur = self.db.execute(
                """
                DELETE FROM reading_attempts
                WHERE attempt_id IN (
                    SELECT attempt_id FROM reading_attempts
                    WHERE created_at<?
                    LIMIT ?
                )
                """,
                (attempt_cutoff, lim),
            )
            removed["reading_attempts"] = int(cur.rowcount or 0)
            cur = self.db.execute(
                """
                DELETE FROM model_calls
                WHERE call_id IN (
                    SELECT call_id FROM model_calls
                    WHERE created_at<?
                    LIMIT ?
                )
                """,
                (attempt_cutoff, lim),
            )
            removed["model_calls"] = int(cur.rowcount or 0)
            cur = self.db.execute(
                """
                DELETE FROM world_outbox
                WHERE log_id IN (
                    SELECT log_id FROM world_outbox
                    WHERE status IN ('done', 'skipped', 'failed') AND next_at<?
                    LIMIT ?
                )
                """,
                (cutoff, lim),
            )
            removed["world_outbox"] = int(cur.rowcount or 0)
            cur = self.db.execute(
                """
                DELETE FROM horizon_intents
                WHERE intent_id IN (
                    SELECT intent_id FROM horizon_intents
                    WHERE status IN ('sent', 'expired', 'cancelled') AND updated_at<?
                    LIMIT ?
                )
                """,
                (cutoff, lim),
            )
            removed["horizon_intents"] = int(cur.rowcount or 0)
            cur = self.db.execute(
                """
                DELETE FROM book_chapters
                WHERE EXISTS (
                    SELECT 1 FROM books b
                    WHERE b.book_id=book_chapters.book_id
                      AND b.status IN ('finished', 'dropped')
                      AND b.updated_at<?
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM proposals p
                    WHERE p.book_id=book_chapters.book_id
                      AND p.chapter_index=book_chapters.chapter_index
                      AND p.state IN ('pending_review', 'held')
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM reading_tasks t
                    WHERE t.book_id=book_chapters.book_id
                      AND t.chapter_index=book_chapters.chapter_index
                      AND t.status IN ('running', 'pending_review', 'interrupted', 'failed')
                )
                """,
                (cutoff,),
            )
            removed["book_chapters"] = int(cur.rowcount or 0)
        return removed

    def diagnostics(self, identity, now):
        start, end = shanghai_day_bounds(now)
        with self.lock:
            calls = self.db.execute(
                "SELECT count(*) FROM model_calls WHERE identity=? AND created_at>=? AND created_at<?",
                (identity, start, end),
            ).fetchone()[0]
            tasks = [
                dict(r)
                for r in self.db.execute(
                    "SELECT status,count(*) n FROM reading_tasks WHERE identity=? GROUP BY status",
                    (identity,),
                )
            ]
            jobs = [
                dict(r)
                for r in self.db.execute(
                    "SELECT j.*,p.identity,p.revision,p.review_reason FROM review_jobs j JOIN proposals p USING(proposal_id) WHERE p.identity=?",
                    (identity,),
                )
            ]
            feedback = [dict(r) for r in self.db.execute(
                "SELECT status,COALESCE(json_extract(payload_json,'$.feedback_outcome'),'legacy') AS outcome,"
                "count(*) AS count FROM feedback_jobs WHERE identity=? GROUP BY status,outcome",
                (identity,),
            )]
            outbox = [
                dict(r)
                for r in self.db.execute(
                    "SELECT log_id,operation,status,attempts,next_at,last_error FROM world_outbox WHERE identity=? AND status='pending'",
                    (identity,),
                )
            ]
            archived = self.db.execute(
                "SELECT count(*) FROM candidate_items WHERE identity=? AND archived=1",
                (identity,),
            ).fetchone()[0]
            corrections = [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM corrections WHERE identity=? AND status='pending'",
                    (identity,),
                )
            ]
        return {
            "corrections": corrections,
            "model_calls_today": calls,
            "feedback_jobs": feedback,
            "reading_tasks": tasks,
            "review_jobs": jobs,
            "world_pending": outbox,
            "archived_candidates": archived,
            "storage_bytes": sum(
                p.stat().st_size
                for p in self.path.parent.glob(self.path.name + "*")
                if p.is_file()
            ),
        }

    def proposal_audit(self, proposal_id):
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT from_state,to_state,actor,reason,created_at FROM review_audit WHERE proposal_id=? ORDER BY audit_id",
                    (proposal_id,),
                )
            ]

    def proposal_continuity_evidence(self, proposal_id):
        with self.lock:
            row = self.db.execute(
                "SELECT book_id,chapter_index,task_id FROM proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if not row or not row["book_id"]:
                return {"proposed": {}, "previous": {}}
            task = self.db.execute(
                "SELECT context_json FROM reading_tasks WHERE task_id=?",
                (row["task_id"],),
            ).fetchone()
            return {
                "proposed": json.loads(task[0]) if task else {},
                "previous": self.continuity(row["book_id"], row["chapter_index"] or 0),
            }

    def intent_evidence_valid(self, row):
        if not self.db.execute(
            "SELECT 1 FROM reading_logs WHERE log_id=? AND identity=? AND status='active'",
            (row["log_id"], row["identity"]),
        ).fetchone():
            return False
        pref = self.db.execute(
            "SELECT opted_out FROM member_outreach_prefs WHERE identity=? AND umo=? AND member_id=?",
            (row["identity"], row["umo"], row["recipient_user_id"]),
        ).fetchone()
        if pref and pref[0]:
            return False
        if self.is_topic_blocked(row["topic"], row["identity"]):
            return False
        if self.db.execute(
            "SELECT 1 FROM member_topic_prefs WHERE identity=? AND umo=? AND member_id=? AND topic=? AND blocked=1",
            (row["identity"], row["umo"], row["recipient_user_id"], row["topic"]),
        ).fetchone():
            return False
        if not self.outreach_binding_valid(row["umo"], row["identity"]):
            return False
        if self.db.execute(
            "SELECT 1 FROM corrections WHERE identity=? AND umo=? AND member_id=? AND log_id=? AND status='pending'",
            (row["identity"], row["umo"], row["recipient_user_id"], row["log_id"]),
        ).fetchone():
            return False
        return True

    def check_poll_source(self, source):
        current = self.db.execute(
            "SELECT identity,enabled,config_json,updated_at FROM sources WHERE source_id=?",
            (source["source_id"],),
        ).fetchone()
        if (
            not current
            or not current["enabled"]
            or current["updated_at"] != source["updated_at"]
            or current["identity"] != source["identity"]
            or json.loads(current["config_json"]) != source["config"]
        ):
            raise SourcePollConflict("source_changed_during_poll")

    def commit_poll(self, source, items, cursor, cooldown, status, now, *, last_error=""):
        with self.lock, self.db:
            self.check_poll_source(source)
            inserted = self.insert_candidates(
                items,
                source_id=source["source_id"],
                source_kind=source["source_kind"],
                identity=source["identity"],
                now=now,
            )
            self.update_source_poll_state(
                source["source_id"],
                next_cursor=cursor,
                cooldown_until=cooldown,
                status=status,
                last_error=last_error,
                now=now,
            )
            return inserted
