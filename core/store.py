"""SQLite WAL persistence engine for Dynamics Horizons."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .contract import (
    BOOK_STATUSES,
    INTENT_STATUSES,
    PLUGIN_ID,
    PROPOSAL_STATES,
    SHANGHAI_TZ,
    SOURCE_KINDS,
    TERMINAL_INTENT_STATUSES,
    clean_text,
    compact_json,
    compute_decayed_heat,
    encode_json,
    is_in_quiet_hours,
    normalize_identity,
    sha256_hex,
    shanghai_day_bounds,
    unpack_identity,
)
from .discussions import DiscussionStoreMixin
from .lifecycle import LifecycleStore, ReviewConflict, check_review_target
from .sanitize import (
    mask_secrets,
    merge_masked_secrets,
    sanitize_admin_url,
    sanitize_candidate_item,
    sanitize_citation,
    sanitize_untrusted_text,
    strip_raw_urls,
)

_OPT_OUT_CUES = (
    "别再向我分享",
    "不要再向我分享",
    "别再给我推荐",
    "不要再给我推荐",
    "不要主动找我",
    "别主动找我",
    "不要主动联系我",
    "别主动联系我",
    "不要主动发消息",
    "别主动发消息",
    "别打扰我",
    "不要打扰我",
)
_OPT_IN_CUES = (
    "恢复向我分享",
    "可以继续向我分享",
    "可以主动找我",
    "允许主动找我",
    "恢复主动找我",
    "可以主动联系我",
    "允许主动联系我",
)


def _escape_like(value: str) -> str:
    """Escape SQLite LIKE pattern metacharacters (`\\`, `%`, `_`)."""
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _extract_media_view(raw: Any) -> dict[str, Any]:
    """Normalize watch_mode, content_origin, visual_observations, and media_meta safely."""
    obj = raw if isinstance(raw, dict) else {}
    raw_meta = obj.get("media_meta") if isinstance(obj.get("media_meta"), dict) else {}
    watch_mode = str(obj.get("watch_mode") or raw_meta.get("watch_mode") or "").strip()[
        :48
    ]
    content_origin = str(
        obj.get("content_origin") or raw_meta.get("content_origin") or ""
    ).strip()[:80]
    raw_obs = obj.get("visual_observations")
    visual_observations: list[str] = []
    if isinstance(raw_obs, list):
        for item in raw_obs[:8]:
            if isinstance(item, str):
                cleaned = strip_raw_urls(item).replace("\n", " ").strip()[:120]
                if cleaned:
                    visual_observations.append(cleaned)
    media_meta: dict[str, Any] = {}
    if raw_meta:
        if raw_meta.get("watch_mode") or watch_mode:
            media_meta["watch_mode"] = str(
                raw_meta.get("watch_mode") or watch_mode
            ).strip()[:48]
        if raw_meta.get("content_origin") or content_origin:
            media_meta["content_origin"] = str(
                raw_meta.get("content_origin") or content_origin
            ).strip()[:80]
        for bkey in (
            "has_visual",
            "has_subtitle",
            "has_audio_transcript",
            "has_danmaku",
        ):
            if bkey in raw_meta:
                media_meta[bkey] = bool(raw_meta[bkey])
        for ikey in ("frame_count", "duration_sec"):
            if ikey in raw_meta:
                try:
                    media_meta[ikey] = max(0, int(raw_meta[ikey] or 0))
                except (TypeError, ValueError):
                    media_meta[ikey] = 0
        if "provider_id" in raw_meta:
            media_meta["provider_id"] = str(raw_meta.get("provider_id") or "").strip()[
                :120
            ]
    return {
        "watch_mode": watch_mode,
        "content_origin": content_origin,
        "visual_observations": visual_observations,
        "media_meta": media_meta,
    }


class HorizonsStore(DiscussionStoreMixin, LifecycleStore):
    """Thread-safe SQLite store separating global character reading from per-UMO consumption."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL;")
        self.db.execute("PRAGMA synchronous=NORMAL;")
        self.db.execute("PRAGMA busy_timeout=5000;")
        self.db.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()
        self.migrate_lifecycle()
        from .transactions import NestedConnection

        self.db = NestedConnection(self.db)

    def _init_schema(self) -> None:
        with self.lock, self.db:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    cursor_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_error TEXT NOT NULL DEFAULT '',
                    last_poll_at REAL NOT NULL DEFAULT 0,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sources_identity ON sources(identity, enabled);

                CREATE TABLE IF NOT EXISTS books (
                    book_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '',
                    source_kind TEXT NOT NULL DEFAULT 'ebook',
                    provider TEXT NOT NULL DEFAULT 'local',
                    external_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    creator TEXT NOT NULL DEFAULT '',
                    chapter_cursor INTEGER NOT NULL DEFAULT 0,
                    total_chapters INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'reading',
                    last_reflection TEXT NOT NULL DEFAULT '',
                    raw_url TEXT NOT NULL DEFAULT '',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_books_identity ON books(identity, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS book_chapters (
                    book_id TEXT NOT NULL,
                    chapter_index INTEGER NOT NULL,
                    chapter_label TEXT NOT NULL,
                    content_text TEXT NOT NULL,
                    PRIMARY KEY (book_id, chapter_index)
                );

                CREATE TABLE IF NOT EXISTS candidate_items (
                    candidate_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    book_id TEXT NOT NULL DEFAULT '',
                    chapter_index INTEGER,
                    total_chapters INTEGER,
                    title TEXT NOT NULL,
                    creator TEXT NOT NULL DEFAULT '',
                    bvid TEXT NOT NULL DEFAULT '',
                    chapter_label TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    raw_url TEXT NOT NULL DEFAULT '',
                    published_at REAL NOT NULL DEFAULT 0,
                    content_ready INTEGER NOT NULL DEFAULT 0,
                    full_text TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unread',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    UNIQUE (source_id, external_id)
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_unread ON candidate_items(identity, status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_candidates_fp ON candidate_items(identity, fingerprint);

                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    candidate_id TEXT NOT NULL DEFAULT '',
                    book_id TEXT NOT NULL DEFAULT '',
                    chapter_index INTEGER,
                    source_kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    title TEXT NOT NULL,
                    creator TEXT NOT NULL DEFAULT '',
                    bvid TEXT NOT NULL DEFAULT '',
                    chapter_label TEXT NOT NULL DEFAULT '',
                    raw_url TEXT NOT NULL DEFAULT '',
                    digest TEXT NOT NULL,
                    reflection TEXT NOT NULL,
                    staged_interest_json TEXT NOT NULL DEFAULT '{}',
                    questions_json TEXT NOT NULL DEFAULT '[]',
                    review_mode TEXT NOT NULL DEFAULT 'llm',
                    review_verdict TEXT NOT NULL DEFAULT '',
                    review_reason TEXT NOT NULL DEFAULT '',
                    review_risks_json TEXT NOT NULL DEFAULT '[]',
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    applied_log_id TEXT NOT NULL DEFAULT '',
                    applied_interest_id TEXT NOT NULL DEFAULT '',
                    applied_question_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_proposals_identity ON proposals(identity, state, created_at DESC);

                CREATE TABLE IF NOT EXISTS reading_logs (
                    log_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL DEFAULT '',
                    identity TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    citation_json TEXT NOT NULL,
                    raw_url TEXT NOT NULL DEFAULT '',
                    digest TEXT NOT NULL,
                    reflection TEXT NOT NULL,
                    fact_kind TEXT NOT NULL DEFAULT 'external_media',
                    media_meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_logs_identity ON reading_logs(identity, created_at DESC);

                CREATE TABLE IF NOT EXISTS staged_interests (
                    interest_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    heat_score REAL NOT NULL DEFAULT 1.0,
                    last_reinforced_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    evidence_log_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (identity, topic)
                );
                CREATE INDEX IF NOT EXISTS idx_interests_identity ON staged_interests(identity, status);

                CREATE TABLE IF NOT EXISTS discussion_questions (
                    question_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    log_id TEXT NOT NULL,
                    interest_id TEXT NOT NULL DEFAULT '',
                    topic TEXT NOT NULL,
                    question TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_questions_identity ON discussion_questions(identity, status, expires_at DESC);

                CREATE TABLE IF NOT EXISTS question_umo_state (
                    question_id TEXT NOT NULL,
                    umo TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    discussed_at REAL NOT NULL DEFAULT 0,
                    turn_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (question_id, umo)
                );
                CREATE INDEX IF NOT EXISTS idx_qumo_umo ON question_umo_state(umo, status, discussed_at DESC);

                CREATE TABLE IF NOT EXISTS co_discussed_topics (
                    identity TEXT NOT NULL,
                    umo TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    bot_reply_count INTEGER NOT NULL DEFAULT 0,
                    last_quote TEXT NOT NULL DEFAULT '',
                    last_event_id TEXT NOT NULL DEFAULT '',
                    last_turn_id TEXT NOT NULL DEFAULT '',
                    last_occurred_at REAL NOT NULL DEFAULT 0,
                    opted_out INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (identity, umo, member_id, topic)
                );

                CREATE TABLE IF NOT EXISTS member_outreach_prefs (
                    identity TEXT NOT NULL,
                    umo TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    opted_out INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (identity, umo, member_id)
                );

                CREATE TABLE IF NOT EXISTS horizon_intents (
                    intent_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    umo TEXT NOT NULL,
                    recipient_user_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    log_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    payload_json TEXT NOT NULL,
                    not_before REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_intents_due ON horizon_intents(status, not_before, expires_at);
                CREATE INDEX IF NOT EXISTS idx_intents_member ON horizon_intents(umo, recipient_user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS blocked_topics (
                    topic TEXT NOT NULL,
                    identity TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    PRIMARY KEY (topic, identity)
                );
                """
            )
            log_cols = {
                r[1] for r in self.db.execute("PRAGMA table_info(reading_logs)")
            }
            if "media_meta_json" not in log_cols:
                self.db.execute(
                    "ALTER TABLE reading_logs ADD COLUMN media_meta_json TEXT NOT NULL DEFAULT '{}'"
                )

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # ------------------------------------------------------------------
    # Blocked Topics
    # ------------------------------------------------------------------

    def list_blocked_topics(self, identity: str = "") -> list[str]:
        norm = normalize_identity(identity) if identity else ""
        with self.lock:
            if norm:
                rows = self.db.execute(
                    "SELECT DISTINCT topic FROM blocked_topics WHERE identity IN ('', ?) ORDER BY created_at DESC",
                    (norm,),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT DISTINCT topic FROM blocked_topics ORDER BY created_at DESC"
                ).fetchall()
        return [str(r["topic"]) for r in rows]

    def block_topic(
        self, topic: str, identity: str = "", now: float | None = None
    ) -> list[str]:
        clean = clean_text(topic, "topic", 80)
        norm = normalize_identity(identity) if identity else ""
        stamp = float(now if now is not None else time.time())
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO blocked_topics(topic, identity, created_at) VALUES (?, ?, ?)",
                (clean, norm, stamp),
            )
            like_pat = f"%{_escape_like(clean)}%"
            if norm:
                self.db.execute(
                    "UPDATE staged_interests SET status='revoked', updated_at=? WHERE identity=? AND status='active' AND (topic=? OR topic LIKE ? ESCAPE '\\')",
                    (stamp, norm, clean, like_pat),
                )
                self.db.execute(
                    "UPDATE discussion_questions SET status='archived' WHERE identity=? AND status='active' AND (topic=? OR question LIKE ? ESCAPE '\\')",
                    (norm, clean, like_pat),
                )
                self.db.execute(
                    "UPDATE candidate_items SET status='blocked' WHERE identity=? AND status='unread' AND (title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\')",
                    (norm, like_pat, like_pat),
                )
            else:
                self.db.execute(
                    "UPDATE staged_interests SET status='revoked', updated_at=? WHERE status='active' AND (topic=? OR topic LIKE ? ESCAPE '\\')",
                    (stamp, clean, like_pat),
                )
                self.db.execute(
                    "UPDATE discussion_questions SET status='archived' WHERE status='active' AND (topic=? OR question LIKE ? ESCAPE '\\')",
                    (clean, like_pat),
                )
                self.db.execute(
                    "UPDATE candidate_items SET status='blocked' WHERE status='unread' AND (title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\')",
                    (like_pat, like_pat),
                )
            self.db.execute(
                "UPDATE horizon_intents SET status='cancelled',updated_at=? WHERE status IN ('pending','claimed') AND (?='' OR identity=?) AND instr(lower(topic),lower(?))>0",
                (stamp, norm, norm, clean),
            )
        return self.list_blocked_topics(norm)

    def unblock_topic(self, topic: str, identity: str = "") -> list[str]:
        clean = clean_text(topic, "topic", 80)
        norm = normalize_identity(identity) if identity else ""
        with self.lock, self.db:
            if norm:
                self.db.execute(
                    "DELETE FROM blocked_topics WHERE topic=? AND identity=?",
                    (clean, norm),
                )
            else:
                self.db.execute(
                    "DELETE FROM blocked_topics WHERE topic=? AND identity=''", (clean,)
                )
        return self.list_blocked_topics(norm)

    def is_topic_blocked(self, text_value: str, identity: str = "") -> bool:
        haystack = str(text_value or "").casefold()
        if not haystack:
            return False
        for blocked in self.list_blocked_topics(identity):
            if blocked.casefold() in haystack:
                return True
        return False

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    @staticmethod
    def _format_source_row(row: sqlite3.Row, *, mask: bool = True) -> dict[str, Any]:
        raw_cfg = json.loads(row["config_json"] or "{}")
        return {
            "source_id": row["source_id"],
            "source_kind": row["source_kind"],
            "identity": row["identity"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "status": row["status"],
            "last_error": row["last_error"],
            "last_poll_at": float(row["last_poll_at"]),
            "cooldown_until": float(row["cooldown_until"]),
            "config": mask_secrets(raw_cfg) if mask else raw_cfg,
            "cursor": json.loads(row["cursor_json"] or "{}"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def create_source(
        self,
        *,
        identity: str,
        source_kind: str,
        name: str,
        config: dict[str, Any],
        enabled: bool = True,
        now: float | None = None,
    ) -> dict[str, Any]:
        if source_kind not in SOURCE_KINDS:
            raise ValueError(f"unsupported source_kind: {source_kind}")
        norm_id = normalize_identity(identity)
        clean_name = clean_text(name, "name", 120)
        stamp = float(now if now is not None else time.time())
        source_id = "src_" + uuid.uuid4().hex[:16]
        with self.lock, self.db:
            self.db.execute(
                """
                INSERT INTO sources(
                    source_id, identity, source_kind, name, enabled,
                    config_json, cursor_json, status, last_error,
                    last_poll_at, cooldown_until, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '{}', 'idle', '', 0, 0, ?, ?)
                """,
                (
                    source_id,
                    norm_id,
                    source_kind,
                    clean_name,
                    int(bool(enabled)),
                    encode_json(config or {}),
                    stamp,
                    stamp,
                ),
            )
            row = self.db.execute(
                "SELECT * FROM sources WHERE source_id=?", (source_id,)
            ).fetchone()
        return self._format_source_row(row, mask=True)

    def get_source(self, source_id: str, *, mask: bool = True) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM sources WHERE source_id=?", (str(source_id),)
            ).fetchone()
        return self._format_source_row(row, mask=mask) if row else None

    def get_merged_config_for_update(
        self, source_id: str, incoming: dict[str, Any], *, expected_identity: str = ""
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM sources WHERE source_id=?", (str(source_id),)
            ).fetchone()
        if row is None:
            raise ValueError("信息源不存在")
        check_review_target(row, expected_identity)
        current = self._format_source_row(row, mask=False)
        merged = merge_masked_secrets(current["config"], incoming)
        return current, merged

    def update_source(
        self,
        source_id: str,
        *,
        name: str | None = None,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
        identity: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        stamp = float(now if now is not None else time.time())
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM sources WHERE source_id=?", (str(source_id),)
            ).fetchone()
            if row is None:
                raise ValueError("信息源不存在")
            new_name = (
                clean_text(name, "name", 120) if name is not None else row["name"]
            )
            new_enabled = (
                int(bool(enabled)) if enabled is not None else int(row["enabled"])
            )
            new_identity = normalize_identity(identity) if identity else row["identity"]
            new_cfg = encode_json(config) if config is not None else row["config_json"]
            if (
                new_identity != row["identity"]
                or new_cfg != row["config_json"]
                or not new_enabled
            ):
                self.cancel_source_work(source_id, stamp)
            self.db.execute(
                """
                UPDATE sources
                SET identity=?, name=?, enabled=?, config_json=?, updated_at=?
                WHERE source_id=?
                """,
                (new_identity, new_name, new_enabled, new_cfg, stamp, str(source_id)),
            )
            updated = self.db.execute(
                "SELECT * FROM sources WHERE source_id=?", (str(source_id),)
            ).fetchone()
        return self._format_source_row(updated, mask=True)

    def toggle_source(
        self,
        source_id: str,
        enabled: bool | None = None,
        now: float | None = None,
        *,
        expected_identity: str = "",
    ) -> dict[str, Any]:
        stamp = float(now if now is not None else time.time())
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM sources WHERE source_id=?", (str(source_id),)
            ).fetchone()
            if row is None:
                raise ValueError("信息源不存在")
            check_review_target(row, expected_identity)
            target = (not bool(row["enabled"])) if enabled is None else bool(enabled)
            if not target:
                self.cancel_source_work(source_id, stamp)
            self.db.execute(
                "UPDATE sources SET enabled=?, updated_at=? WHERE source_id=?",
                (int(target), stamp, str(source_id)),
            )
            updated = self.db.execute(
                "SELECT * FROM sources WHERE source_id=?", (str(source_id),)
            ).fetchone()
        return self._format_source_row(updated, mask=True)

    def delete_source(self, source_id: str, *, expected_identity: str = "") -> bool:
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT identity FROM sources WHERE source_id=?", (str(source_id),)
            ).fetchone()
            if row is None:
                return False
            check_review_target(row, expected_identity)
            self.cancel_source_work(source_id, time.time())
            cur = self.db.execute(
                "DELETE FROM sources WHERE source_id=?", (str(source_id),)
            )
            self.db.execute(
                "UPDATE candidate_items SET status='skipped' WHERE source_id=? AND status='unread'",
                (str(source_id),),
            )
        return cur.rowcount > 0

    def list_sources(
        self, identity: str = "", *, enabled_only: bool = False, mask: bool = True
    ) -> list[dict[str, Any]]:
        norm = normalize_identity(identity) if identity else ""
        query = "SELECT * FROM sources WHERE 1=1"
        params: list[Any] = []
        if norm:
            query += " AND identity=?"
            params.append(norm)
        if enabled_only:
            query += " AND enabled=1"
        query += " ORDER BY created_at DESC"
        with self.lock:
            rows = self.db.execute(query, params).fetchall()
        return [self._format_source_row(r, mask=mask) for r in rows]

    def ensure_manual_source(
        self, identity: str, now: float | None = None
    ) -> dict[str, Any]:
        norm = normalize_identity(identity)
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM sources WHERE identity=? AND source_kind='manual' ORDER BY created_at ASC LIMIT 1",
                (norm,),
            ).fetchone()
            if row is not None:
                return self._format_source_row(row, mask=False)
            return self.create_source(
                identity=norm,
                source_kind="manual",
                name="管理员投喂剪报",
                config={"mode": "manual"},
                enabled=True,
                now=now,
            )

    def update_source_poll_state(
        self,
        source_id: str,
        *,
        next_cursor: dict[str, Any] | None = None,
        cooldown_until: float = 0.0,
        status: str = "ok",
        last_error: str = "",
        now: float | None = None,
        expected_source: dict[str, Any] | None = None,
    ) -> None:
        stamp = float(now if now is not None else time.time())
        with self.lock, self.db:
            if expected_source is not None:
                self.check_poll_source(expected_source)
            row = self.db.execute(
                "SELECT cursor_json FROM sources WHERE source_id=?", (str(source_id),)
            ).fetchone()
            if row is None:
                return
            cursor_str = (
                encode_json(next_cursor)
                if isinstance(next_cursor, dict)
                else row["cursor_json"]
            )
            self.db.execute(
                """
                UPDATE sources
                SET cursor_json=?, cooldown_until=?, status=?, last_error=?, last_poll_at=?, updated_at=?
                WHERE source_id=?
                """,
                (
                    cursor_str,
                    float(cooldown_until or 0.0),
                    str(status or "ok")[:64],
                    strip_raw_urls(str(last_error or ""))[:240],
                    stamp,
                    stamp,
                    str(source_id),
                ),
            )

    # ------------------------------------------------------------------
    # Books & Serial Chapters
    # ------------------------------------------------------------------

    @staticmethod
    def _format_book_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "book_id": row["book_id"],
            "identity": row["identity"],
            "source_id": row["source_id"],
            "source_kind": row["source_kind"],
            "provider": row["provider"],
            "external_id": row["external_id"],
            "title": row["title"],
            "creator": row["creator"],
            "chapter_cursor": int(row["chapter_cursor"]),
            "total_chapters": int(row["total_chapters"]),
            "status": row["status"],
            "progress_verified": bool(row["progress_verified"]),
            "reading_label": (
                "待核查"
                if not row["progress_verified"]
                else "书架待读"
                if row["status"] == "reading" and row["chapter_cursor"] == 0
                else "正在阅读"
                if row["status"] == "reading"
                else "读完"
                if row["status"] == "finished"
                else "弃书"
            ),
            "last_reflection": row["last_reflection"],
            "raw_url": row["raw_url"],
            "meta": json.loads(row["meta_json"] or "{}"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _strip_ebook_prefix(external_id: str, provider: str) -> str:
        s = str(external_id or "").strip()
        prov = str(provider or "").strip().lower()
        if prov and s.lower().startswith(f"{prov}:"):
            s = s[len(prov) + 1 :]
            if ":" in s:
                head, tail = s.split(":", 1)
                if (
                    re.fullmatch(r"[a-z]{2,8}(?:-[a-z0-9]{2,8})?", head.lower())
                    and tail
                ):
                    s = tail
        return s.strip()

    def create_book_with_chapters(
        self,
        *,
        identity: str,
        title: str,
        creator: str = "",
        chapters: list[dict[str, Any]],
        provider: str = "local",
        source_id: str = "",
        external_id: str = "",
        raw_url: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        norm_id = normalize_identity(identity)
        clean_title = strip_raw_urls(clean_text(title, "title", 160))
        clean_creator = strip_raw_urls(str(creator or "").strip())[:80]
        if not chapters:
            raise ValueError("电子书内容为空，未能解析出有效章节")
        stamp = float(now if now is not None else time.time())
        book_id = "book_" + uuid.uuid4().hex[:16]
        normalized_chapters: list[tuple[str, int, str, str]] = []
        for idx, ch in enumerate(chapters):
            raw_label = str(ch.get("label") or f"第 {idx + 1} 节")
            label = (
                sanitize_untrusted_text(raw_label, max_chars=80)["text"]
                or f"第 {idx + 1} 节"
            )
            text_res = sanitize_untrusted_text(
                str(ch.get("text") or ""), max_chars=4000
            )
            if not text_res["text"]:
                continue
            normalized_chapters.append(
                (book_id, len(normalized_chapters), label, text_res["text"])
            )
        if not normalized_chapters:
            raise ValueError("电子书章节经安全清洗后无可用文本")

        with self.lock, self.db:
            self.db.execute(
                """
                INSERT INTO books(
                    book_id, identity, source_id, source_kind, provider, external_id,
                    title, creator, chapter_cursor, total_chapters, status,
                    last_reflection, raw_url, meta_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'ebook', ?, ?, ?, ?, 0, ?, 'reading', '', ?, '{}', ?, ?)
                """,
                (
                    book_id,
                    norm_id,
                    str(source_id or ""),
                    str(provider or "local")[:32],
                    str(external_id or "")[:200],
                    clean_title,
                    clean_creator,
                    len(normalized_chapters),
                    sanitize_admin_url(raw_url),
                    stamp,
                    stamp,
                ),
            )
            self.db.executemany(
                "INSERT INTO book_chapters(book_id, chapter_index, chapter_label, content_text) VALUES (?, ?, ?, ?)",
                normalized_chapters,
            )
            row = self.db.execute(
                "SELECT * FROM books WHERE book_id=?", (book_id,)
            ).fetchone()
        return self._format_book_row(row)

    def ensure_shelf_book(
        self,
        *,
        identity: str,
        source_id: str,
        provider: str,
        external_id: str,
        title: str,
        creator: str = "",
        total_chapters: int = 0,
        raw_url: str = "",
        meta: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        norm_id = normalize_identity(identity)
        prov = str(provider or "ebook").strip()[:32] or "ebook"
        clean_title = strip_raw_urls(str(title or "").strip())[:160] or "未命名书目"
        clean_creator = strip_raw_urls(str(creator or "").strip())[:80]
        clean_url = sanitize_admin_url(raw_url)
        ext = str(external_id or clean_title)[:200]
        ext_stripped = self._strip_ebook_prefix(ext, prov)
        safe_meta = mask_secrets(meta) if isinstance(meta, dict) else {}
        stamp = float(now if now is not None else time.time())
        with self.lock, self.db:
            existing = self.db.execute(
                "SELECT * FROM books WHERE identity=? AND (source_id=? AND external_id=? OR title=? AND provider=?)",
                (norm_id, str(source_id), ext, clean_title, prov),
            ).fetchone()
            if existing is None and source_id:
                source_rows = self.db.execute(
                    "SELECT * FROM books WHERE identity=? AND source_id=? ORDER BY created_at ASC",
                    (norm_id, str(source_id)),
                ).fetchall()
                if prov in {"wikisource", "gutendex"} and source_rows:
                    existing = source_rows[0]
                elif ext_stripped:
                    for cand_row in source_rows:
                        if (
                            self._strip_ebook_prefix(cand_row["external_id"], prov)
                            == ext_stripped
                        ):
                            existing = cand_row
                            break
            if existing is not None:
                old_meta = json.loads(existing["meta_json"] or "{}")
                merged_meta = {**old_meta, **safe_meta}
                next_total = max(
                    int(existing["total_chapters"]), max(0, int(total_chapters or 0))
                )
                next_creator = existing["creator"] or clean_creator
                next_url = existing["raw_url"] or clean_url
                next_title = existing["title"]
                if (
                    clean_title
                    and clean_title != "未命名书目"
                    and (
                        not next_title
                        or next_title in {"未命名书目", "ebook 订阅源"}
                        or next_title.isdigit()
                    )
                ):
                    next_title = clean_title
                self.db.execute(
                    """
                    UPDATE books
                    SET title=?, creator=?, total_chapters=?, raw_url=?, meta_json=?, updated_at=?
                    WHERE book_id=?
                    """,
                    (
                        next_title,
                        next_creator,
                        next_total,
                        next_url,
                        encode_json(merged_meta),
                        stamp,
                        existing["book_id"],
                    ),
                )
                existing = self.db.execute(
                    "SELECT * FROM books WHERE book_id=?", (existing["book_id"],)
                ).fetchone()
                return self._format_book_row(existing)
            book_id = "book_" + uuid.uuid4().hex[:16]
            self.db.execute(
                """
                INSERT INTO books(
                    book_id, identity, source_id, source_kind, provider, external_id,
                    title, creator, chapter_cursor, total_chapters, status,
                    last_reflection, raw_url, meta_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'ebook', ?, ?, ?, ?, 0, ?, 'reading', '', ?, ?, ?, ?)
                """,
                (
                    book_id,
                    norm_id,
                    str(source_id),
                    prov,
                    ext,
                    clean_title,
                    clean_creator,
                    max(0, int(total_chapters or 0)),
                    clean_url,
                    encode_json(safe_meta),
                    stamp,
                    stamp,
                ),
            )
            row = self.db.execute(
                "SELECT * FROM books WHERE book_id=?", (book_id,)
            ).fetchone()
        return self._format_book_row(row)

    def list_books(
        self, identity: str = "", *, status: str = ""
    ) -> list[dict[str, Any]]:
        norm = normalize_identity(identity) if identity else ""
        query = "SELECT * FROM books WHERE 1=1"
        params: list[Any] = []
        if norm:
            query += " AND identity=?"
            params.append(norm)
        if status in BOOK_STATUSES:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY CASE status WHEN 'reading' THEN 0 WHEN 'finished' THEN 1 ELSE 2 END, updated_at DESC"
        with self.lock:
            rows = self.db.execute(query, params).fetchall()
        return [self._format_book_row(r) for r in rows]

    def get_book(self, book_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM books WHERE book_id=?", (str(book_id),)
            ).fetchone()
        return self._format_book_row(row) if row else None

    def get_book_chapter(
        self, book_id: str, chapter_index: int
    ) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM book_chapters WHERE book_id=? AND chapter_index=?",
                (str(book_id), int(chapter_index)),
            ).fetchone()
        if row is None:
            return None
        return {
            "book_id": row["book_id"],
            "chapter_index": int(row["chapter_index"]),
            "chapter_label": row["chapter_label"],
            "content_text": row["content_text"],
        }

    def store_book_chapters(
        self, book_id: str, chapters: list[dict[str, Any]], now: float | None = None
    ) -> None:
        if not chapters:
            return
        stamp = float(now if now is not None else time.time())
        rows: list[tuple[str, int, str, str]] = []
        for idx, ch in enumerate(chapters):
            c_idx = int(ch.get("index", idx))
            label = sanitize_untrusted_text(
                str(ch.get("label") or f"第 {c_idx + 1} 节"), max_chars=80
            )["text"]
            text = sanitize_untrusted_text(str(ch.get("text") or ""), max_chars=4000)[
                "text"
            ]
            if text:
                rows.append((str(book_id), c_idx, label or f"第 {c_idx + 1} 节", text))
        if not rows:
            return
        with self.lock, self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO book_chapters(book_id, chapter_index, chapter_label, content_text) VALUES (?, ?, ?, ?)",
                rows,
            )
            total = self.db.execute(
                "SELECT COUNT(*) FROM book_chapters WHERE book_id=?",
                (str(book_id),),
            ).fetchone()[0]
            self.db.execute(
                "UPDATE books SET total_chapters=MAX(total_chapters, ?), updated_at=? WHERE book_id=?",
                (int(total), stamp, str(book_id)),
            )

    def update_book_status(
        self,
        book_id: str,
        status: str,
        *,
        chapter_cursor: int | None = None,
        reflection: str | None = None,
        now: float | None = None,
        expected_identity: str = "",
    ) -> dict[str, Any]:
        if status not in BOOK_STATUSES:
            raise ValueError(f"invalid book status: {status}")
        stamp = float(now if now is not None else time.time())
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM books WHERE book_id=?", (str(book_id),)
            ).fetchone()
            if row is None:
                raise ValueError("书籍不存在")
            check_review_target(row, expected_identity)
            cursor = (
                int(chapter_cursor)
                if chapter_cursor is not None
                else int(row["chapter_cursor"])
            )
            cursor = max(0, cursor)
            refl = (
                strip_raw_urls(reflection)[:160]
                if reflection is not None
                else row["last_reflection"]
            )
            self.db.execute(
                "UPDATE books SET status=?, chapter_cursor=?, last_reflection=?, updated_at=? WHERE book_id=?",
                (status, cursor, refl, stamp, str(book_id)),
            )
            updated = self.db.execute(
                "SELECT * FROM books WHERE book_id=?", (str(book_id),)
            ).fetchone()
        return self._format_book_row(updated)

    # ------------------------------------------------------------------
    # Candidate Pool
    # ------------------------------------------------------------------

    @staticmethod
    def _format_candidate_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "candidate_id": row["candidate_id"],
            "source_id": row["source_id"],
            "external_id": row["external_id"],
            "identity": row["identity"],
            "source_kind": row["source_kind"],
            "book_id": row["book_id"],
            "chapter_index": row["chapter_index"],
            "total_chapters": row["total_chapters"],
            "title": row["title"],
            "creator": row["creator"],
            "bvid": row["bvid"],
            "chapter_label": row["chapter_label"],
            "summary": row["summary"],
            "raw_url": row["raw_url"],
            "published_at": float(row["published_at"]),
            "content_origin": row["content_origin"],
            "content_ready": bool(row["content_ready"]),
            "full_text": row["full_text"],
            "fingerprint": row["fingerprint"],
            "status": row["status"],
            "meta": json.loads(row["meta_json"] or "{}"),
            "created_at": float(row["created_at"]),
        }

    def insert_candidates(
        self,
        raw_items: list[dict[str, Any]],
        *,
        source_id: str,
        source_kind: str,
        identity: str,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        stamp = float(now if now is not None else time.time())
        norm_id = normalize_identity(identity)
        blocked_list = [b.casefold() for b in self.list_blocked_topics(norm_id)]
        inserted: list[dict[str, Any]] = []

        with self.lock, self.db:
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                clean = sanitize_candidate_item(
                    raw,
                    source_id=source_id,
                    source_kind=source_kind,
                    identity=norm_id,
                )
                blob = f"{clean['title']}\n{clean['summary']}\n{clean['full_text']}".casefold()
                is_blocked = clean["blocked"] or any(b in blob for b in blocked_list)
                status = "blocked" if is_blocked else "unread"

                # Dedup by (source_id, external_id) and content fingerprint per identity
                dup = self.db.execute(
                    """
                    SELECT candidate_id FROM candidate_items
                    WHERE (source_id=? AND external_id=?)
                       OR (identity=? AND fingerprint=? AND status IN ('unread', 'digested', 'selected'))
                    LIMIT 1
                    """,
                    (
                        clean["source_id"],
                        clean["external_id"],
                        clean["identity"],
                        clean["fingerprint"],
                    ),
                ).fetchone()
                if dup is not None:
                    continue

                # If ebook candidate carries book metadata, register/reconcile shelf book id (`book_<uuid>`)
                book_id = clean["book_id"]
                if source_kind == "ebook" and (
                    book_id
                    or clean["total_chapters"]
                    or clean["chapter_index"] is not None
                ):
                    existing_shelf = (
                        self.db.execute(
                            "SELECT book_id FROM books WHERE book_id=?", (book_id,)
                        ).fetchone()
                        if book_id
                        else None
                    )
                    shelf_meta = {
                        k: v for k, v in (clean["meta"] or {}).items() if k != "subpage"
                    }
                    book_title = (
                        str(clean["meta"].get("book_title") or "").strip()
                        or clean["title"]
                    )
                    if existing_shelf is None:
                        shelf = self.ensure_shelf_book(
                            identity=clean["identity"],
                            source_id=clean["source_id"],
                            provider=str(clean["meta"].get("provider") or "ebook"),
                            external_id=str(
                                clean["meta"].get("book_external_id")
                                or book_id
                                or book_title
                            ),
                            title=book_title,
                            creator=clean["creator"],
                            total_chapters=int(clean["total_chapters"] or 0),
                            raw_url=clean["raw_url"],
                            meta=shelf_meta,
                            now=stamp,
                        )
                        book_id = shelf["book_id"]
                    elif clean["total_chapters"] or shelf_meta:
                        old_row = self.db.execute(
                            "SELECT * FROM books WHERE book_id=?", (book_id,)
                        ).fetchone()
                        if old_row is not None:
                            merged_meta = {
                                **json.loads(old_row["meta_json"] or "{}"),
                                **shelf_meta,
                            }
                            self.db.execute(
                                "UPDATE books SET total_chapters=MAX(total_chapters, ?), meta_json=?, updated_at=? WHERE book_id=?",
                                (
                                    int(clean["total_chapters"] or 0),
                                    encode_json(merged_meta),
                                    stamp,
                                    book_id,
                                ),
                            )

                cid = "cand_" + uuid.uuid4().hex[:16]
                self.db.execute(
                    """
                    INSERT OR IGNORE INTO candidate_items(
                        candidate_id, source_id, external_id, identity, source_kind,
                        book_id, chapter_index, total_chapters, title, creator,
                        bvid, chapter_label, summary, raw_url, published_at,
                        content_ready, full_text, fingerprint, status, meta_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        clean["source_id"],
                        clean["external_id"],
                        clean["identity"],
                        clean["source_kind"],
                        book_id,
                        clean["chapter_index"],
                        clean["total_chapters"],
                        clean["title"],
                        clean["creator"],
                        clean["bvid"],
                        clean["chapter_label"],
                        clean["summary"],
                        clean["raw_url"],
                        clean["published_at"],
                        int(clean["content_ready"]),
                        clean["full_text"],
                        clean["fingerprint"],
                        status,
                        encode_json(clean["meta"]),
                        stamp,
                    ),
                )
                row = self.db.execute(
                    "SELECT * FROM candidate_items WHERE candidate_id=?", (cid,)
                ).fetchone()
                if row is not None:
                    self.db.execute(
                        "UPDATE candidate_items SET content_origin=? WHERE candidate_id=?",
                        (
                            str(
                                raw.get("content_origin")
                                or clean["meta"].get("content_origin")
                                or "unknown"
                            ),
                            cid,
                        ),
                    )
                if row is not None and status == "unread":
                    inserted.append(self._format_candidate_row(row))
        return inserted

    def list_unread_candidates(
        self, identity: str, *, limit: int = 12
    ) -> list[dict[str, Any]]:
        norm = normalize_identity(identity)
        with self.lock, self.db:
            rows = self.db.execute(
                """
                WITH ranked AS (
                    SELECT c.*, ROW_NUMBER() OVER(PARTITION BY c.source_id ORDER BY c.pinned DESC,c.last_offered_at,c.created_at) rn
                    FROM candidate_items c JOIN sources s ON s.source_id=c.source_id
                    WHERE c.identity=? AND s.identity=c.identity AND s.enabled=1 AND c.status='unread' AND c.archived=0
                      AND (c.book_id='' OR EXISTS(SELECT 1 FROM books b WHERE b.book_id=c.book_id AND b.status='reading'
                           AND b.progress_verified=1 AND b.chapter_cursor=c.chapter_index))
                      AND NOT EXISTS(SELECT 1 FROM proposals p WHERE p.candidate_id=c.candidate_id AND p.state IN ('pending_review','held'))
                ) SELECT * FROM ranked ORDER BY pinned DESC,rn,last_offered_at,created_at LIMIT ?
            """,
                (norm, max(1, min(50, int(limit)))),
            ).fetchall()
            self.db.executemany(
                "UPDATE candidate_items SET last_offered_at=? WHERE candidate_id=?",
                [(time.time(), r["candidate_id"]) for r in rows],
            )
        return [self._format_candidate_row(r) for r in rows]

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM candidate_items WHERE candidate_id=?",
                (str(candidate_id),),
            ).fetchone()
        return self._format_candidate_row(row) if row else None

    def update_candidate_content(
        self,
        candidate_id: str,
        *,
        full_text: str,
        chapter_label: str = "",
        total_chapters: int | None = None,
        content_origin: str = "unknown",
        max_chars: int | None = None,
    ) -> dict[str, Any] | None:
        effective_max = (
            max(4000, min(8000, int(max_chars)))
            if isinstance(max_chars, int)
            else (
                6000
                if str(content_origin or "").startswith("bilibili_")
                and str(content_origin or "") != "bilibili_dynamic"
                else 4000
            )
        )
        san = sanitize_untrusted_text(full_text, max_chars=effective_max)
        ch_label = (
            sanitize_untrusted_text(chapter_label, max_chars=80)["text"]
            if chapter_label
            else ""
        )
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM candidate_items WHERE candidate_id=?",
                (str(candidate_id),),
            ).fetchone()
            if row is None:
                return None
            new_label = ch_label or row["chapter_label"]
            new_total = (
                int(total_chapters)
                if isinstance(total_chapters, int) and total_chapters > 0
                else row["total_chapters"]
            )
            self.db.execute(
                """
                UPDATE candidate_items
                SET full_text=?, content_ready=1, chapter_label=?, total_chapters=?
                WHERE candidate_id=?
                """,
                (san["text"], new_label, new_total, str(candidate_id)),
            )
            self.db.execute(
                "UPDATE candidate_items SET content_origin=? WHERE candidate_id=?",
                (content_origin[:80], candidate_id),
            )
            if row["book_id"] and isinstance(new_total, int) and new_total > 0:
                self.db.execute(
                    "UPDATE books SET total_chapters=MAX(total_chapters, ?) WHERE book_id=?",
                    (int(new_total), str(row["book_id"])),
                )
            updated = self.db.execute(
                "SELECT * FROM candidate_items WHERE candidate_id=?",
                (str(candidate_id),),
            ).fetchone()
        return self._format_candidate_row(updated)

    def set_candidate_status(
        self, candidate_id: str, status: str, *, reason: str = ""
    ) -> None:
        if not candidate_id:
            return
        clean_reason = strip_raw_urls(str(reason or "")).strip()[:160]
        with self.lock, self.db:
            if clean_reason:
                row = self.db.execute(
                    "SELECT meta_json FROM candidate_items WHERE candidate_id=?",
                    (str(candidate_id),),
                ).fetchone()
                meta: dict[str, Any] = {}
                if row is not None:
                    try:
                        parsed = json.loads(row["meta_json"] or "{}")
                        if isinstance(parsed, dict):
                            meta = parsed
                    except Exception:
                        meta = {}
                meta["skip_reason"] = clean_reason
                self.db.execute(
                    "UPDATE candidate_items SET status=?, meta_json=? WHERE candidate_id=?",
                    (str(status), encode_json(mask_secrets(meta)), str(candidate_id)),
                )
            else:
                self.db.execute(
                    "UPDATE candidate_items SET status=? WHERE candidate_id=?",
                    (str(status), str(candidate_id)),
                )

    # ------------------------------------------------------------------
    # Proposals, Reviews, Reading Logs, Staged Interests & Questions
    # ------------------------------------------------------------------

    @staticmethod
    def _format_proposal_row(row: sqlite3.Row) -> dict[str, Any]:
        evidence = json.loads(row["evidence_json"] or "{}")
        media_view = _extract_media_view(evidence)
        return {
            "proposal_id": row["proposal_id"],
            "identity": row["identity"],
            "candidate_id": row["candidate_id"],
            "book_id": row["book_id"],
            "chapter_index": row["chapter_index"],
            "source_kind": row["source_kind"],
            "state": row["state"],
            "title": row["title"],
            "creator": row["creator"],
            "bvid": row["bvid"],
            "chapter_label": row["chapter_label"],
            "raw_url": row["raw_url"],
            "digest": row["digest"],
            "reflection": row["reflection"],
            "staged_interest": json.loads(row["staged_interest_json"] or "{}"),
            "questions": json.loads(row["questions_json"] or "[]"),
            "review_mode": row["review_mode"],
            "review_verdict": row["review_verdict"],
            "review_reason": row["review_reason"],
            "review_risks": json.loads(row["review_risks_json"] or "[]"),
            "reviewed_by": row["reviewed_by"],
            "revision": int(row["revision"]),
            "evidence": evidence,
            "watch_mode": media_view["watch_mode"],
            "content_origin": media_view["content_origin"],
            "visual_observations": media_view["visual_observations"],
            "media_meta": media_view["media_meta"],
            "task_id": row["task_id"],
            "applied_log_id": row["applied_log_id"],
            "applied_interest_id": row["applied_interest_id"],
            "applied_question_ids": json.loads(
                row["applied_question_ids_json"] or "[]"
            ),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def create_proposal(
        self,
        *,
        identity: str,
        source_kind: str,
        title: str,
        creator: str = "",
        bvid: str = "",
        chapter_label: str = "",
        raw_url: str = "",
        digest: str,
        reflection: str,
        staged_interest: dict[str, Any] | None = None,
        questions: list[dict[str, Any]] | None = None,
        candidate_id: str = "",
        book_id: str = "",
        chapter_index: int | None = None,
        review_mode: str = "llm",
        evidence: dict | None = None,
        task_id: str = "",
        continuity: dict | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        norm_id = normalize_identity(identity)
        stamp = float(now if now is not None else time.time())
        pid = "prop_" + uuid.uuid4().hex[:16]
        cit = sanitize_citation(
            {
                "title": title,
                "creator": creator,
                "bvid": bvid,
                "chapter_label": chapter_label,
            }
        )
        clean_digest = strip_raw_urls(
            sanitize_untrusted_text(digest, max_chars=200)["text"]
        )[:200]
        clean_refl = strip_raw_urls(
            sanitize_untrusted_text(reflection, max_chars=150)["text"]
        )[:150]
        if not clean_digest:
            raise ValueError("digest cannot be empty")
        if not clean_refl:
            clean_refl = clean_digest[:100]

        si = staged_interest if isinstance(staged_interest, dict) else {}
        clean_topic = strip_raw_urls(
            sanitize_untrusted_text(str(si.get("topic") or ""), max_chars=48)["text"]
        )[:48]
        clean_si_summary = strip_raw_urls(
            sanitize_untrusted_text(
                str(si.get("summary") or si.get("stance_or_curiosity") or ""),
                max_chars=140,
            )["text"]
        )[:140]
        normalized_si = (
            {"topic": clean_topic, "summary": clean_si_summary} if clean_topic else {}
        )

        normalized_qs: list[dict[str, str]] = []
        for q in (questions or [])[:2]:
            if isinstance(q, str):
                q_topic = clean_topic or cit["title"][:32]
                q_text = q
            elif isinstance(q, dict):
                q_topic = str(q.get("topic") or clean_topic or cit["title"][:32])
                q_text = str(q.get("question") or "")
            else:
                continue
            q_topic_clean = strip_raw_urls(
                sanitize_untrusted_text(q_topic, max_chars=48)["text"]
            )[:48]
            q_text_clean = strip_raw_urls(
                sanitize_untrusted_text(q_text, max_chars=120)["text"]
            )[:120]
            if q_text_clean and not self.is_topic_blocked(
                f"{q_topic_clean} {q_text_clean}", norm_id
            ):
                normalized_qs.append(
                    {"topic": q_topic_clean or "近期见闻", "question": q_text_clean}
                )

        with self.lock, self.db:
            if task_id:
                task = self.db.execute(
                    "SELECT * FROM reading_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if (
                    not task
                    or task["status"] != "running"
                    or task["identity"] != norm_id
                ):
                    raise ValueError("reading_task_changed")
                if book_id and not self.chapter_available(book_id, chapter_index):
                    raise ValueError("reading_chapter_changed")
            if candidate_id:
                self.db.execute(
                    "UPDATE candidate_items SET status='selected' WHERE candidate_id=? AND status='unread'",
                    (str(candidate_id),),
                )
            self.db.execute(
                """
                INSERT INTO proposals(
                    proposal_id, identity, candidate_id, book_id, chapter_index,
                    source_kind, state, title, creator, bvid, chapter_label, raw_url,
                    digest, reflection, staged_interest_json, questions_json,
                    review_mode, review_verdict, review_reason, review_risks_json,
                    reviewed_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', '[]', '', ?, ?)
                """,
                (
                    pid,
                    norm_id,
                    str(candidate_id or ""),
                    str(book_id or ""),
                    chapter_index,
                    str(source_kind),
                    cit["title"] or "未命名内容",
                    cit["creator"],
                    cit["bvid"],
                    cit["chapter_label"],
                    sanitize_admin_url(raw_url),
                    clean_digest,
                    clean_refl,
                    encode_json(normalized_si),
                    encode_json(normalized_qs),
                    str(review_mode or "llm"),
                    stamp,
                    stamp,
                ),
            )
            self.db.execute(
                "UPDATE proposals SET evidence_json=?,task_id=? WHERE proposal_id=?",
                (encode_json(evidence or {}), task_id, pid),
            )
            if task_id:
                self.db.execute(
                    "UPDATE reading_tasks SET status='pending_review',proposal_id=?,context_json=?,updated_at=? WHERE task_id=?",
                    (pid, encode_json(continuity or {}), stamp, task_id),
                )
            self.db.execute(
                "INSERT INTO review_jobs(proposal_id,kind,attempts,next_at) VALUES(?,?,0,?)",
                (pid, "human" if review_mode == "human" else "retry", stamp + 60),
            )
            row = self.db.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (pid,)
            ).fetchone()
        return self._format_proposal_row(row)

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (str(proposal_id),)
            ).fetchone()
        return self._format_proposal_row(row) if row else None

    def list_proposals(
        self,
        *,
        identity: str = "",
        state: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        norm = normalize_identity(identity) if identity else ""
        mapped_state = "pending_review" if state == "pending" else state
        where = "WHERE 1=1"
        params: list[Any] = []
        if norm:
            where += " AND identity=?"
            params.append(norm)
        if mapped_state in PROPOSAL_STATES:
            where += " AND state=?"
            params.append(mapped_state)
        p = max(1, min(10000, int(page or 1)))
        ps = max(1, min(100, int(page_size or 20)))
        with self.lock:
            total = self.db.execute(
                f"SELECT COUNT(*) FROM proposals {where}", params
            ).fetchone()[0]
            p = min(p, max(1, (total + ps - 1) // ps))
            rows = self.db.execute(
                f"SELECT * FROM proposals {where} ORDER BY created_at DESC, proposal_id DESC LIMIT ? OFFSET ?",
                [*params, ps, (p - 1) * ps],
            ).fetchall()
            audits_map: dict[str, list[dict[str, Any]]] = {}
            task_ctx_map: dict[str, dict[str, Any]] = {}
            continuity_cache: dict[tuple[str, int], dict[str, Any]] = {}
            pids = [str(r["proposal_id"]) for r in rows]
            if pids:
                ph = ",".join("?" for _ in pids)
                for a in self.db.execute(
                    f"SELECT proposal_id,from_state,to_state,actor,reason,created_at FROM review_audit WHERE proposal_id IN ({ph}) ORDER BY audit_id",
                    pids,
                ).fetchall():
                    audits_map.setdefault(str(a["proposal_id"]), []).append(
                        {
                            "from_state": a["from_state"],
                            "to_state": a["to_state"],
                            "actor": a["actor"],
                            "reason": a["reason"],
                            "created_at": a["created_at"],
                        }
                    )
                tids = list(
                    dict.fromkeys(
                        str(r["task_id"])
                        for r in rows
                        if r["book_id"] and r["task_id"]
                    )
                )
                if tids:
                    tph = ",".join("?" for _ in tids)
                    for t in self.db.execute(
                        f"SELECT task_id,context_json FROM reading_tasks WHERE task_id IN ({tph})",
                        tids,
                    ).fetchall():
                        task_ctx_map[str(t["task_id"])] = (
                            json.loads(t["context_json"]) if t["context_json"] else {}
                        )
                for r in rows:
                    bid = str(r["book_id"] or "")
                    if not bid:
                        continue
                    cidx = int(r["chapter_index"] or 0)
                    key = (bid, cidx)
                    if key not in continuity_cache:
                        continuity_cache[key] = self.continuity(bid, cidx)
        items_out: list[dict[str, Any]] = []
        for r in rows:
            pid = str(r["proposal_id"])
            bid = str(r["book_id"] or "")
            if bid:
                cidx = int(r["chapter_index"] or 0)
                cont = {
                    "proposed": task_ctx_map.get(str(r["task_id"] or ""), {}),
                    "previous": continuity_cache.get((bid, cidx), {}),
                }
            else:
                cont = {"proposed": {}, "previous": {}}
            items_out.append(
                {
                    **self._format_proposal_row(r),
                    "continuity": cont,
                    "audit": audits_map.get(pid, []),
                }
            )
        return {
            "items": items_out,
            "page": p,
            "page_size": ps,
            "total": int(total),
        }

    def record_review_verdict(
        self,
        proposal_id: str,
        *,
        state: str,
        verdict: str,
        reason: str,
        risks: list[str] | None = None,
        reviewed_by: str = "llm",
        expected_revision: int | None = None,
        expected_identity: str = "",
        review_job: tuple[str, str] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        if state not in PROPOSAL_STATES:
            raise ValueError(f"invalid proposal state: {state}")
        stamp = float(now if now is not None else time.time())
        clean_reason = strip_raw_urls(str(reason or "").strip())[:500]
        clean_risks = [strip_raw_urls(str(r))[:160] for r in (risks or [])[:8]]
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (str(proposal_id),)
            ).fetchone()
            if row is None:
                raise ValueError("提案不存在")
            check_review_target(row, expected_identity, expected_revision)
            if row["state"] in {"approved", "revoked", "rejected"}:
                if row["state"] == state:
                    return self._format_proposal_row(row)
                raise ReviewConflict("审核结果已终结，请刷新；已批准内容需使用撤销")
            self.check_review_source(row)
            self.db.execute(
                "INSERT INTO review_audit(proposal_id,from_state,to_state,actor,reason,created_at) VALUES(?,?,?,?,?,?)",
                (proposal_id, row["state"], state, reviewed_by, clean_reason, stamp),
            )
            self.db.execute(
                "UPDATE proposals SET revision=revision+1 WHERE proposal_id=?",
                (proposal_id,),
            )
            if state == "rejected" and row["task_id"]:
                self.db.execute(
                    "UPDATE reading_tasks SET status='rejected',updated_at=? WHERE task_id=?",
                    (stamp, row["task_id"]),
                )
                self.advance_completed_chapters(row["book_id"], stamp)
            self.db.execute(
                """
                UPDATE proposals
                SET state=?, review_verdict=?, review_reason=?, review_risks_json=?, reviewed_by=?, updated_at=?
                WHERE proposal_id=?
                """,
                (
                    state,
                    str(verdict)[:32],
                    clean_reason,
                    encode_json(clean_risks),
                    str(reviewed_by)[:64],
                    stamp,
                    str(proposal_id),
                ),
            )
            if state == "rejected" and row["candidate_id"]:
                self.db.execute(
                    "UPDATE candidate_items SET status='skipped' WHERE candidate_id=?",
                    (row["candidate_id"],),
                )
            if state == "rejected":
                self.schedule_review(proposal_id, "done", stamp)
            elif review_job is not None:
                job_kind, job_error = review_job
                self.schedule_review(proposal_id, job_kind, stamp, job_error)
            updated = self.db.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (str(proposal_id),)
            ).fetchone()
        return self._format_proposal_row(updated)

    def approve_proposal_atomic(
        self,
        proposal_id: str,
        *,
        reviewed_by: str = "llm",
        reason: str = "审查通过",
        risks: list[str] | None = None,
        half_life_days: float = 6.0,
        question_ttl_hours: float = 72.0,
        proactive_sessions: list[str] | None = None,
        expected_revision: int | None = None,
        expected_identity: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically commit an approved proposal into logs, interests, questions, books & intents."""
        stamp = float(now if now is not None else time.time())
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (str(proposal_id),)
            ).fetchone()
            if row is None:
                raise ValueError("提案不存在")
            check_review_target(row, expected_identity, expected_revision)
            if row["state"] == "approved":
                log_row = self.db.execute(
                    "SELECT * FROM reading_logs WHERE log_id=?",
                    (row["applied_log_id"],),
                ).fetchone()
                return {
                    "proposal": self._format_proposal_row(row),
                    "log": self._format_log_row(log_row) if log_row else None,
                    "created_intents": [],
                }
            self.check_review_source(row)
            if row["state"] in {"rejected", "revoked"}:
                raise ReviewConflict(f"提案当前状态为 {row['state']}，无法直接批准")

            identity = row["identity"]
            if self.is_topic_blocked(f"{row['title']} {row['digest']}", identity):
                raise ValueError("提案命中已屏蔽话题，无法批准")

            citation = sanitize_citation(
                {
                    "title": row["title"],
                    "creator": row["creator"],
                    "bvid": row["bvid"],
                    "chapter_label": row["chapter_label"],
                }
            )
            log_id = "log_" + uuid.uuid4().hex[:16]
            try:
                proposal_evidence = json.loads(row["evidence_json"] or "{}")
            except Exception:
                proposal_evidence = {}
            log_media_view = _extract_media_view(proposal_evidence)
            log_media_payload = (
                log_media_view
                if (
                    log_media_view["watch_mode"]
                    or log_media_view["content_origin"]
                    or log_media_view["visual_observations"]
                    or log_media_view["media_meta"]
                )
                else {}
            )
            self.db.execute(
                """
                INSERT INTO reading_logs(
                    log_id, proposal_id, identity, source_kind,
                    citation_json, raw_url, digest, reflection, fact_kind, media_meta_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'external_media', ?, ?)
                """,
                (
                    log_id,
                    row["proposal_id"],
                    identity,
                    row["source_kind"],
                    compact_json(citation),
                    row["raw_url"],
                    row["digest"],
                    row["reflection"],
                    encode_json(log_media_payload),
                    stamp,
                ),
            )

            # Upsert / reinforce staged interest (never mutating character card!)
            si = json.loads(row["staged_interest_json"] or "{}")
            topic = str(si.get("topic") or "").strip()[:48]
            interest_id = ""
            if topic and not self.is_topic_blocked(topic, identity):
                summary = str(si.get("summary") or row["reflection"] or row["digest"])[
                    :140
                ]
                existing_si = self.db.execute(
                    "SELECT * FROM staged_interests WHERE identity=? AND topic=?",
                    (identity, topic),
                ).fetchone()
                ttl_span = max(2.0, float(half_life_days) * 2.0) * 86400.0
                if existing_si is not None:
                    interest_id = existing_si["interest_id"]
                    decayed = compute_decayed_heat(
                        float(existing_si["heat_score"]),
                        float(existing_si["last_reinforced_at"]),
                        stamp,
                        half_life_days,
                    )
                    next_heat = min(2.0, round(max(0.7, decayed) + 0.55, 4))
                    ev_ids = json.loads(existing_si["evidence_log_ids_json"] or "[]")
                    ev_ids = list(dict.fromkeys([*ev_ids[-9:], log_id]))
                    self.db.execute(
                        """
                        UPDATE staged_interests
                        SET summary=?, heat_score=?, last_reinforced_at=?, expires_at=?,
                            status='active', evidence_log_ids_json=?, updated_at=?
                        WHERE interest_id=?
                        """,
                        (
                            summary,
                            next_heat,
                            stamp,
                            max(float(existing_si["expires_at"]), stamp + ttl_span),
                            encode_json(ev_ids),
                            stamp,
                            interest_id,
                        ),
                    )
                else:
                    interest_id = "int_" + uuid.uuid4().hex[:16]
                    self.db.execute(
                        """
                        INSERT INTO staged_interests(
                            interest_id, identity, topic, summary, heat_score,
                            last_reinforced_at, expires_at, status, evidence_log_ids_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 1.0, ?, ?, 'active', ?, ?, ?)
                        """,
                        (
                            interest_id,
                            identity,
                            topic,
                            summary,
                            stamp,
                            stamp + ttl_span,
                            encode_json([log_id]),
                            stamp,
                            stamp,
                        ),
                    )

            # Insert global discussion questions (per-UMO state stays independent!)
            q_ttl = max(3600.0, float(question_ttl_hours) * 3600.0)
            q_ids: list[str] = []
            for q in json.loads(row["questions_json"] or "[]")[:2]:
                q_text = str(q.get("question") or "").strip()[:120]
                q_topic = str(
                    q.get("topic") or topic or citation["title"][:32]
                ).strip()[:48]
                if not q_text or self.is_topic_blocked(f"{q_topic} {q_text}", identity):
                    continue
                qid = "q_" + uuid.uuid4().hex[:16]
                self.db.execute(
                    """
                    INSERT INTO discussion_questions(
                        question_id, identity, log_id, interest_id,
                        topic, question, status, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        qid,
                        identity,
                        log_id,
                        interest_id,
                        q_topic,
                        q_text,
                        stamp,
                        stamp + q_ttl,
                    ),
                )
                q_ids.append(qid)

            # Mark candidate digested & advance book cursor if applicable
            if row["candidate_id"]:
                self.db.execute(
                    "UPDATE candidate_items SET status='digested' WHERE candidate_id=?",
                    (row["candidate_id"],),
                )
            if row["task_id"]:
                self.db.execute(
                    "UPDATE reading_tasks SET status='approved',updated_at=? WHERE task_id=?",
                    (stamp, row["task_id"]),
                )
            if row["book_id"]:
                self.db.execute(
                    "UPDATE books SET last_reflection=? WHERE book_id=?",
                    (row["reflection"][:160], row["book_id"]),
                )

            # Update proposal state
            clean_reason = strip_raw_urls(str(reason or "审查通过"))[:500]
            clean_risks = [strip_raw_urls(str(r))[:160] for r in (risks or [])[:8]]
            self.db.execute(
                """
                UPDATE proposals
                SET state='approved', review_verdict='approve', review_reason=?,
                    review_risks_json=?, reviewed_by=?, applied_log_id=?,
                    applied_interest_id=?, applied_question_ids_json=?, updated_at=?
                WHERE proposal_id=?
                """,
                (
                    clean_reason,
                    encode_json(clean_risks),
                    str(reviewed_by)[:64],
                    log_id,
                    interest_id,
                    encode_json(q_ids),
                    stamp,
                    row["proposal_id"],
                ),
            )

            self.db.execute(
                "UPDATE proposals SET revision=revision+1 WHERE proposal_id=?",
                (proposal_id,),
            )
            self.db.execute(
                "INSERT INTO review_audit(proposal_id,from_state,to_state,actor,reason,created_at) VALUES(?,?,?,?,?,?)",
                (
                    proposal_id,
                    row["state"],
                    "approved",
                    reviewed_by,
                    clean_reason,
                    stamp,
                ),
            )
            self.db.execute(
                "UPDATE review_jobs SET kind='done' WHERE proposal_id=?", (proposal_id,)
            )
            self.advance_completed_chapters(row["book_id"], stamp)
            self.queue_world(
                log_id,
                identity,
                "upsert",
                {
                    "log_id": log_id,
                    "source_kind": row["source_kind"],
                    "title": citation["title"],
                    "creator": citation["creator"],
                    "chapter_label": citation["chapter_label"],
                    "digest": row["digest"],
                    "reflection": row["reflection"],
                    "occurred_at": stamp,
                },
            )
            # Generate gated low-frequency proactive sharing candidates on proactive_sessions
            created_intents = self._create_proactive_intents_in_tx(
                identity=identity,
                topic=topic,
                log_id=log_id,
                title=citation["title"],
                proactive_sessions=proactive_sessions or [],
                now=stamp,
            )

            updated_prop = self.db.execute(
                "SELECT * FROM proposals WHERE proposal_id=?",
                (row["proposal_id"],),
            ).fetchone()
            log_row = self.db.execute(
                "SELECT * FROM reading_logs WHERE log_id=?", (log_id,)
            ).fetchone()

        return {
            "proposal": self._format_proposal_row(updated_prop),
            "log": self._format_log_row(log_row),
            "created_intents": created_intents,
        }

    def _create_proactive_intents_in_tx(
        self,
        *,
        identity: str,
        topic: str,
        log_id: str,
        title: str,
        proactive_sessions: list[str],
        now: float,
    ) -> list[dict[str, Any]]:
        allowed_umos = [
            str(u).strip() for u in proactive_sessions if str(u or "").strip()
        ]
        if not topic or not allowed_umos:
            return []
        persona_id, instance_id = unpack_identity(identity)
        created: list[dict[str, Any]] = []
        for umo in dict.fromkeys(allowed_umos):
            if not self.outreach_binding_valid(umo, identity):
                continue
            # Per-UMO daily/pending/topic cooldowns to prevent group spam bursts
            umo_daily_used = self.db.execute(
                """
                SELECT COUNT(*) FROM horizon_intents
                WHERE umo=? AND status IN ('pending', 'claimed', 'sent') AND created_at>?
                """,
                (umo, now - 86400.0),
            ).fetchone()[0]
            if int(umo_daily_used or 0) >= 3:
                continue
            umo_pending = self.db.execute(
                """
                SELECT COUNT(*) FROM horizon_intents
                WHERE umo=? AND status IN ('pending', 'claimed') AND expires_at>?
                """,
                (umo, now),
            ).fetchone()[0]
            if int(umo_pending or 0) >= 3:
                continue
            same_topic_recent = self.db.execute(
                """
                SELECT 1 FROM horizon_intents
                WHERE umo=? AND topic=?
                  AND (status IN ('pending', 'claimed') OR (status='sent' AND updated_at>?))
                LIMIT 1
                """,
                (umo, topic, now - 3 * 86400.0),
            ).fetchone()
            if same_topic_recent is not None:
                continue
            platform = umo.split(":", 2)[0]
            # Find consenting member in this UMO with >=2 reciprocal turns on this topic
            rows = self.db.execute(
                """
                SELECT * FROM co_discussed_topics
                WHERE identity=? AND umo=? AND opted_out=0
                  AND turn_count>=2 AND bot_reply_count>=1
                  AND last_occurred_at>=?
                  AND (topic=? OR topic LIKE ? ESCAPE '\\' OR (topic!='' AND instr(lower(?), lower(topic))>0))
                ORDER BY turn_count DESC, last_occurred_at DESC
                """,
                (identity, umo, now - 14 * 86400.0, topic, f"%{_escape_like(topic)}%", topic),
            ).fetchall()
            for r in rows:
                member_id = str(r["member_id"])
                if not member_id or member_id in {"bot", "system"}:
                    continue
                pref = self.db.execute(
                    "SELECT opted_out FROM member_outreach_prefs WHERE identity=? AND umo=? AND member_id=?",
                    (identity, umo, member_id),
                ).fetchone()
                if pref is not None and bool(pref["opted_out"]):
                    continue
                if (
                    self.is_topic_blocked(topic, identity)
                    or self.db.execute(
                        "SELECT 1 FROM member_topic_prefs WHERE identity=? AND umo=? AND member_id=? AND topic=? AND blocked=1",
                        (identity, umo, member_id, topic),
                    ).fetchone()
                ):
                    continue
                if self.db.execute(
                    "SELECT 1 FROM corrections WHERE identity=? AND umo=? AND member_id=? AND log_id=? AND status='pending'",
                    (identity, umo, member_id, log_id),
                ).fetchone():
                    continue
                # Enforce 24h per-member cap
                recent_intent = self.db.execute(
                    """
                    SELECT intent_id FROM horizon_intents
                    WHERE umo=? AND recipient_user_id=?
                      AND status IN ('pending', 'claimed', 'sent')
                      AND created_at>?
                    LIMIT 1
                    """,
                    (umo, member_id, now - 86400.0),
                ).fetchone()
                if recent_intent is not None:
                    continue

                anchor = compact_json([identity, umo, member_id, topic, log_id])
                intent_id = "horizon:" + sha256_hex(anchor)[:32]
                scope_dict = {
                    "platform": platform,
                    "umo": umo,
                    "persona_id": persona_id,
                    "instance_id": instance_id,
                }
                src_ev = str(r["last_event_id"] or f"horizon_log:{log_id}")
                # Note: NEVER include 'text' or 'message' keys in intent payload (Chat Dynamics contract).
                payload = {
                    "schema_version": 1,
                    "intent_id": intent_id,
                    "provider": "dynamics_horizons",
                    "kind": "social_chat",
                    "horizon_kind": "co_discussed_topic_share",
                    "status": "pending",
                    "scope": scope_dict,
                    "target_umo": umo,
                    "recipient_user_id": member_id,
                    "topic": topic,
                    "log_id": log_id,
                    "reason": f"最近刚读到与此前共同聊过的「{topic}」相关的新见闻（{title[:48]}），若当下气氛合适可自然分享一句感受。",
                    "evidence_quote": str(r["last_quote"] or "")[:200],
                    "source_event_id": src_ev,
                    "source_ids": [src_ev],
                    "source_occurred_at": float(r["last_occurred_at"] or now),
                    "not_before": now,
                    "expires_at": now + 86400.0,
                    "created_at": now,
                }
                self.db.execute(
                    """
                    INSERT OR IGNORE INTO horizon_intents(
                        intent_id, identity, umo, recipient_user_id, topic, log_id,
                        status, payload_json, not_before, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                    """,
                    (
                        intent_id,
                        identity,
                        umo,
                        member_id,
                        topic,
                        log_id,
                        encode_json(payload),
                        now,
                        now + 86400.0,
                        now,
                        now,
                    ),
                )
                created.append(payload)
                break  # At most 1 candidate per UMO per approved reading
        return created

    # ------------------------------------------------------------------
    # Reading Logs, Interests & Questions Queries / Governance
    # ------------------------------------------------------------------

    @staticmethod
    def _format_log_row(row: sqlite3.Row) -> dict[str, Any]:
        raw_media = {}
        try:
            keys = row.keys()
            if "media_meta_json" in keys and row["media_meta_json"]:
                raw_media = json.loads(row["media_meta_json"])
        except Exception:
            raw_media = {}
        media_view = _extract_media_view(raw_media)
        return {
            "log_id": row["log_id"],
            "proposal_id": row["proposal_id"],
            "identity": row["identity"],
            "source_kind": row["source_kind"],
            "citation": sanitize_citation(json.loads(row["citation_json"] or "{}")),
            "raw_url": row["raw_url"],
            "digest": row["digest"],
            "reflection": row["reflection"],
            "fact_kind": row["fact_kind"],
            "watch_mode": media_view["watch_mode"],
            "content_origin": media_view["content_origin"],
            "visual_observations": media_view["visual_observations"],
            "media_meta": media_view["media_meta"],
            "created_at": float(row["created_at"]),
            "status": row["status"],
        }

    def list_logs(
        self,
        *,
        identity: str = "",
        source_kind: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        norm = normalize_identity(identity) if identity else ""
        where = "WHERE 1=1"
        params: list[Any] = []
        if norm:
            where += " AND identity=?"
            params.append(norm)
        if source_kind and source_kind != "all" and source_kind in SOURCE_KINDS:
            where += " AND source_kind=?"
            params.append(source_kind)
        p = max(1, min(10000, int(page or 1)))
        ps = max(1, min(100, int(page_size or 20)))
        with self.lock:
            total = self.db.execute(
                f"SELECT COUNT(*) FROM reading_logs {where}", params
            ).fetchone()[0]
            p = min(p, max(1, (total + ps - 1) // ps))
            rows = self.db.execute(
                f"SELECT * FROM reading_logs {where} ORDER BY created_at DESC, log_id DESC LIMIT ? OFFSET ?",
                [*params, ps, (p - 1) * ps],
            ).fetchall()
            items = [self._format_log_row(r) for r in rows]
            by_identity: dict[str, list[str]] = {}
            for item in items:
                by_identity.setdefault(item["identity"], []).append(item["log_id"])
            reflections_by_id_and_log: dict[tuple[str, str], list[dict[str, Any]]] = {}
            questions_by_id_and_log: dict[tuple[str, str], list[dict[str, Any]]] = {}
            now_stamp = time.time()
            for ident_key, lids in by_identity.items():
                grouped = self._list_discussion_reflections_for_logs(ident_key, lids)
                for lid, refs in grouped.items():
                    reflections_by_id_and_log[(ident_key, lid)] = refs
                if lids:
                    ph = ",".join("?" for _ in lids)
                    q_rows = self.db.execute(
                        f"""
                        SELECT question_id, log_id, topic, question, status, expires_at
                        FROM discussion_questions
                        WHERE identity=? AND log_id IN ({ph}) AND status='active' AND expires_at>?
                        ORDER BY created_at DESC
                        """,
                        [ident_key, *lids, now_stamp],
                    ).fetchall()
                    for qr in q_rows:
                        questions_by_id_and_log.setdefault(
                            (ident_key, str(qr["log_id"])), []
                        ).append(
                            {
                                "question_id": qr["question_id"],
                                "topic": qr["topic"],
                                "question": qr["question"],
                                "status": qr["status"],
                                "expires_at": float(qr["expires_at"]),
                            }
                        )
            for item in items:
                item["discussion_reflections"] = reflections_by_id_and_log.get(
                    (item["identity"], item["log_id"]), []
                )
                item["questions"] = questions_by_id_and_log.get(
                    (item["identity"], item["log_id"]), []
                )
        return {
            "items": items,
            "page": p,
            "page_size": ps,
            "total": int(total),
        }

    def count_today_digests(
        self, identity: str, now: float | None = None, *, tz=None
    ) -> int:
        stamp = float(now if now is not None else time.time())
        start, end = shanghai_day_bounds(stamp, tz=tz)
        with self.lock:
            n = self.db.execute(
                "SELECT count(*) FROM reading_attempts WHERE identity=? AND created_at>=? AND created_at<?",
                (normalize_identity(identity), start, end),
            ).fetchone()[0]
            legacy = self.db.execute(
                "SELECT count(*) FROM proposals WHERE identity=? AND task_id='' AND created_at>=? AND created_at<?",
                (normalize_identity(identity), start, end),
            ).fetchone()[0]
        return n + legacy

    def settle_decay(
        self, now: float | None = None, half_life_days: float = 6.0
    ) -> int:
        stamp = float(now if now is not None else time.time())
        cooled_count = 0
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT interest_id, heat_score, last_reinforced_at, expires_at FROM staged_interests WHERE status='active'"
            ).fetchall()
            for r in rows:
                decayed = compute_decayed_heat(
                    float(r["heat_score"]),
                    float(r["last_reinforced_at"]),
                    stamp,
                    half_life_days,
                )
                if stamp >= float(r["expires_at"]) or decayed < 0.25:
                    self.db.execute(
                        "UPDATE staged_interests SET status='cooled', updated_at=? WHERE interest_id=?",
                        (stamp, r["interest_id"]),
                    )
                    cooled_count += 1
            self.db.execute(
                "UPDATE discussion_questions SET status='archived' WHERE status='active' AND expires_at<=?",
                (stamp,),
            )
            self.db.execute(
                "UPDATE horizon_intents SET status='expired', updated_at=? WHERE status IN ('pending', 'failed') AND expires_at<=?",
                (stamp, stamp),
            )
        return cooled_count

    def list_interests(
        self,
        identity: str = "",
        *,
        active_only: bool = False,
        half_life_days: float = 6.0,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        stamp = float(now if now is not None else time.time())
        norm = normalize_identity(identity) if identity else ""
        query = "SELECT * FROM staged_interests WHERE 1=1"
        params: list[Any] = []
        if norm:
            query += " AND identity=?"
            params.append(norm)
        if active_only:
            query += " AND status='active'"
        else:
            query += " AND status != 'revoked'"
        query += " ORDER BY updated_at DESC"
        with self.lock:
            rows = self.db.execute(query, params).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            decayed = compute_decayed_heat(
                float(r["heat_score"]),
                float(r["last_reinforced_at"]),
                stamp,
                half_life_days,
            )
            status = r["status"]
            if status == "active" and (
                stamp >= float(r["expires_at"]) or decayed < 0.25
            ):
                status = "cooled"
                if active_only:
                    continue
            out.append(
                {
                    "interest_id": r["interest_id"],
                    "identity": r["identity"],
                    "topic": r["topic"],
                    "summary": r["summary"],
                    "heat_score": round(decayed, 3),
                    "raw_heat_score": float(r["heat_score"]),
                    "last_reinforced_at": float(r["last_reinforced_at"]),
                    "expires_at": float(r["expires_at"]),
                    "status": status,
                }
            )
        out.sort(key=lambda x: (0 if x["status"] == "active" else 1, -x["heat_score"]))
        return out

    def revoke_interest(
        self, interest_id: str, now: float | None = None, *, expected_identity: str = ""
    ) -> dict[str, Any]:
        stamp = float(now if now is not None else time.time())
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM staged_interests WHERE interest_id=?",
                (str(interest_id),),
            ).fetchone()
            if row is None:
                raise ValueError("暂存兴趣不存在")
            check_review_target(row, expected_identity)
            self.db.execute(
                "UPDATE staged_interests SET status='revoked', updated_at=? WHERE interest_id=?",
                (stamp, str(interest_id)),
            )
            self.db.execute(
                "UPDATE discussion_questions SET status='archived' WHERE interest_id=? AND status='active'",
                (str(interest_id),),
            )
            self.db.execute(
                "UPDATE horizon_intents SET status='cancelled',updated_at=? WHERE identity=? AND topic=? AND status IN ('pending','claimed')",
                (stamp, row["identity"], row["topic"]),
            )
        return {"interest_id": str(interest_id), "status": "revoked"}

    def extend_interest(
        self,
        interest_id: str,
        *,
        days: float = 6.0,
        half_life_days: float = 6.0,
        now: float | None = None,
        expected_identity: str = "",
    ) -> dict[str, Any]:
        stamp = float(now if now is not None else time.time())
        add_sec = max(1.0, min(90.0, float(days))) * 86400.0
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM staged_interests WHERE interest_id=?",
                (str(interest_id),),
            ).fetchone()
            if row is None:
                raise ValueError("暂存兴趣不存在")
            check_review_target(row, expected_identity)
            decayed = compute_decayed_heat(
                float(row["heat_score"]),
                float(row["last_reinforced_at"]),
                stamp,
                half_life_days,
            )
            next_heat = max(0.85, min(2.0, decayed + 0.35))
            new_exp = max(float(row["expires_at"]), stamp) + add_sec
            self.db.execute(
                """
                UPDATE staged_interests
                SET status='active', heat_score=?, last_reinforced_at=?, expires_at=?, updated_at=?
                WHERE interest_id=?
                """,
                (next_heat, stamp, new_exp, stamp, str(interest_id)),
            )
        return {
            "interest_id": str(interest_id),
            "status": "active",
            "expires_at": new_exp,
            "heat_score": round(next_heat, 3),
        }

    def archive_question(
        self, question_id: str, *, expected_identity: str = ""
    ) -> dict[str, Any]:
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM discussion_questions WHERE question_id=?",
                (str(question_id),),
            ).fetchone()
            if row is None:
                raise ValueError("待讨论问题不存在")
            check_review_target(row, expected_identity)
            self.db.execute(
                "UPDATE discussion_questions SET status='archived' WHERE question_id=?",
                (str(question_id),),
            )
        return {"question_id": str(question_id), "status": "archived"}

    def list_questions_with_umo_states(
        self,
        identity: str = "",
        *,
        active_only: bool = True,
        configured_umos: list[str] | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        stamp = float(now if now is not None else time.time())
        norm = normalize_identity(identity) if identity else ""
        query = "SELECT * FROM discussion_questions WHERE 1=1"
        params: list[Any] = []
        if norm:
            query += " AND identity=?"
            params.append(norm)
        if active_only:
            query += " AND status='active' AND expires_at>?"
            params.append(stamp)
        query += " ORDER BY created_at DESC LIMIT 50"
        with self.lock:
            q_rows = self.db.execute(query, params).fetchall()
            qids = [r["question_id"] for r in q_rows]
            umo_map: dict[str, dict[str, dict[str, Any]]] = {qid: {} for qid in qids}
            if qids:
                placeholders = ",".join("?" for _ in qids)
                s_rows = self.db.execute(
                    f"SELECT * FROM question_umo_state WHERE question_id IN ({placeholders})",
                    qids,
                ).fetchall()
                for s in s_rows:
                    umo_map[s["question_id"]][s["umo"]] = {
                        "umo": s["umo"],
                        "status": s["status"],
                        "discussed_at": float(s["discussed_at"]),
                    }
        base_umos = [
            str(u).strip() for u in (configured_umos or []) if str(u or "").strip()
        ]
        out: list[dict[str, Any]] = []
        for r in q_rows:
            states_by_umo = dict(umo_map.get(r["question_id"], {}))
            for u in base_umos:
                states_by_umo.setdefault(
                    u, {"umo": u, "status": "pending", "discussed_at": 0.0}
                )
            out.append(
                {
                    "question_id": r["question_id"],
                    "identity": r["identity"],
                    "log_id": r["log_id"],
                    "interest_id": r["interest_id"],
                    "topic": r["topic"],
                    "question": r["question"],
                    "status": r["status"],
                    "created_at": float(r["created_at"]),
                    "expires_at": float(r["expires_at"]),
                    "umo_states": sorted(
                        states_by_umo.values(), key=lambda x: x["umo"]
                    ),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Per-UMO Turn Feedback & Co-Discussed Topic Tracking
    # ------------------------------------------------------------------

    def record_turn_feedback(
        self,
        *,
        identity: str,
        umo: str,
        member_id: str = "",
        user_text: str = "",
        reply_text: str = "",
        delivered: bool = False,
        offered_question_ids: list[str] | None = None,
        offered_topics: list[str] | None = None,
        turn_id: str = "",
        event_id: str = "",
        is_proactive_intent: bool = False,
        verified: bool = False,
        half_life_days: float = 6.0,
        now: float | None = None,
    ) -> dict[str, Any]:
        norm_id = normalize_identity(identity)
        clean_umo = str(umo or "").strip()[:512]
        if not clean_umo:
            return {"accepted": False, "discussed_questions": [], "topics_recorded": []}
        stamp = float(now if now is not None else time.time())
        clean_member = str(member_id or "").strip()[:128]
        compact_user = re.sub(r"\s+", "", str(user_text or ""))

        discussed_qids: list[str] = []
        topics_recorded: list[str] = []
        prefs_changed = False

        with self.lock, self.db:
            # 1. Explicit proactive contact opt-out / opt-in per (identity, umo, member)
            if clean_member and clean_member not in {"bot", "system"} and compact_user:

                def direct_request(cues):
                    phrases = "|".join(re.escape(cue) for cue in cues)
                    return bool(
                        re.fullmatch(
                            r"(?:以后|请|请你|麻烦|你|我现在忙[，,])?(?:"
                            + phrases
                            + r")(?:了|吧|啦)?[，,。！!]*(?:谢谢[。！!]*)?",
                            compact_user,
                        )
                    )

                if direct_request(_OPT_OUT_CUES):
                    self.db.execute(
                        "INSERT OR REPLACE INTO member_outreach_prefs VALUES (?, ?, ?, 1, ?)",
                        (norm_id, clean_umo, clean_member, stamp),
                    )
                    self.db.execute(
                        "UPDATE co_discussed_topics SET opted_out=1 WHERE identity=? AND umo=? AND member_id=?",
                        (norm_id, clean_umo, clean_member),
                    )
                    self.db.execute(
                        """
                        UPDATE horizon_intents SET status='cancelled', updated_at=?
                        WHERE identity=? AND umo=? AND recipient_user_id=? AND status IN ('pending', 'claimed')
                        """,
                        (stamp, norm_id, clean_umo, clean_member),
                    )
                    prefs_changed = True
                elif direct_request(_OPT_IN_CUES):
                    self.db.execute(
                        "INSERT OR REPLACE INTO member_outreach_prefs VALUES (?, ?, ?, 0, ?)",
                        (norm_id, clean_umo, clean_member, stamp),
                    )
                    self.db.execute(
                        "UPDATE co_discussed_topics SET opted_out=0 WHERE identity=? AND umo=? AND member_id=?",
                        (norm_id, clean_umo, clean_member),
                    )
                    prefs_changed = True

            if not delivered or not verified:
                return {
                    "accepted": True,
                    "discussed_questions": [],
                    "topics_recorded": [],
                    "prefs_changed": prefs_changed,
                }
            receipt_key = sha256_hex(
                [norm_id, clean_umo, clean_member, event_id or turn_id]
            )
            if (
                not (event_id or turn_id)
                or self.db.execute(
                    "SELECT 1 FROM feedback_receipts WHERE event_id=?", (receipt_key,)
                ).fetchone()
            ):
                return {
                    "accepted": False,
                    "discussed_questions": [],
                    "topics_recorded": [],
                }
            self.db.execute(
                "INSERT INTO feedback_receipts VALUES(?,?)", (receipt_key, stamp)
            )

            # 2. Mark offered / matched questions as 'discussed' ONLY in this UMO
            candidate_qids = [
                str(q).strip()
                for q in (offered_question_ids or [])
                if str(q or "").strip()
            ]
            active_qs = self.db.execute(
                "SELECT question_id, topic, question FROM discussion_questions WHERE identity=? AND status='active' AND expires_at>?",
                (norm_id, stamp),
            ).fetchall()
            active_q_by_id = {r["question_id"]: r for r in active_qs}

            for qid in candidate_qids:
                if qid in active_q_by_id:
                    discussed_qids.append(qid)

            for qid in dict.fromkeys(discussed_qids):
                self.db.execute(
                    """
                    INSERT INTO question_umo_state(question_id, umo, status, discussed_at, turn_id)
                    VALUES (?, ?, 'discussed', ?, ?)
                    ON CONFLICT(question_id, umo) DO UPDATE SET
                        status='discussed',
                        discussed_at=excluded.discussed_at,
                        turn_id=excluded.turn_id
                    """,
                    (qid, clean_umo, stamp, str(turn_id or "")[:128]),
                )

            # 3. Record reciprocal topic evidence per (identity, umo, member, topic)
            active_interests = self.db.execute(
                "SELECT interest_id, topic, heat_score, last_reinforced_at, expires_at FROM staged_interests WHERE identity=? AND status='active'",
                (norm_id,),
            ).fetchall()
            interest_by_topic = {r["topic"]: r for r in active_interests}

            matched_topics: list[str] = []
            for t in offered_topics or []:
                ts = str(t or "").strip()[:48]
                if len(ts) >= 2:
                    matched_topics.append(ts)

            clean_quote = strip_raw_urls(
                sanitize_untrusted_text(user_text, max_chars=180)["text"]
            )[:180]
            pref_row = (
                self.db.execute(
                    "SELECT opted_out FROM member_outreach_prefs WHERE identity=? AND umo=? AND member_id=?",
                    (norm_id, clean_umo, clean_member),
                ).fetchone()
                if clean_member
                else None
            )
            opted_out = int(bool(pref_row["opted_out"])) if pref_row is not None else 0

            for topic in dict.fromkeys(matched_topics):
                member_blocked = self.db.execute(
                    "SELECT 1 FROM member_topic_prefs WHERE identity=? AND umo=? AND member_id=? AND topic=? AND blocked=1",
                    (norm_id, clean_umo, clean_member, topic),
                ).fetchone()
                if member_blocked or opted_out or self.is_topic_blocked(topic, norm_id):
                    continue
                topics_recorded.append(topic)
                # Reinforce staged interest warmth (without copying any group text into global interest!)
                si_row = interest_by_topic.get(topic)
                if si_row is not None:
                    decayed = compute_decayed_heat(
                        float(si_row["heat_score"]),
                        float(si_row["last_reinforced_at"]),
                        stamp,
                        half_life_days,
                    )
                    next_heat = min(2.0, max(0.75, decayed) + 0.25)
                    ttl_span = max(2.0, float(half_life_days) * 2.0) * 86400.0
                    self.db.execute(
                        """
                        UPDATE staged_interests
                        SET heat_score=?, last_reinforced_at=?, expires_at=MAX(expires_at, ?), updated_at=?
                        WHERE interest_id=?
                        """,
                        (
                            next_heat,
                            stamp,
                            stamp + ttl_span,
                            stamp,
                            si_row["interest_id"],
                        ),
                    )

                if (
                    clean_member
                    and clean_member not in {"bot", "system"}
                    and not is_proactive_intent
                ):
                    old_co = self.db.execute(
                        "SELECT * FROM co_discussed_topics WHERE identity=? AND umo=? AND member_id=? AND topic=?",
                        (norm_id, clean_umo, clean_member, topic),
                    ).fetchone()
                    tid = str(turn_id or event_id or f"t:{stamp}")[:128]
                    if old_co is None:
                        self.db.execute(
                            """
                            INSERT INTO co_discussed_topics(
                                identity, umo, member_id, topic, turn_count, bot_reply_count,
                                last_quote, last_event_id, last_turn_id, last_occurred_at, opted_out
                            ) VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?)
                            """,
                            (
                                norm_id,
                                clean_umo,
                                clean_member,
                                topic,
                                clean_quote,
                                str(event_id or tid)[:128],
                                tid,
                                stamp,
                                opted_out,
                            ),
                        )
                    elif old_co["last_turn_id"] != tid:
                        self.db.execute(
                            """
                            UPDATE co_discussed_topics
                            SET turn_count=turn_count+1,
                                bot_reply_count=bot_reply_count+1,
                                last_quote=CASE WHEN ?!='' THEN ? ELSE last_quote END,
                                last_event_id=?,
                                last_turn_id=?,
                                last_occurred_at=?,
                                opted_out=?
                            WHERE identity=? AND umo=? AND member_id=? AND topic=?
                            """,
                            (
                                clean_quote,
                                clean_quote,
                                str(event_id or tid)[:128],
                                tid,
                                stamp,
                                opted_out or int(old_co["opted_out"]),
                                norm_id,
                                clean_umo,
                                clean_member,
                                topic,
                            ),
                        )

        return {
            "accepted": True,
            "discussed_questions": list(dict.fromkeys(discussed_qids)),
            "topics_recorded": topics_recorded,
        }

    # ------------------------------------------------------------------
    # Proactive Outreach Intents (Gated by Chat Dynamics)
    # ------------------------------------------------------------------

    def list_due_intents(
        self,
        now: float | None = None,
        *,
        proactive_sessions: list[str] | None = None,
        quiet_hours: str = "00:00-08:00",
        tz=None,
    ) -> list[dict[str, Any]]:
        stamp = float(now if now is not None else time.time())
        if is_in_quiet_hours(stamp, quiet_hours, tz=tz):
            return []
        allowed = {
            str(u).strip() for u in (proactive_sessions or []) if str(u or "").strip()
        }
        if not allowed:
            return []
        allowed_list = list(dict.fromkeys(allowed))
        placeholders = ",".join("?" for _ in allowed_list)
        day_ago = stamp - 86400.0
        out: list[dict[str, Any]] = []
        with self.lock, self.db:
            self.db.execute(
                "UPDATE horizon_intents SET status='expired', updated_at=? WHERE status IN ('pending', 'failed') AND expires_at<=?",
                (stamp, stamp),
            )
            # Mark due intents whose backing evidence or consent was revoked as cancelled before LIMIT
            self.db.execute(
                """
                UPDATE horizon_intents SET status='cancelled', updated_at=?
                WHERE status='pending' AND not_before<=? AND expires_at>?
                  AND (
                      NOT EXISTS (
                          SELECT 1 FROM reading_logs rl
                          WHERE rl.log_id=horizon_intents.log_id
                            AND rl.identity=horizon_intents.identity
                            AND rl.status='active'
                      )
                      OR EXISTS (
                          SELECT 1 FROM member_outreach_prefs mop
                          WHERE mop.identity=horizon_intents.identity
                            AND mop.umo=horizon_intents.umo
                            AND mop.member_id=horizon_intents.recipient_user_id
                            AND mop.opted_out=1
                      )
                      OR EXISTS (
                          SELECT 1 FROM member_topic_prefs mtp
                          WHERE mtp.identity=horizon_intents.identity
                            AND mtp.umo=horizon_intents.umo
                            AND mtp.member_id=horizon_intents.recipient_user_id
                            AND mtp.topic=horizon_intents.topic
                            AND mtp.blocked=1
                      )
                      OR EXISTS (
                          SELECT 1 FROM corrections cor
                          WHERE cor.identity=horizon_intents.identity
                            AND cor.umo=horizon_intents.umo
                            AND cor.member_id=horizon_intents.recipient_user_id
                            AND cor.log_id=horizon_intents.log_id
                            AND cor.status='pending'
                      )
                      OR EXISTS (
                          SELECT 1 FROM scope_bindings sb
                          WHERE sb.umo=horizon_intents.umo
                            AND (sb.managed=0 OR sb.identity!=horizon_intents.identity)
                      )
                  )
                """,
                (stamp, stamp, stamp),
            )
            rows = self.db.execute(
                f"""
                SELECT hi.* FROM horizon_intents hi
                WHERE hi.status='pending'
                  AND hi.not_before<=?
                  AND hi.expires_at>?
                  AND hi.umo IN ({placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM blocked_topics bt
                      WHERE bt.topic=hi.topic
                        AND (bt.identity='' OR bt.identity=hi.identity)
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM horizon_intents o
                      WHERE o.umo=hi.umo
                        AND o.recipient_user_id=hi.recipient_user_id
                        AND (
                            o.status IN ('claimed', 'uncertain')
                            OR (o.status='sent' AND o.updated_at>?)
                        )
                  )
                  AND (
                      SELECT COUNT(*) FROM horizon_intents u
                      WHERE u.umo=hi.umo
                        AND (
                            u.status IN ('claimed', 'uncertain')
                            OR (u.status='sent' AND u.updated_at>?)
                        )
                  ) < 3
                ORDER BY hi.not_before ASC LIMIT 50
                """,
                (stamp, stamp, *allowed_list, day_ago, day_ago),
            ).fetchall()
            seen_umo_topics: set[tuple[str, str]] = set()
            seen_umo_members: set[tuple[str, str]] = set()
            umo_batch_counts: dict[str, int] = {}
            for r in rows:
                if not self.intent_evidence_valid(r):
                    self.db.execute(
                        "UPDATE horizon_intents SET status='cancelled', updated_at=? WHERE intent_id=?",
                        (stamp, r["intent_id"]),
                    )
                    continue
                umo_key = str(r["umo"])
                topic_key = (umo_key, str(r["topic"]))
                member_key = (umo_key, str(r["recipient_user_id"]))
                if member_key in seen_umo_members or topic_key in seen_umo_topics:
                    continue
                if umo_batch_counts.get(umo_key, 0) >= 2:
                    continue
                seen_umo_members.add(member_key)
                seen_umo_topics.add(topic_key)
                umo_batch_counts[umo_key] = umo_batch_counts.get(umo_key, 0) + 1
                item = json.loads(r["payload_json"])
                item.update(
                    status=r["status"],
                    managed=True,
                    paused=False,
                    outreach_paused=False,
                )
                out.append(item)
        return out

    def get_intent(
        self,
        intent_id: str,
        *,
        now: float | None = None,
        proactive_sessions: list[str] | None = None,
    ) -> dict[str, Any]:
        stamp = float(now if now is not None else time.time())
        allowed = {
            str(u).strip() for u in (proactive_sessions or []) if str(u or "").strip()
        }
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM horizon_intents WHERE intent_id=?",
                (str(intent_id),),
            ).fetchone()
            if row is None:
                return {"schema_version": 1, "available": False, "item": None}
            status = row["status"]
            if status in {"pending", "claimed"} and not self.intent_evidence_valid(row):
                status = "cancelled"
                self.db.execute(
                    "UPDATE horizon_intents SET status='cancelled' WHERE intent_id=?",
                    (intent_id,),
                )
            if status in {"pending", "failed"} and float(row["expires_at"]) <= stamp:
                status = "expired"
                self.db.execute(
                    "UPDATE horizon_intents SET status='expired', updated_at=? WHERE intent_id=?",
                    (stamp, str(intent_id)),
                )
            item = json.loads(row["payload_json"])
            managed = row["umo"] in allowed
            item.update(
                status=status,
                managed=managed,
                paused=False,
                outreach_paused=not managed,
            )
            return {"schema_version": 1, "available": True, "item": item}

    def update_intent(
        self,
        intent_id: str,
        status: str,
        *,
        receipt: dict[str, Any] | None = None,
        now: float | None = None,
        proactive_sessions: list[str] | None = None,
    ) -> dict[str, Any]:
        if status not in INTENT_STATUSES:
            raise ValueError(f"unsupported intent status: {status}")
        stamp = float(now if now is not None else time.time())
        allowed = {
            str(u).strip() for u in (proactive_sessions or []) if str(u or "").strip()
        }
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM horizon_intents WHERE intent_id=?",
                (str(intent_id),),
            ).fetchone()
            if row is None:
                return {"schema_version": 1, "status": "missing", "changed": False}
            cur_status = row["status"]
            if cur_status in TERMINAL_INTENT_STATUSES:
                return {"schema_version": 1, "status": cur_status, "changed": False}
            item = json.loads(row["payload_json"])
            if status == "claimed":
                if not self.intent_evidence_valid(row):
                    return {
                        "schema_version": 1,
                        "status": "cancelled",
                        "changed": False,
                    }
                if (
                    cur_status != "pending"
                    or float(row["not_before"]) > stamp
                    or float(row["expires_at"]) <= stamp
                ):
                    return {"schema_version": 1, "status": cur_status, "changed": False}
                if row["umo"] not in allowed:
                    return {"schema_version": 1, "status": "disabled", "changed": False}
                item["claimed_at"] = stamp
            if status == "pending" and cur_status in {"claimed", "failed"}:
                raise ValueError("a send attempt cannot return to pending")
            if receipt is not None:
                item["receipt"] = copy.deepcopy(receipt)
            item["status"] = status
            self.db.execute(
                "UPDATE horizon_intents SET status=?, payload_json=?, updated_at=? WHERE intent_id=?",
                (status, encode_json(item), stamp, str(intent_id)),
            )
        return {"schema_version": 1, "status": status, "changed": True, "intent": item}

    # ------------------------------------------------------------------
    # Projection Snapshot & Overview Aggregates
    # ------------------------------------------------------------------

    def known_identities(self) -> list[str]:
        with self.lock:
            rows = self.db.execute(
                """
                SELECT identity FROM sources
                UNION SELECT identity FROM books
                UNION SELECT identity FROM reading_logs
                UNION SELECT identity FROM staged_interests
                UNION SELECT identity FROM proposals
                """
            ).fetchall()
        return sorted({str(r[0]) for r in rows if r[0]})

    def summary_counts(
        self,
        identity: str = "",
        *,
        allow_all: bool = False,
        now: float | None = None,
        half_life_days: float = 6.0,
    ) -> dict[str, Any]:
        stamp = float(now if now is not None else time.time())
        norm = normalize_identity(identity) if str(identity or "").strip() else ""
        if not norm and not allow_all:
            return {
                "sources": 0,
                "sources_total": 0,
                "sources_enabled": 0,
                "candidates_unread": 0,
                "logs_total": 0,
                "books_reading": 0,
                "active_interests": 0,
                "pending_questions": 0,
                "pending_reviews": 0,
                "recent_title": "",
            }
        id_clause = " AND identity=?" if norm else ""
        id_params = (norm,) if norm else ()
        with self.lock:
            sources_total = self.db.execute(
                f"SELECT COUNT(*) FROM sources WHERE 1=1{id_clause}", id_params
            ).fetchone()[0]
            sources_enabled = self.db.execute(
                f"SELECT COUNT(*) FROM sources WHERE enabled=1{id_clause}", id_params
            ).fetchone()[0]
            candidates_unread = self.db.execute(
                f"SELECT COUNT(*) FROM candidate_items WHERE status='unread'{id_clause}",
                id_params,
            ).fetchone()[0]
            logs_total = self.db.execute(
                f"SELECT COUNT(*) FROM reading_logs WHERE 1=1{id_clause}", id_params
            ).fetchone()[0]
            books_reading = self.db.execute(
                f"SELECT COUNT(*) FROM books WHERE status='reading'{id_clause}",
                id_params,
            ).fetchone()[0]
            pending_reviews = self.db.execute(
                f"SELECT COUNT(*) FROM proposals WHERE state='pending_review'{id_clause}",
                id_params,
            ).fetchone()[0]
            pending_questions = self.db.execute(
                f"SELECT COUNT(*) FROM discussion_questions WHERE status='active' AND expires_at>?{id_clause}",
                (stamp, *id_params),
            ).fetchone()[0]
            recent_row = self.db.execute(
                f"SELECT citation_json FROM reading_logs WHERE 1=1{id_clause} ORDER BY created_at DESC LIMIT 1",
                id_params,
            ).fetchone()
        active_interests = len(
            self.list_interests(
                norm, active_only=True, half_life_days=half_life_days, now=stamp
            )
        )
        recent_title = ""
        if recent_row is not None:
            recent_title = str(
                json.loads(recent_row["citation_json"] or "{}").get("title") or ""
            )
        return {
            "sources": int(sources_total),
            "sources_total": int(sources_total),
            "sources_enabled": int(sources_enabled),
            "candidates_unread": int(candidates_unread),
            "logs_total": int(logs_total),
            "books_reading": int(books_reading),
            "active_interests": int(active_interests),
            "pending_questions": int(pending_questions),
            "pending_reviews": int(pending_reviews),
            "recent_title": recent_title,
        }

    def build_projection_snapshot(
        self, *, now: float | None = None, half_life_days: float = 6.0
    ) -> dict[str, Any]:
        """Build an immutable, URL-free RAM snapshot for zero-I/O get_horizons_context."""
        stamp = float(now if now is not None else time.time())
        identities = self.known_identities()
        by_identity: dict[str, dict[str, Any]] = {}

        with self.lock:
            for ident in identities:
                blocked = [b.casefold() for b in self.list_blocked_topics(ident)]
                interests = self.list_interests(
                    ident, active_only=True, half_life_days=half_life_days, now=stamp
                )
                log_rows = self.db.execute(
                    "SELECT * FROM reading_logs WHERE identity=? AND status='active' ORDER BY created_at DESC LIMIT 20",
                    (ident,),
                ).fetchall()
                discussion_reflections = self.list_discussion_reflections(
                    ident,
                    limit=200,
                )
                discussion_reflections = [
                    row
                    for row in discussion_reflections
                    if row["occurred_at"] >= stamp - 30 * 86400.0
                ]
                logs = []
                for lr in log_rows:
                    cit = sanitize_citation(json.loads(lr["citation_json"] or "{}"))
                    digest_txt = strip_raw_urls(lr["digest"])
                    refl_txt = strip_raw_urls(lr["reflection"])
                    if any(
                        b in f"{cit['title']} {digest_txt}".casefold() for b in blocked
                    ):
                        continue
                    logs.append(
                        {
                            "log_id": lr["log_id"],
                            "source_kind": lr["source_kind"],
                            "citation": cit,  # Strictly NO raw_url!
                            "digest": digest_txt,
                            "reflection": refl_txt,
                            "created_at": float(lr["created_at"]),
                        }
                    )

                book_rows = self.db.execute(
                    "SELECT * FROM books WHERE identity=? AND status='reading' ORDER BY updated_at DESC LIMIT 8",
                    (ident,),
                ).fetchall()
                books = [
                    {
                        "book_id": br["book_id"],
                        "title": strip_raw_urls(br["title"]),
                        "creator": strip_raw_urls(br["creator"]),
                        "chapter_cursor": int(br["chapter_cursor"]),
                        "total_chapters": int(br["total_chapters"]),
                        "last_reflection": strip_raw_urls(br["last_reflection"]),
                    }
                    for br in book_rows
                ]

                q_rows = self.db.execute(
                    "SELECT * FROM discussion_questions WHERE identity=? AND status='active' AND expires_at>? ORDER BY created_at DESC LIMIT 30",
                    (ident, stamp),
                ).fetchall()
                qids = [qr["question_id"] for qr in q_rows]
                discussed_by_qid: dict[str, dict[str, float]] = {
                    qid: {} for qid in qids
                }
                if qids:
                    ph = ",".join("?" for _ in qids)
                    s_rows = self.db.execute(
                        f"SELECT question_id, umo, discussed_at FROM question_umo_state WHERE status='discussed' AND question_id IN ({ph})",
                        qids,
                    ).fetchall()
                    for sr in s_rows:
                        discussed_by_qid[sr["question_id"]][sr["umo"]] = float(
                            sr["discussed_at"]
                        )

                questions = []
                for qr in q_rows:
                    q_topic = strip_raw_urls(qr["topic"])
                    q_text = strip_raw_urls(qr["question"])
                    if any(b in f"{q_topic} {q_text}".casefold() for b in blocked):
                        continue
                    questions.append(
                        {
                            "question_id": qr["question_id"],
                            "log_id": qr["log_id"],
                            "interest_id": qr["interest_id"],
                            "topic": q_topic,
                            "question": q_text,
                            "created_at": float(qr["created_at"]),
                            "expires_at": float(qr["expires_at"]),
                            "discussed_umos": discussed_by_qid.get(
                                qr["question_id"], {}
                            ),
                        }
                    )

                corrections = [
                    dict(r)
                    for r in self.db.execute(
                        "SELECT umo,member_id,log_id FROM corrections WHERE identity=? AND status='pending'",
                        (ident,),
                    )
                ]
                member_topics = [
                    dict(r)
                    for r in self.db.execute(
                        "SELECT umo,member_id,topic FROM member_topic_prefs WHERE identity=? AND blocked=1",
                        (ident,),
                    )
                ]
                by_identity[ident] = {
                    "corrections": corrections,
                    "member_topics": member_topics,
                    "identity": ident,
                    "blocked": blocked,
                    "interests": interests,
                    "logs": logs,
                    "discussion_reflections": discussion_reflections,
                    "books": books,
                    "questions": questions,
                }

        return {
            "built_at": stamp,
            "by_identity": by_identity,
            "summary": self.summary_counts(
                allow_all=True, now=stamp, half_life_days=half_life_days
            ),
        }

    # ------------------------------------------------------------------
    # Read-Only Cross-Plugin Series Hooks Helpers (v2.0)
    # ------------------------------------------------------------------

    def count_today_logs_and_latest(
        self,
        identity: str = "",
        now: float | None = None,
        *,
        allow_all: bool = False,
        tz=None,
    ) -> tuple[int, float]:
        """Return (active logs created today in configured timezone, latest log created_at)."""
        stamp = float(now if now is not None else time.time())
        start, end = shanghai_day_bounds(stamp, tz=tz)
        norm = normalize_identity(identity) if str(identity or "").strip() else ""
        if not norm and not allow_all:
            return 0, 0.0
        id_clause = " AND identity=?" if norm else ""
        id_params = (norm,) if norm else ()
        with self.lock:
            today_count = self.db.execute(
                f"SELECT COUNT(*) FROM reading_logs WHERE status='active' AND created_at>=? AND created_at<?{id_clause}",
                (start, end, *id_params),
            ).fetchone()[0]
            latest_row = self.db.execute(
                f"SELECT MAX(created_at) FROM reading_logs WHERE status='active'{id_clause}",
                id_params,
            ).fetchone()
        latest_ts = float(latest_row[0] or 0.0) if latest_row else 0.0
        return int(today_count or 0), latest_ts

    def get_scope_binding(self, umo: str) -> dict[str, Any] | None:
        clean = str(umo or "").strip()
        if not clean:
            return None
        with self.lock:
            row = self.db.execute(
                "SELECT umo, identity, managed, updated_at FROM scope_bindings WHERE umo=?",
                (clean,),
            ).fetchone()
        if row is None:
            return None
        return {
            "umo": str(row["umo"]),
            "identity": str(row["identity"]),
            "managed": bool(row["managed"]),
            "updated_at": float(row["updated_at"]),
        }

    def count_scope_activity_today(
        self,
        umo: str,
        identity: str = "",
        now: float | None = None,
        *,
        allow_all: bool = False,
        tz=None,
    ) -> int:
        stamp = float(now if now is not None else time.time())
        start, end = shanghai_day_bounds(stamp, tz=tz)
        clean_umo = str(umo or "").strip()
        norm = normalize_identity(identity) if str(identity or "").strip() else ""
        total = 0
        with self.lock:
            if clean_umo:
                total += int(
                    self.db.execute(
                        "SELECT COUNT(*) FROM question_umo_state WHERE umo=? AND status='discussed' AND discussed_at>=? AND discussed_at<?",
                        (clean_umo, start, end),
                    ).fetchone()[0]
                    or 0
                )
                total += int(
                    self.db.execute(
                        "SELECT COUNT(*) FROM discussion_reflections WHERE umo=? AND status='active' AND occurred_at>=? AND occurred_at<?",
                        (clean_umo, start, end),
                    ).fetchone()[0]
                    or 0
                )
            if norm:
                total += int(
                    self.db.execute(
                        "SELECT COUNT(*) FROM reading_logs WHERE identity=? AND status='active' AND created_at>=? AND created_at<?",
                        (norm, start, end),
                    ).fetchone()[0]
                    or 0
                )
            elif allow_all:
                total += int(
                    self.db.execute(
                        "SELECT COUNT(*) FROM reading_logs WHERE status='active' AND created_at>=? AND created_at<?",
                        (start, end),
                    ).fetchone()[0]
                    or 0
                )
        return total

    def search_items_readonly(
        self,
        query: str,
        *,
        identity: str = "",
        allow_all: bool = False,
        limit: int = 8,
        half_life_days: float = 6.0,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Read-only search across active interests, reading logs, books, sources, and questions."""
        q_raw = str(query or "").strip()
        if not q_raw:
            return []
        q_low = q_raw.casefold()
        lim = max(1, min(50, int(limit or 8)))
        stamp = float(now if now is not None else time.time())
        norm = normalize_identity(identity) if str(identity or "").strip() else ""
        if not norm and not allow_all:
            return []
        blocked_cache: dict[str, list[str]] = {}

        def _is_blocked(ident_key: str, text_blob: str) -> bool:
            if ident_key not in blocked_cache:
                blocked_cache[ident_key] = [
                    b.casefold() for b in self.list_blocked_topics(ident_key)
                ]
            low = text_blob.casefold()
            return any(b and b in low for b in blocked_cache[ident_key])

        results: list[dict[str, Any]] = []

        # 1. Active staged interests
        interests = self.list_interests(
            norm, active_only=True, half_life_days=half_life_days, now=stamp
        )
        for item in interests:
            if len(results) >= lim:
                return results
            topic = strip_raw_urls(str(item.get("topic") or "")).strip()
            summary = strip_raw_urls(str(item.get("summary") or "")).strip()
            ident_key = str(item.get("identity") or "")
            blob = f"{topic} {summary}"
            if q_low not in blob.casefold() or _is_blocked(ident_key, blob):
                continue
            heat_val = float(item.get("heat_score") or 0.0)
            heat_pct = (
                min(100, round(heat_val * 100))
                if heat_val <= 2.0001
                else min(100, round(heat_val))
            )
            results.append(
                {
                    "kind": "interest",
                    "title": topic[:80] or "暂存兴趣",
                    "subtitle": f"暂存兴趣 · 热度 {heat_pct}% · {summary[:64]}".rstrip(
                        " ·"
                    ),
                    "plugin_id": PLUGIN_ID,
                    "page": "horizons",
                    "deep_link": {
                        "tab": "overview",
                        "identity": ident_key,
                        "interest_id": str(item.get("interest_id") or ""),
                    },
                }
            )

        # 2. Reading journal entries (active, URL-free)
        if len(results) < lim:
            id_clause = " AND identity=?" if norm else ""
            id_params = (norm,) if norm else ()
            with self.lock:
                log_rows = self.db.execute(
                    f"SELECT log_id, identity, source_kind, citation_json, digest, reflection FROM reading_logs WHERE status='active'{id_clause} ORDER BY created_at DESC LIMIT 80",
                    id_params,
                ).fetchall()
            for r in log_rows:
                if len(results) >= lim:
                    return results
                cit = sanitize_citation(json.loads(r["citation_json"] or "{}"))
                title = strip_raw_urls(str(cit.get("title") or "")).strip()
                creator = strip_raw_urls(str(cit.get("creator") or "")).strip()
                digest = strip_raw_urls(str(r["digest"] or "")).strip()
                refl = strip_raw_urls(str(r["reflection"] or "")).strip()
                ident_key = str(r["identity"] or "")
                blob = f"{title} {creator} {digest} {refl}"
                if q_low not in blob.casefold() or _is_blocked(ident_key, blob):
                    continue
                excerpt = (refl or digest)[:72]
                sub_parts = ["阅读手账"]
                if creator:
                    sub_parts.append(creator[:24])
                if excerpt:
                    sub_parts.append(excerpt)
                results.append(
                    {
                        "kind": "reading_log",
                        "title": title[:80] or "阅读手账",
                        "subtitle": " · ".join(sub_parts),
                        "plugin_id": PLUGIN_ID,
                        "page": "horizons",
                        "deep_link": {
                            "tab": "logs",
                            "identity": ident_key,
                            "log_id": str(r["log_id"]),
                        },
                    }
                )

        # 3. Bookshelf books
        if len(results) < lim:
            for book in self.list_books(norm):
                if len(results) >= lim:
                    return results
                title = strip_raw_urls(str(book.get("title") or "")).strip()
                creator = strip_raw_urls(str(book.get("creator") or "")).strip()
                refl = strip_raw_urls(str(book.get("last_reflection") or "")).strip()
                blob = f"{title} {creator} {refl}"
                if q_low not in blob.casefold():
                    continue
                cursor = int(book.get("chapter_cursor") or 0)
                total = int(book.get("total_chapters") or 0)
                prog = f"第 {cursor}/{total} 章" if total > 0 else f"第 {cursor} 章"
                sub_parts = ["连载书架"]
                if creator:
                    sub_parts.append(creator[:24])
                sub_parts.append(prog)
                results.append(
                    {
                        "kind": "book",
                        "title": title[:80] or "连载书目",
                        "subtitle": " · ".join(sub_parts),
                        "plugin_id": PLUGIN_ID,
                        "page": "horizons",
                        "deep_link": {
                            "tab": "sources",
                            "identity": str(book.get("identity") or ""),
                            "book_id": str(book.get("book_id") or ""),
                        },
                    }
                )

        # 4. Subscribed sources (never expose raw or masked secrets)
        if len(results) < lim:
            kind_names = {
                "bilibili": "B站只读源",
                "ebook": "电子书源",
                "feed": "标准订阅",
                "manual": "手动投喂",
            }
            for src in self.list_sources(norm, mask=True):
                if len(results) >= lim:
                    return results
                name = strip_raw_urls(str(src.get("name") or "")).strip()
                sk = str(src.get("source_kind") or "")
                kind_label = kind_names.get(sk, sk)
                blob = f"{name} {sk} {kind_label}"
                if q_low not in blob.casefold():
                    continue
                status_text = "已启用" if src.get("enabled") else "已停用"
                results.append(
                    {
                        "kind": "source",
                        "title": name[:80] or "信息源",
                        "subtitle": f"信息源 · {kind_label} · {status_text}",
                        "plugin_id": PLUGIN_ID,
                        "page": "horizons",
                        "deep_link": {
                            "tab": "sources",
                            "identity": str(src.get("identity") or ""),
                            "source_id": str(src.get("source_id") or ""),
                        },
                    }
                )

        # 5. Active pending discussion questions
        if len(results) < lim:
            id_clause = " AND identity=?" if norm else ""
            id_params = (norm,) if norm else ()
            with self.lock:
                q_rows = self.db.execute(
                    f"SELECT question_id, identity, topic, question FROM discussion_questions WHERE status='active' AND expires_at>?{id_clause} ORDER BY created_at DESC LIMIT 40",
                    (stamp, *id_params),
                ).fetchall()
            for qr in q_rows:
                if len(results) >= lim:
                    return results
                q_topic = strip_raw_urls(str(qr["topic"] or "")).strip()
                q_text = strip_raw_urls(str(qr["question"] or "")).strip()
                ident_key = str(qr["identity"] or "")
                blob = f"{q_topic} {q_text}"
                if q_low not in blob.casefold() or _is_blocked(ident_key, blob):
                    continue
                results.append(
                    {
                        "kind": "question",
                        "title": q_text[:80] or q_topic[:80],
                        "subtitle": f"待讨论问题 · {q_topic[:32]}" if q_topic else "待讨论问题",
                        "plugin_id": PLUGIN_ID,
                        "page": "horizons",
                        "deep_link": {
                            "tab": "overview",
                            "identity": ident_key,
                            "question_id": str(qr["question_id"]),
                        },
                    }
                )

        return results[:lim]

    def list_journal_events_readonly(
        self,
        *,
        identity: str = "",
        allow_all: bool = False,
        start_ts: float,
        end_ts: float,
        limit: int = 20,
        tz=None,
    ) -> list[dict[str, Any]]:
        """Read-only reading & interest events within [start_ts, end_ts) for World's Living Journal."""
        event_tz = tz or SHANGHAI_TZ
        lim = max(1, min(100, int(limit or 20)))
        norm = normalize_identity(identity) if str(identity or "").strip() else ""
        if not norm and not allow_all:
            return []
        id_clause = " AND identity=?" if norm else ""
        id_params = (norm,) if norm else ()
        blocked_cache: dict[str, list[str]] = {}

        def _is_blocked(ident_key: str, text_blob: str) -> bool:
            if ident_key not in blocked_cache:
                blocked_cache[ident_key] = [
                    b.casefold() for b in self.list_blocked_topics(ident_key)
                ]
            low = text_blob.casefold()
            return any(b and b in low for b in blocked_cache[ident_key])

        raw_events: list[tuple[float, dict[str, Any]]] = []
        with self.lock:
            log_rows = self.db.execute(
                f"""
                SELECT log_id, identity, source_kind, citation_json, digest, reflection, created_at
                FROM reading_logs
                WHERE status='active' AND created_at>=? AND created_at<?{id_clause}
                ORDER BY created_at DESC LIMIT ?
                """,
                (start_ts, end_ts, *id_params, lim),
            ).fetchall()
            int_rows = self.db.execute(
                f"""
                SELECT interest_id, identity, topic, summary, last_reinforced_at
                FROM staged_interests
                WHERE status='active' AND last_reinforced_at>=? AND last_reinforced_at<?{id_clause}
                ORDER BY last_reinforced_at DESC LIMIT ?
                """,
                (start_ts, end_ts, *id_params, lim),
            ).fetchall()

        for r in log_rows:
            cit = sanitize_citation(json.loads(r["citation_json"] or "{}"))
            title = strip_raw_urls(str(cit.get("title") or "")).strip() or "外部读物"
            digest = strip_raw_urls(str(r["digest"] or "")).strip()
            refl = strip_raw_urls(str(r["reflection"] or "")).strip()
            ident_key = str(r["identity"] or "")
            if _is_blocked(ident_key, f"{title} {digest}"):
                continue
            ts = float(r["created_at"] or 0.0)
            iso_ts = datetime.fromtimestamp(ts, event_tz).isoformat()
            raw_events.append(
                (
                    ts,
                    {
                        "id": f"log:{r['log_id']}",
                        "category": "horizons",
                        "source_label": "群间见闻",
                        "tone": "accent",
                        "occurred_at": iso_ts,
                        "title": f"精读《{title[:48]}》",
                        "subtitle": (refl or digest)[:120],
                        "target_plugin": PLUGIN_ID,
                        "target_route": "horizons",
                        "target_id": f"logs:{r['log_id']}",
                    },
                )
            )

        for r in int_rows:
            topic = strip_raw_urls(str(r["topic"] or "")).strip()
            summary = strip_raw_urls(str(r["summary"] or "")).strip()
            ident_key = str(r["identity"] or "")
            if not topic or _is_blocked(ident_key, f"{topic} {summary}"):
                continue
            ts = float(r["last_reinforced_at"] or 0.0)
            iso_ts = datetime.fromtimestamp(ts, event_tz).isoformat()
            raw_events.append(
                (
                    ts,
                    {
                        "id": f"interest:{r['interest_id']}",
                        "category": "horizons",
                        "source_label": "群间见闻",
                        "tone": "warm",
                        "occurred_at": iso_ts,
                        "title": f"萌发兴趣 · {topic[:40]}",
                        "subtitle": summary[:120],
                        "target_plugin": PLUGIN_ID,
                        "target_route": "horizons",
                        "target_id": f"overview:{r['interest_id']}",
                    },
                )
            )

        raw_events.sort(key=lambda pair: pair[0], reverse=True)
        return [ev for _, ev in raw_events[:lim]]
