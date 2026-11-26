"""Zero-I/O in-memory projection cache for Chat Dynamics decision and reply paths."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .contract import (
    DECISION_SLOT_MAX_CHARS,
    REPLY_PROJECTION_MAX_CHARS,
    SCHEMA_VERSION,
    make_identity,
    normalize_identity,
    unpack_identity,
)
from .sanitize import contains_raw_url, sanitize_citation, strip_raw_urls

_STATUS_INQUIRY_RE = re.compile(
    r"(?:"
    r"(?:最近|这两天|近来|平时|空闲|闲着|今天|晚上)[^\n。！？!?]{0,8}"
    r"(?:在看什么|看什么|读什么|刷什么|刷到什么|看到什么|看啥|读啥|忙什么|忙啥|关注什么|追什么|在追什么|聊什么|聊啥|想聊什么|想聊啥"
    r"|(?<![没不别])有(?:没有)?(?:什么|啥)(?:好玩|有意思|有趣|新鲜|值得聊|可聊|想聊|话题|见闻))"
    r"|(?:在看什么书|看什么书|读什么书|在读什么|刷到什么(?:视频|好玩的|有意思的|有趣的)?"
    r"|(?<![没不别])有(?:没有)?(?:什么|啥)推荐的(?:书|小说|视频|文章|读物|话题)"
    r"|推荐(?:点|些|个|几个)?(?:有意思|有趣|好玩)的?(?:话题|书|视频|文章))"
    r"|(?:(?:(?<![没不别])有(?:没有)?(?:什么|啥)|找(?:个|点)?|聊(?:点|个|聊)?(?:什么|啥)?|说说|分享(?:点|个|下)?)[^\n。！？!?]{0,6}"
    r"(?:(?:好玩|有意思|有趣|新鲜|值得聊|可聊|想聊|新)的?(?:话题|见闻|事儿?|新鲜事)|话题(?:吗|呀|啊|呢|聊|可以聊|能聊|推荐)))"
    r"|(?:(?<![没不别])有(?:没有)?(?:什么|啥)(?:好玩|有意思|有趣|值得聊|想聊)的(?:可以聊|能聊|聊|分享))"
    r"|what\s+have\s+you\s+been\s+(?:reading|watching)"
    r"|any\s+interesting\s+topics?"
    r")",
    re.IGNORECASE,
)

_CJK_STOPWORDS = frozenset(
    {
        "今天",
        "明天",
        "昨天",
        "现在",
        "刚才",
        "以前",
        "以后",
        "最近",
        "时候",
        "时间",
        "什么",
        "怎么",
        "怎样",
        "为什么",
        "哪里",
        "哪个",
        "哪些",
        "多少",
        "几个",
        "这个",
        "那个",
        "这些",
        "那些",
        "这里",
        "那里",
        "这么",
        "那么",
        "这样",
        "那样",
        "因为",
        "所以",
        "但是",
        "不过",
        "如果",
        "虽然",
        "而且",
        "或者",
        "还是",
        "只是",
        "觉得",
        "感觉",
        "以为",
        "知道",
        "明白",
        "发现",
        "看到",
        "听到",
        "出来",
        "进去",
        "可以",
        "能够",
        "应该",
        "需要",
        "可能",
        "大概",
        "也许",
        "肯定",
        "一定",
        "真的",
        "没有",
        "不是",
        "就是",
        "都是",
        "也是",
        "还是",
        "已经",
        "正在",
        "一直",
        "一起",
        "我们",
        "你们",
        "他们",
        "她们",
        "大家",
        "自己",
        "别人",
        "有人",
        "群友",
        "群里",
        "一下",
        "一个",
        "一些",
        "一点",
        "一种",
        "一样",
        "一般",
        "非常",
        "特别",
        "比较",
        "哈哈",
        "嘿嘿",
        "呜呜",
        "啊啊",
        "哦哦",
        "嗯嗯",
        "好的",
        "行吧",
        "算了",
        "喜欢",
        "问题",
        "东西",
        "事情",
        "地方",
        "方面",
        "内容",
        "意思",
        "情况",
        "开始",
        "结束",
    }
)

_LATIN_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "have",
        "from",
        "what",
        "when",
        "where",
        "which",
        "about",
        "just",
        "like",
        "your",
        "they",
        "them",
        "been",
        "were",
        "will",
        "would",
        "could",
        "should",
        "into",
        "some",
        "more",
        "very",
    }
)

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_LATIN_WORD_RE = re.compile(r"[a-zA-Z0-9_+#.-]{3,}")


def _extract_features(text: str) -> tuple[set[str], set[str]]:
    """Return (strong_tokens_3plus_or_words, cjk_bigrams) excluding generic stopwords."""
    strong: set[str] = set()
    bigrams: set[str] = set()
    lowered = str(text or "").casefold()

    for m in _LATIN_WORD_RE.finditer(lowered):
        w = m.group(0).strip("._-")
        if len(w) >= 3 and w not in _LATIN_STOPWORDS and not w.isdigit():
            strong.add(w)

    for m in _CJK_RUN_RE.finditer(lowered):
        run = m.group(0)
        if 2 <= len(run) <= 8 and run not in _CJK_STOPWORDS:
            if len(run) >= 3:
                strong.add(run)
        for n in (2, 3, 4):
            if len(run) >= n:
                for idx in range(len(run) - n + 1):
                    gram = run[idx : idx + n]
                    if gram in _CJK_STOPWORDS:
                        continue
                    if n == 2:
                        bigrams.add(gram)
                    else:
                        strong.add(gram)
    return strong, bigrams


def _empty_result(reason: str = "") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "matched": False,
        "match_reason": reason,
        "decision_slot": None,
        "reply_projection": None,
        "versions": [],
    }


class ProjectionCache:
    """Lock-free atomic snapshot reader for synchronous reply-path lookups."""

    def __init__(self) -> None:
        self._snapshot: dict[str, Any] = {
            "built_at": 0.0,
            "by_identity": {},
            "summary": {},
        }
        self._umo_identities: dict[str, str] = {}
        self._persona_aliases: dict[str, str] = {}
        self.generation: int = 0

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        by_id = snapshot.get("by_identity", {})
        aliases = dict(self._persona_aliases)
        for ident in by_id:
            persona_id, _ = unpack_identity(ident)
            aliases.setdefault(persona_id, ident)
            aliases[ident] = ident
        self._persona_aliases = aliases
        self._snapshot = snapshot
        self.generation += 1

    def bind_umo_identity(self, umo: str, identity: str) -> None:
        clean_umo = str(umo or "").strip()
        if clean_umo and identity:
            norm = normalize_identity(identity)
            self._umo_identities[clean_umo] = norm
            persona_id, _ = unpack_identity(norm)
            self._persona_aliases[persona_id] = norm
            self._persona_aliases[norm] = norm

    def bind_persona_alias(self, alias: str, identity: str) -> None:
        clean_alias = str(alias or "").strip()
        if clean_alias and identity:
            self._persona_aliases[clean_alias] = normalize_identity(identity)

    def summary(self) -> dict[str, Any]:
        return dict(self._snapshot.get("summary") or {})

    def resolve_identity(
        self,
        *,
        umo: str = "",
        persona: str = "",
        character_scope: dict[str, Any] | None = None,
    ) -> str | None:
        by_id = self._snapshot.get("by_identity") or {}
        if isinstance(character_scope, dict) and character_scope.get("persona_id"):
            p = str(character_scope["persona_id"]).strip()
            inst = str(character_scope.get("instance_id") or p).strip()
            return make_identity(p, inst)

        if persona:
            p_clean = str(persona).strip()
            if p_clean in by_id:
                return p_clean
            if p_clean in self._persona_aliases:
                return self._persona_aliases[p_clean]
            # If persona is a plain persona_id (not a 64-hex hash), treat it as explicit identity
            if not re.fullmatch(r"[0-9a-f]{64}", p_clean):
                candidate = normalize_identity(p_clean)
                if candidate in by_id:
                    return candidate
                # Explicit unknown persona_id must NOT steal another character's data!
                return candidate

        if persona:
            return None
        if umo and umo in self._umo_identities:
            return self._umo_identities[umo]

        return None

    def query(
        self,
        *,
        enabled: bool,
        ready: bool,
        allowed_sessions: list[str] | None,
        scope: str | dict[str, Any],
        persona: str = "",
        member: str = "",
        query: str = "",
        nearby_text: str = "",
        character_scope: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not enabled or not ready:
            return _empty_result("disabled")

        if isinstance(scope, dict):
            umo = str(scope.get("umo") or "").strip()
            if character_scope is None and scope.get("persona_id"):
                character_scope = scope
        else:
            umo = str(scope or "").strip()

        if not umo:
            return _empty_result("missing_scope")

        whitelist = [
            str(s).strip() for s in (allowed_sessions or []) if str(s or "").strip()
        ]
        if whitelist and umo not in whitelist:
            return _empty_result("scope_not_allowed")

        ident = self.resolve_identity(
            umo=umo, persona=persona, character_scope=character_scope
        )
        if not ident:
            return _empty_result("no_identity")

        bucket = (self._snapshot.get("by_identity") or {}).get(ident)
        if not bucket:
            return _empty_result("empty_state")

        stamp = float(now if now is not None else time.time())
        q_str = str(query or "").strip()
        near_str = str(nearby_text or "").strip()
        if not q_str and not near_str:
            return _empty_result("empty_query")

        combined_lower = f"{q_str}\n{near_str}".casefold()
        for blocked in bucket.get("blocked") or []:
            if blocked and blocked in combined_lower:
                return _empty_result("blocked_topic")

        interests = [
            i
            for i in (bucket.get("interests") or [])
            if i.get("status") == "active" and float(i.get("expires_at", 0)) > stamp
        ]
        suppressed = {
            r["log_id"]
            for r in bucket.get("corrections", [])
            if r["umo"] == umo and r["member_id"] == member
        }
        blocked_member_topics = {
            r["topic"]
            for r in bucket.get("member_topics", [])
            if r["umo"] == umo and r["member_id"] == member
        }
        interests = [
            i
            for i in interests
            if i["topic"] not in blocked_member_topics
            and not suppressed.intersection(i.get("evidence_log_ids", []))
        ]
        # Discussion reflections are private to the exact character, group and
        # member that produced them. Filter before ranking so another member's
        # reply can never influence the text sent to this turn's model.
        blocked_lower = {str(topic).casefold() for topic in bucket.get("blocked") or []}
        member_blocked_lower = {str(topic).casefold() for topic in blocked_member_topics}
        reflections_by_log: dict[str, list[dict[str, Any]]] = {}
        if member:
            for note in bucket.get("discussion_reflections") or []:
                note_topic = str(note.get("topic") or "").strip()
                log_id = str(note.get("log_id") or "")
                if (
                    note.get("status", "active") != "active"
                    or str(note.get("identity") or "") != ident
                    or str(note.get("umo") or "") != umo
                    or str(note.get("member_id") or "") != member
                    or not log_id
                    or log_id in suppressed
                    or note_topic.casefold() in blocked_lower
                    or note_topic.casefold() in member_blocked_lower
                ):
                    continue
                reflections_by_log.setdefault(log_id, []).append(note)

        logs = []
        for row in bucket.get("logs") or []:
            if row["log_id"] in suppressed:
                continue
            # Copy the snapshot row; never write member text into the persona-wide
            # reading reflection stored on the reading log.
            scoped = dict(row)
            scoped["discussion_reflections"] = sorted(
                reflections_by_log.get(row["log_id"], []),
                key=lambda note: float(note.get("created_at") or 0),
                reverse=True,
            )[:2]
            logs.append(scoped)
        books = [
            b for b in (bucket.get("books") or []) if b.get("chapter_cursor", 0) > 0
        ]

        # Filter questions strictly per-UMO: never offer expired or UMO-discussed questions,
        # and respect a 4-hour per-(UMO, topic) cooldown after discussing a question on that topic.
        recently_discussed_topics: set[str] = set()
        eligible_questions: list[dict[str, Any]] = []
        for q in bucket.get("questions") or []:
            if q.get("log_id") in suppressed or q.get("topic") in blocked_member_topics:
                continue
            if float(q.get("expires_at", 0)) <= stamp:
                continue
            discussed_map = q.get("discussed_umos") or {}
            if umo in discussed_map:
                if stamp - float(discussed_map[umo]) < 14400.0 and q.get("topic"):
                    recently_discussed_topics.add(str(q["topic"]).casefold())
                continue
            eligible_questions.append(q)

        eligible_questions = [
            q
            for q in eligible_questions
            if str(q.get("topic") or "").casefold() not in recently_discussed_topics
        ]

        # 1. Check Status-Inquiry / Topic-Inquiry Cues
        # (e.g. "最近在看什么", "最近有什么有趣的话题吗", "有什么有意思的话题", "看什么书", "刷到什么")
        if _STATUS_INQUIRY_RE.search(q_str):
            if not interests and not logs and not books and not eligible_questions:
                return _empty_result("empty_state")
            chosen_interests = interests[:2]
            chosen_logs = logs[:2]
            chosen_qs = self._select_diverse_questions(eligible_questions, limit=3)
            return self._pack_projection(
                match_reason="status_inquiry",
                interests=chosen_interests,
                logs=chosen_logs,
                books=books[:1],
                questions=chosen_qs,
            )

        # 2. Topic / Keyword Matching
        q_lower = q_str.casefold()
        near_lower = near_str.casefold()
        q_strong, q_bi = _extract_features(q_str)
        near_strong, near_bi = _extract_features(near_str)
        all_strong = q_strong | near_strong
        all_bi = q_bi | near_bi

        if not all_strong and not all_bi:
            return _empty_result("no_match")

        def score_text(primary_label: str, body_text: str) -> float:
            score = 0.0
            p_low = primary_label.strip().casefold()
            if (
                len(p_low) >= 2
                and p_low not in _CJK_STOPWORDS
                and p_low not in _LATIN_STOPWORDS
            ):
                if p_low in q_lower:
                    score += 5.0
                elif p_low in near_lower:
                    score += 3.0
            t_strong, t_bi = _extract_features(f"{primary_label} {body_text}")
            q_strong_hits = q_strong & t_strong
            near_strong_hits = near_strong & t_strong
            q_bi_hits = q_bi & t_bi
            near_bi_hits = near_bi & t_bi

            if q_strong_hits:
                score += 2.6 + 0.8 * min(3, len(q_strong_hits) - 1)
            elif near_strong_hits:
                score += 2.2 + 0.5 * min(2, len(near_strong_hits) - 1)

            if len(q_bi_hits) >= 2:
                score += 2.4 + 0.4 * min(3, len(q_bi_hits) - 2)
            elif len(q_bi_hits) == 1 and len(near_bi_hits) >= 2:
                score += 2.2
            elif (
                len(q_bi_hits) == 1
                and p_low
                and any(bg in p_low for bg in q_bi_hits)
                and len(p_low) <= 4
            ):
                score += 2.3
            return score

        scored_interests: list[tuple[float, dict[str, Any]]] = []
        for item in interests:
            s = score_text(item["topic"], item["summary"]) * (
                0.85 + 0.15 * min(2.0, float(item.get("heat_score", 1.0)))
            )
            if s >= 2.2:
                scored_interests.append((s, item))
        scored_interests.sort(key=lambda x: -x[0])

        scored_logs: list[tuple[float, dict[str, Any]]] = []
        for item in logs:
            cit = item["citation"]
            title_or_creator = (
                f"{cit.get('title', '')} {cit.get('creator', '')}".strip()
            )
            scoped_discussion_text = " ".join(
                f"{note.get('topic', '')} {note.get('member_quote', '')} {note.get('reflection', '')}"
                for note in item.get("discussion_reflections", [])
            )
            s = max(
                score_text(
                    cit.get("title", ""),
                    f"{item['digest']} {item['reflection']} {scoped_discussion_text}",
                ),
                score_text(cit.get("creator", ""), cit.get("title", ""))
                if len(cit.get("creator", "")) >= 2
                else 0.0,
            )
            if title_or_creator and s >= 2.2:
                scored_logs.append((s, item))
        scored_logs.sort(key=lambda x: -x[0])

        scored_questions: list[tuple[float, dict[str, Any]]] = []
        matched_topic_names = {i["topic"].casefold() for _, i in scored_interests}
        matched_log_ids = {log_item["log_id"] for _, log_item in scored_logs}
        for q in eligible_questions:
            s = score_text(q["topic"], q["question"])
            if q["topic"].casefold() in matched_topic_names:
                s = max(s, 3.0)
            if q.get("log_id") in matched_log_ids:
                s = max(s, 2.8)
            if s >= 2.2:
                scored_questions.append((s, q))
        scored_questions.sort(key=lambda x: -x[0])

        scored_books: list[tuple[float, dict[str, Any]]] = []
        for b in books:
            s = score_text(b["title"], f"{b['creator']} {b['last_reflection']}")
            if s >= 2.2:
                scored_books.append((s, b))
        scored_books.sort(key=lambda x: -x[0])

        if (
            not scored_interests
            and not scored_logs
            and not scored_questions
            and not scored_books
        ):
            return _empty_result("no_match")

        chosen_interests = [x[1] for x in scored_interests[:2]]
        chosen_logs = [x[1] for x in scored_logs[:2]]
        chosen_q = scored_questions[0][1] if scored_questions else None
        chosen_books = [x[1] for x in scored_books[:1]]

        # If matched via interest/question without a direct log match, attach the most recent related log for context
        if not chosen_logs and logs:
            target_log_id = chosen_q.get("log_id") if chosen_q else ""
            related = next(
                (log_item for log_item in logs if log_item["log_id"] == target_log_id),
                None,
            )
            if related is None and chosen_interests:
                top_t = chosen_interests[0]["topic"].casefold()
                related = next(
                    (
                        log_item
                        for log_item in logs
                        if top_t
                        in f"{log_item['citation']['title']} {log_item['digest']}".casefold()
                    ),
                    None,
                )
            if related is not None:
                chosen_logs = [related]

        return self._pack_projection(
            match_reason="topic_match",
            interests=chosen_interests,
            logs=chosen_logs,
            books=chosen_books,
            question=chosen_q,
        )

    @staticmethod
    def _select_diverse_questions(
        eligible_questions: list[dict[str, Any]], *, limit: int = 3
    ) -> list[dict[str, Any]]:
        """Pick up to `limit` questions, preferring distinct topics first."""
        cap = max(1, int(limit))
        picked: list[dict[str, Any]] = []
        seen_topics: set[str] = set()
        overflow: list[dict[str, Any]] = []
        for q in eligible_questions:
            topic_key = str(q.get("topic") or "").strip().casefold()
            if topic_key and topic_key in seen_topics:
                overflow.append(q)
                continue
            if topic_key:
                seen_topics.add(topic_key)
            picked.append(q)
            if len(picked) >= cap:
                return picked
        for q in overflow:
            picked.append(q)
            if len(picked) >= cap:
                break
        return picked

    def query_for_tool(
        self,
        *,
        enabled: bool,
        ready: bool,
        allowed_sessions: list[str] | None,
        scope: str | dict[str, Any],
        persona: str = "",
        member: str = "",
        kind: str = "questions",
        query: str = "",
        limit: int = 5,
        character_scope: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Read-only, URL-free lookup for the `query_horizons_topics` LLM tool."""
        if not enabled or not ready:
            return {"available": False, "reason": "disabled"}

        if isinstance(scope, dict):
            umo = str(scope.get("umo") or "").strip()
            if character_scope is None and scope.get("persona_id"):
                character_scope = scope
        else:
            umo = str(scope or "").strip()
        if not umo:
            return {"available": False, "reason": "missing_scope"}

        whitelist = [
            str(s).strip() for s in (allowed_sessions or []) if str(s or "").strip()
        ]
        if whitelist and umo not in whitelist:
            return {"available": False, "reason": "scope_not_allowed"}

        ident = self.resolve_identity(
            umo=umo, persona=persona, character_scope=character_scope
        )
        if not ident:
            return {"available": False, "reason": "no_identity"}

        bucket = (self._snapshot.get("by_identity") or {}).get(ident)
        if not bucket:
            return {
                "available": True,
                "fact_kind": "external_media",
                "questions": [],
                "interests": [],
                "readings": [],
                "reason": "empty_state",
            }

        stamp = float(now if now is not None else time.time())
        clean_kind = str(kind or "questions").strip().lower()
        if clean_kind not in {"questions", "topics", "readings"}:
            clean_kind = "questions"
        try:
            lim = max(1, min(8, int(limit)))
        except (TypeError, ValueError):
            lim = 5
        q_str = str(query or "").strip()
        q_lower = q_str.casefold()

        for blocked in bucket.get("blocked") or []:
            if blocked and q_lower and blocked in q_lower:
                return {"available": False, "reason": "blocked_topic"}

        suppressed = {
            r["log_id"]
            for r in bucket.get("corrections", [])
            if r["umo"] == umo and r["member_id"] == member
        }
        blocked_member_topics = {
            r["topic"]
            for r in bucket.get("member_topics", [])
            if r["umo"] == umo and r["member_id"] == member
        }
        blocked_member_lower = {
            str(t).strip().casefold() for t in blocked_member_topics if str(t).strip()
        }
        if q_lower and any(bt in q_lower for bt in blocked_member_lower):
            return {"available": False, "reason": "blocked_topic"}

        interests = [
            i
            for i in (bucket.get("interests") or [])
            if i.get("status") == "active"
            and float(i.get("expires_at", 0)) > stamp
            and i["topic"] not in blocked_member_topics
            and not suppressed.intersection(i.get("evidence_log_ids", []))
        ]
        logs = [
            row for row in (bucket.get("logs") or []) if row["log_id"] not in suppressed
        ]
        log_title_by_id = {
            row["log_id"]: strip_raw_urls(
                sanitize_citation(row.get("citation")).get("title", "")
            )[:60]
            for row in logs
        }

        recently_discussed_topics: set[str] = set()
        eligible_questions: list[dict[str, Any]] = []
        for q in bucket.get("questions") or []:
            if q.get("log_id") in suppressed or q.get("topic") in blocked_member_topics:
                continue
            if float(q.get("expires_at", 0)) <= stamp:
                continue
            discussed_map = q.get("discussed_umos") or {}
            if umo in discussed_map:
                if stamp - float(discussed_map[umo]) < 14400.0 and q.get("topic"):
                    recently_discussed_topics.add(str(q["topic"]).casefold())
                continue
            eligible_questions.append(q)
        eligible_questions = [
            q
            for q in eligible_questions
            if str(q.get("topic") or "").casefold() not in recently_discussed_topics
        ]

        q_strong, q_bi = _extract_features(q_str) if q_str else (set(), set())

        def _score_match(primary: str, body: str) -> float:
            if not q_str:
                return 1.0
            p_low = str(primary or "").strip().casefold()
            b_low = str(body or "").strip().casefold()
            score = 0.0
            if len(q_lower) >= 2 and (q_lower in p_low or q_lower in b_low):
                score += 5.0
            if (
                len(p_low) >= 2
                and p_low not in _CJK_STOPWORDS
                and p_low not in _LATIN_STOPWORDS
                and p_low in q_lower
            ):
                score += 4.0
            t_strong, t_bi = _extract_features(f"{primary} {body}")
            if q_strong & t_strong:
                score += 2.6 + 0.6 * min(3, len(q_strong & t_strong) - 1)
            if len(q_bi & t_bi) >= 2:
                score += 2.2
            elif len(q_bi & t_bi) == 1 and p_low and any(bg in p_low for bg in (q_bi & t_bi)):
                score += 2.0
            return score

        matched_by = "recent"
        if q_str:
            scored_q = [
                (s, q)
                for q in eligible_questions
                if (s := _score_match(q.get("topic", ""), q.get("question", ""))) >= 2.0
            ]
            scored_q.sort(key=lambda x: -x[0])
            scored_i = [
                (s, i)
                for i in interests
                if (s := _score_match(i.get("topic", ""), i.get("summary", ""))) >= 2.0
            ]
            scored_i.sort(key=lambda x: -x[0])
            scored_l = [
                (s, row)
                for row in logs
                if (
                    s := _score_match(
                        f"{row.get('citation', {}).get('title', '')} {row.get('citation', {}).get('creator', '')}",
                        f"{row.get('digest', '')} {row.get('reflection', '')}",
                    )
                )
                >= 2.0
            ]
            scored_l.sort(key=lambda x: -x[0])
            if scored_q or scored_i or scored_l:
                matched_by = "query"
                sel_questions = [x[1] for x in scored_q[:lim]]
                sel_interests = [x[1] for x in scored_i[: min(3, lim)]]
                sel_logs = [x[1] for x in scored_l[: min(3, lim)]]
            else:
                matched_by = "recent_fallback"
                sel_questions = self._select_diverse_questions(
                    eligible_questions, limit=min(3, lim)
                )
                sel_interests = interests[: min(2, lim)]
                sel_logs = logs[: min(2, lim)]
        else:
            sel_questions = self._select_diverse_questions(eligible_questions, limit=lim)
            sel_interests = interests[: min(3, lim)]
            sel_logs = logs[: min(lim if clean_kind == "readings" else 3, lim)]

        out_questions: list[dict[str, str]] = []
        out_interests: list[dict[str, Any]] = []
        out_readings: list[dict[str, Any]] = []
        offer_qids: list[str] = []
        offer_log_ids: list[str] = []
        offer_topics: list[str] = []

        if clean_kind in {"questions", "topics"}:
            for q in sel_questions[:lim]:
                qt = strip_raw_urls(q.get("topic", ""))[:40]
                qq = strip_raw_urls(q.get("question", ""))[:120]
                qid = str(q.get("question_id") or "")[:128]
                lid = str(q.get("log_id") or "")[:128]
                item: dict[str, str] = {
                    "question_id": qid,
                    "topic": qt,
                    "question": qq,
                }
                src_title = log_title_by_id.get(lid, "")
                if src_title:
                    item["source_title"] = src_title
                out_questions.append(item)
                if qid and qid not in offer_qids:
                    offer_qids.append(qid)
                if lid and lid.startswith("log_") and lid not in offer_log_ids:
                    offer_log_ids.append(lid)
                if qt and qt not in offer_topics:
                    offer_topics.append(qt)

        if clean_kind in {"questions", "topics"}:
            i_cap = 2 if clean_kind == "questions" else min(3, lim)
            for i in sel_interests[:i_cap]:
                t = strip_raw_urls(i.get("topic", ""))[:40]
                out_interests.append(
                    {
                        "topic": t,
                        "summary": strip_raw_urls(i.get("summary", ""))[:120],
                    }
                )
                if t and t not in offer_topics:
                    offer_topics.append(t)

        if clean_kind in {"topics", "readings"} or (
            clean_kind == "questions" and not out_questions
        ):
            r_cap = lim if clean_kind == "readings" else min(3, lim)
            for r in sel_logs[:r_cap]:
                cit = sanitize_citation(r.get("citation"))
                lid = str(r.get("log_id") or "")[:128]
                out_readings.append(
                    {
                        "log_id": lid,
                        "source_kind": str(r.get("source_kind") or ""),
                        "citation": cit,
                        "digest": strip_raw_urls(r.get("digest", ""))[:140],
                        "reflection": strip_raw_urls(r.get("reflection", ""))[:110],
                    }
                )
                if lid and lid.startswith("log_") and lid not in offer_log_ids:
                    offer_log_ids.append(lid)
                title = cit.get("title", "")[:40]
                if title and title not in offer_topics:
                    offer_topics.append(title)

        payload: dict[str, Any] = {
            "available": True,
            "fact_kind": "external_media",
            "kind": clean_kind,
            "matched_by": matched_by,
            "policy": (
                "这是角色在群间见闻里积累的外部见闻与待讨论问题库。"
                "群友问最近有什么有趣或有意思的话题时，可从中自然挑 1-2 个用角色自己的话展开，不必照念清单。"
                "转述作品内容时交代来自外部作品，不编造出处，不发裸链接。"
            ),
            "questions": out_questions,
            "interests": out_interests,
            "readings": out_readings,
            "_offer": {
                "question_ids": offer_qids[:8],
                "log_ids": offer_log_ids[:4],
                "topics": offer_topics[:12],
            },
        }
        if contains_raw_url(payload):
            return {"available": False, "reason": "url_guard_blocked"}
        return payload

    def _pack_projection(
        self,
        *,
        match_reason: str,
        interests: list[dict[str, Any]],
        logs: list[dict[str, Any]],
        books: list[dict[str, Any]],
        question: dict[str, Any] | None = None,
        questions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        versions: list[str] = []
        topics: list[str] = []

        proj_interests: list[dict[str, Any]] = []
        for i in interests[:2]:
            t = strip_raw_urls(i["topic"])[:40]
            if t and t not in topics:
                topics.append(t)
            proj_interests.append(
                {
                    "interest_id": i["interest_id"],
                    "topic": t,
                    "summary": strip_raw_urls(i["summary"])[:120],
                    "heat": round(float(i.get("heat_score", 1.0)), 2),
                }
            )
            versions.append(
                f"interest:{i['interest_id']}:{int(i.get('expires_at', 0))}"
            )

        proj_readings: list[dict[str, Any]] = []
        for r in logs[:2]:
            cit = sanitize_citation(r.get("citation"))
            proj_readings.append(
                {
                    "log_id": r["log_id"],
                    "source_kind": r["source_kind"],
                    "digest": strip_raw_urls(r["digest"])[:160],
                    "reflection": strip_raw_urls(r["reflection"])[:120],
                    "citation": cit,
                    "discussion_reflections": [
                        {
                            "reflection_id": str(note.get("reflection_id") or "")[:96],
                            "topic": strip_raw_urls(str(note.get("topic") or ""))[:48],
                            "member_id": str(note.get("member_id") or "")[:64],
                            "member_quote": strip_raw_urls(str(note.get("member_quote") or ""))[:100],
                            "reflection": strip_raw_urls(str(note.get("reflection") or ""))[:120],
                        }
                        for note in r.get("discussion_reflections", [])[:2]
                    ],
                }
            )
            versions.append(f"log:{r['log_id']}")
            versions.extend(
                f"discussion:{note['reflection_id']}"
                for note in proj_readings[-1]["discussion_reflections"]
                if note["reflection_id"]
            )

        if not proj_readings and books:
            b = books[0]
            proj_readings.append(
                {
                    "log_id": f"book:{b['book_id']}",
                    "source_kind": "ebook",
                    "digest": f"正在连载阅读《{strip_raw_urls(b['title'])[:60]}》，已读到第 {b['chapter_cursor']}/{b['total_chapters']} 节。",
                    "reflection": strip_raw_urls(b.get("last_reflection") or "")[:120],
                    "citation": sanitize_citation(
                        {
                            "title": b["title"],
                            "creator": b["creator"],
                            "bvid": "",
                            "chapter_label": f"{b['chapter_cursor']}/{b['total_chapters']}",
                        }
                    ),
                }
            )
            versions.append(f"book:{b['book_id']}:{b['chapter_cursor']}")

        proj_questions: list[dict[str, str]] = []
        q_slot: dict[str, str] | None = None
        raw_questions: list[dict[str, Any]] = []
        if questions:
            raw_questions.extend(q for q in questions if isinstance(q, dict))
        elif question is not None:
            raw_questions.append(question)
        for q_item in raw_questions[:3]:
            qt = strip_raw_urls(q_item["topic"])[:36]
            qq = strip_raw_urls(q_item["question"])[:100]
            if qt and qt not in topics:
                topics.append(qt)
            q_obj = {
                "question_id": q_item["question_id"],
                "topic": qt,
                "question": qq,
            }
            proj_questions.append(q_obj)
            if q_slot is None:
                q_slot = {
                    "question_id": q_item["question_id"],
                    "topic": qt[:28],
                    "question": qq[:80],
                }
            versions.append(f"question:{q_item['question_id']}")

        if not topics and proj_readings:
            t_fallback = proj_readings[0]["citation"]["title"][:32]
            if t_fallback:
                topics.append(t_fallback)

        # Build bounded decision_slot (<= 500 serialized chars)
        interest_brief = proj_interests[0]["summary"][:90] if proj_interests else ""
        reading_brief = ""
        if proj_readings:
            r0 = proj_readings[0]
            reading_brief = f"{r0['citation']['title'][:36]}：{r0['reflection'][:60] or r0['digest'][:60]}"

        decision_slot: dict[str, Any] = {
            "fact_kind": "external_media",
            "match_reason": match_reason,
            "topics": topics[:2],
        }
        if interest_brief:
            decision_slot["interest"] = interest_brief
        if reading_brief:
            decision_slot["recent_reading"] = reading_brief
        if q_slot is not None:
            decision_slot["open_question"] = q_slot

        decision_slot = _fit_decision_slot(decision_slot)

        # Build bounded reply_projection (<= 1800 serialized chars, ZERO raw URLs)
        reply_projection: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "fact_kind": "external_media",
            "match_reason": match_reason,
            "policy": (
                "见闻感悟供参考。契合时可用角色自己的话融入一个想法，平时无需汇报看过什么或强行追问；"
                "若对方主动问最近有什么有趣或有意思的话题，可直接从 questions 或见闻里自然挑一个展开。"
                "转述作品内容时交代其来自作品。摘要和感悟不能作原句引用，笔记里的想象不算亲历。"
                "群友回应与交流后记是特定会话中的参考，不能当作事实或自动变成固定人设。"
                "有人问阅读或出处时按记录答，不发裸链接。"
            ),
            "topics": topics[:3],
            "interests": proj_interests,
            "readings": proj_readings,
            "questions": proj_questions,
            "question_ids": [q["question_id"] for q in proj_questions],
        }
        reply_projection = _fit_reply_projection(reply_projection)

        # Defense-in-depth invariant check
        if contains_raw_url(decision_slot) or contains_raw_url(reply_projection):
            return _empty_result("url_guard_blocked")

        return {
            "schema_version": SCHEMA_VERSION,
            "matched": True,
            "match_reason": match_reason,
            "decision_slot": decision_slot,
            "reply_projection": reply_projection,
            "versions": versions,
        }


