"""Scoped reflections from member replies to Horizons-initiated discussions."""

from __future__ import annotations

from typing import Any

from .contract import normalize_identity, sha256_hex
from .sanitize import sanitize_untrusted_text, strip_raw_urls


class DiscussionStoreMixin:
    """Persistence helpers for human reflections tied to an approved reading log."""

    def record_discussion_reflection(
        self,
        *,
        identity: str,
        umo: str,
        member_id: str,
        log_id: str,
        topic: str,
        anchor_event_id: str,
        source_message_id: str,
        member_quote: str,
        reflection: str,
        source_log_revision: int,
        occurred_at: float,
        now: float,
    ) -> bool:
        """Insert once if the approved log and member-scoped consent still hold."""
        norm_id = normalize_identity(identity)
        clean_umo = str(umo or "").strip()[:512]
        clean_member = str(member_id or "").strip()[:128]
        clean_log = str(log_id or "").strip()[:128]
        clean_topic = str(topic or "").strip()[:80]
        clean_anchor = str(anchor_event_id or "").strip()[:128]
        clean_message = str(source_message_id or "").strip()[:128]
        try:
            revision = int(source_log_revision)
            happened = float(occurred_at)
            stamp = float(now)
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            not clean_umo
            or not clean_member
            or clean_member in {"bot", "system"}
            or not clean_log
            or not clean_topic
            or not clean_anchor
            or not clean_message
            or revision < 1
        ):
            return False

        quote = sanitize_untrusted_text(str(member_quote or ""), max_chars=240)
        note = sanitize_untrusted_text(str(reflection or ""), max_chars=240)
        clean_quote = strip_raw_urls(quote["text"])[:240]
        clean_reflection = strip_raw_urls(note["text"])[:240]
        if quote["blocked"] or note["blocked"] or len(clean_quote) < 4 or not clean_reflection:
            return False

        with self.lock, self.db:
            log = self.db.execute(
                "SELECT revision FROM reading_logs WHERE log_id=? AND identity=? AND status='active'",
                (clean_log, norm_id),
            ).fetchone()
            if not log or int(log["revision"]) != revision:
                return False
            if self.is_topic_blocked(clean_topic, norm_id):
                return False
            preference = self.db.execute(
                "SELECT opted_out FROM member_outreach_prefs WHERE identity=? AND umo=? AND member_id=?",
                (norm_id, clean_umo, clean_member),
            ).fetchone()
            if preference and bool(preference["opted_out"]):
                return False
            if self.db.execute(
                "SELECT 1 FROM member_topic_prefs WHERE identity=? AND umo=? AND member_id=? AND topic=? AND blocked=1",
                (norm_id, clean_umo, clean_member, clean_topic),
            ).fetchone():
                return False
            if self.db.execute(
                "SELECT 1 FROM corrections WHERE identity=? AND umo=? AND member_id=? AND log_id=? AND status='pending'",
                (norm_id, clean_umo, clean_member, clean_log),
            ).fetchone():
                return False

            reflection_id = "discussion:" + sha256_hex(
                [norm_id, clean_umo, clean_member, clean_log, clean_message]
            )[:40]
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO discussion_reflections(
                    reflection_id,identity,umo,member_id,log_id,topic,anchor_event_id,
                    source_message_id,member_quote,reflection,occurred_at,created_at,
                    status,source_log_revision
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active',?)""",
                (
                    reflection_id,
                    norm_id,
                    clean_umo,
                    clean_member,
                    clean_log,
                    clean_topic,
                    clean_anchor,
                    clean_message,
                    clean_quote,
                    clean_reflection,
                    happened,
                    stamp,
                    revision,
                ),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _format_discussion_reflection(row) -> dict[str, Any]:
        return {
            "reflection_id": row["reflection_id"],
            "identity": row["identity"],
            "umo": row["umo"],
            "member_id": row["member_id"],
            "log_id": row["log_id"],
            "topic": row["topic"],
            "anchor_event_id": row["anchor_event_id"],
            "source_message_id": row["source_message_id"],
            "member_quote": row["member_quote"],
            "reflection": row["reflection"],
            "occurred_at": float(row["occurred_at"]),
            "created_at": float(row["created_at"]),
            "source_log_revision": int(row["source_log_revision"]),
        }

    def list_discussion_reflections(
        self,
        identity: str,
        *,
        log_id: str = "",
        umo: str = "",
        member_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        norm_id = normalize_identity(identity)
        where = [
            "d.identity=?",
            "d.status='active'",
            "l.status='active'",
            "d.source_log_revision=l.revision",
        ]
        params: list[Any] = [norm_id]
        for column, value in (("d.log_id", log_id), ("d.umo", umo), ("d.member_id", member_id)):
            clean = str(value or "").strip()
            if clean:
                where.append(f"{column}=?")
                params.append(clean)
        params.append(max(1, min(200, int(limit or 20))))
        with self.lock:
            rows = self.db.execute(
                f"""SELECT d.* FROM discussion_reflections d
                    JOIN reading_logs l ON l.log_id=d.log_id AND l.identity=d.identity
                    WHERE {' AND '.join(where)}
                    ORDER BY d.occurred_at DESC,d.reflection_id DESC LIMIT ?""",
                params,
            ).fetchall()
        return [self._format_discussion_reflection(row) for row in rows]

    def _list_discussion_reflections_for_logs(
        self, identity: str, log_ids: list[str], *, limit_per_log: int = 5
    ) -> dict[str, list[dict[str, Any]]]:
        """Load a bounded set for the admin reading-log view in one batched query."""
        norm_id = normalize_identity(identity)
        clean_ids = list(dict.fromkeys(str(lid or "").strip() for lid in log_ids if str(lid or "").strip()))
        result: dict[str, list[dict[str, Any]]] = {lid: [] for lid in clean_ids}
        if not clean_ids:
            return result
        per_limit = max(1, min(200, int(limit_per_log or 5)))
        placeholders = ",".join("?" for _ in clean_ids)
        with self.lock:
            rows = self.db.execute(
                f"""
                WITH ranked AS (
                    SELECT d.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY d.log_id
                               ORDER BY d.occurred_at DESC, d.reflection_id DESC
                           ) AS rn
                    FROM discussion_reflections d
                    JOIN reading_logs l ON l.log_id=d.log_id AND l.identity=d.identity
                    WHERE d.identity=?
                      AND d.status='active'
                      AND l.status='active'
                      AND l.revision=d.source_log_revision
                      AND d.log_id IN ({placeholders})
                )
                SELECT * FROM ranked WHERE rn<=? ORDER BY occurred_at DESC, reflection_id DESC
                """,
                [norm_id, *clean_ids, per_limit],
            ).fetchall()
        for row in rows:
            result.setdefault(str(row["log_id"]), []).append(
                self._format_discussion_reflection(row)
            )
        return result