def _serialized_len(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def _fit_decision_slot(slot: dict[str, Any]) -> dict[str, Any]:
    out = dict(slot)
    if _serialized_len(out) <= DECISION_SLOT_MAX_CHARS:
        return out
    if "recent_reading" in out:
        out["recent_reading"] = str(out["recent_reading"])[:48]
    if _serialized_len(out) <= DECISION_SLOT_MAX_CHARS:
        return out
    out.pop("recent_reading", None)
    if _serialized_len(out) <= DECISION_SLOT_MAX_CHARS:
        return out
    if "interest" in out:
        out["interest"] = str(out["interest"])[:50]
    if _serialized_len(out) <= DECISION_SLOT_MAX_CHARS:
        return out
    out.pop("interest", None)
    if "open_question" in out and isinstance(out["open_question"], dict):
        oq = dict(out["open_question"])
        oq["question"] = str(oq.get("question") or "")[:50]
        out["open_question"] = oq
    while _serialized_len(out) > DECISION_SLOT_MAX_CHARS and out.get("topics"):
        out["topics"] = [str(out["topics"][0])[:20]]
        break
    return out


def _fit_reply_projection(proj: dict[str, Any]) -> dict[str, Any]:
    out = dict(proj)
    if _serialized_len(out) <= REPLY_PROJECTION_MAX_CHARS:
        return out
    # Keep the most recent scoped reflection useful while staying within the
    # existing prompt budget, even when a reading has several receipts.
    for reading in out.get("readings", []):
        notes = [dict(note) for note in reading.get("discussion_reflections", [])]
        if notes:
            notes = notes[:1]
            for note in notes:
                note["member_quote"] = str(note.get("member_quote") or "")[:56]
                note["reflection"] = str(note.get("reflection") or "")[:72]
            reading["discussion_reflections"] = notes
    if _serialized_len(out) <= REPLY_PROJECTION_MAX_CHARS:
        return out
    for reading in out.get("readings", []):
        reading.pop("discussion_reflections", None)
    if _serialized_len(out) <= REPLY_PROJECTION_MAX_CHARS:
        return out
    if len(out.get("readings", [])) > 1:
        out["readings"] = out["readings"][:1]
    if _serialized_len(out) <= REPLY_PROJECTION_MAX_CHARS:
        return out
    if len(out.get("questions", [])) > 1:
        out["questions"] = out["questions"][:1]
        out["question_ids"] = [
            q["question_id"] for q in out["questions"] if isinstance(q, dict) and q.get("question_id")
        ]
    if _serialized_len(out) <= REPLY_PROJECTION_MAX_CHARS:
        return out
    if len(out.get("interests", [])) > 1:
        out["interests"] = out["interests"][:1]
    if _serialized_len(out) <= REPLY_PROJECTION_MAX_CHARS:
        return out
    if out.get("readings"):
        r0 = dict(out["readings"][0])
        r0["digest"] = str(r0.get("digest") or "")[:90]
        r0["reflection"] = str(r0.get("reflection") or "")[:80]
        out["readings"] = [r0]
    if _serialized_len(out) > REPLY_PROJECTION_MAX_CHARS:
        out["policy"] = "感悟可融入当前话题。摘要不能当原句，笔记想象不算亲历。问阅读或出处时按记录答，勿发裸链接。"
    return out
