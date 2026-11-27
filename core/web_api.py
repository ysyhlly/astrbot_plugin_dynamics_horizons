"""Dashboard-authenticated Web API endpoints for the Dynamics Horizons workbench."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from astrbot.api import logger
from astrbot.api.web import json_response, request

from .contract import (
    BOOK_STATUSES,
    PLUGIN_ID,
    SOURCE_KINDS,
    clean_text,
    normalize_identity,
    unpack_identity,
)
from .io import run_io
from .lifecycle import ReviewConflict

try:
    from ..adapters import (
        fetch_item_content,
        parse_local_ebook,
        poll_source,
        validate_source_config,
    )
except Exception:  # pragma: no cover
    fetch_item_content = None
    parse_local_ebook = None
    poll_source = None
    validate_source_config = None

try:
    from ..adapters import search_public_ebooks
except Exception:  # pragma: no cover
    search_public_ebooks = None

_MAX_STD_BODY = 256 * 1024
_MAX_UPLOAD_BODY = 14 * 1024 * 1024
_MAX_EBOOK_BYTES = 10 * 1024 * 1024


def _ok(data: Any):
    return json_response({"status": "ok", "ok": True, "data": data}, status_code=200)


def _err(message: str, status_code: int = 400):
    msg = str(message or "请求失败")[:300]
    return json_response(
        {"status": "error", "ok": False, "message": msg, "error": msg, "data": None},
        status_code=status_code,
    )


def _query_param(name: str, default: str = "") -> str:
    targets = [request, getattr(request, "_request", None)]
    for target in targets:
        if target is None:
            continue
        for attr in ("query_params", "query", "args"):
            try:
                holder = getattr(target, attr, None)
            except Exception:
                continue
            if holder is not None and hasattr(holder, "get"):
                try:
                    val = holder.get(name)
                    if val is not None:
                        return str(val).strip()
                except Exception:
                    continue
    return default


async def _read_json_body(max_bytes: int = _MAX_STD_BODY) -> dict[str, Any]:
    raw_req = getattr(request, "_request", None)
    if raw_req is not None and callable(getattr(raw_req, "body", None)):
        raw_bytes = await raw_req.body()
        if len(raw_bytes) > max_bytes:
            raise ValueError("请求体体积超出上限")
        if not raw_bytes:
            return {}
        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    if callable(getattr(request, "get_data", None)):
        raw_bytes = await request.get_data()
        if len(raw_bytes) > max_bytes:
            raise ValueError("请求体体积超出上限")
        if not raw_bytes:
            return {}
        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    loader = getattr(request, "json", None)
    if callable(loader):
        payload = loader()
        if hasattr(payload, "__await__"):
            payload = await payload
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload
    return {}


def _coerce_int(val: Any, default: int, low: int = 1, high: int = 10000) -> int:
    try:
        parsed = int(val)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _normalize_media_fields(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    meta = out.get("media_meta") if isinstance(out.get("media_meta"), dict) else {}
    obs = (
        [str(x) for x in out.get("visual_observations") if isinstance(x, str) and x.strip()]
        if isinstance(out.get("visual_observations"), list)
        else []
    )
    out["watch_mode"] = str(out.get("watch_mode") or meta.get("watch_mode") or "")
    out["content_origin"] = str(
        out.get("content_origin") or meta.get("content_origin") or ""
    )
    out["visual_observations"] = obs[:8]
    out["media_meta"] = meta
    return out


class HorizonsWebAPI:
    """Registers and serves `/astrbot_plugin_dynamics_horizons/*` endpoints."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.routes: list[tuple[str, Any, list[str], str]] = []
        self.operations: set[asyncio.Task] = set()

    def register(self) -> None:
        from ..series_workspace import register_panel_api

        specs = [
            ("overview", self.overview_get, ["GET"], "群间见闻总览与兴趣池"),
            ("sources", self.sources_get, ["GET"], "群间见闻信息源与书架列表"),
            ("sources", self.sources_post, ["POST"], "群间见闻信息源与书架管理"),
            ("logs", self.logs_get, ["GET"], "群间见闻阅读手账"),
            ("reviews", self.reviews_get, ["GET"], "群间见闻审查队列与黑名单"),
            ("reviews", self.reviews_post, ["POST"], "群间见闻审查与治理操作"),
        ]
        self.routes = []
        for endpoint, handler, methods, desc in specs:
            path = f"/{PLUGIN_ID}/{endpoint}"
            register_panel_api(self.plugin, self.plugin.context.register_web_api, path, handler, methods, desc)
            self.routes.append((path, handler, methods, desc))

    def unregister(self) -> None:
        registered = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(registered, list):
            registered[:] = [
                r for r in registered if getattr(r[1], "__self__", None) is not self
            ]

    async def drain(self) -> None:
        tasks = tuple(self.operations)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _authorize(self, *, check_csrf: bool = False) -> str:
        from ..series_panels import require_series_admin

        username = await require_series_admin(
            self.plugin.context, request, check_csrf=check_csrf
        )
        return f"human:{username}"

    async def _serve(self, fn, *, max_body: int = 0, check_csrf: bool | None = None):
        try:
            enforce_csrf = (max_body > 0) if check_csrf is None else bool(check_csrf)
            if enforce_csrf:
                actor = await self._authorize(check_csrf=True)
            else:
                actor = await self._authorize()
            if self.plugin.closed or self.plugin.engine is None:
                return _err("群间见闻尚未就绪", status_code=503)
            body = await _read_json_body(max_body) if max_body > 0 else {}
            if self.plugin.closed:
                return _err("群间见闻尚未就绪", status_code=503)
            task = asyncio.create_task(fn(body, actor))
            self.operations.add(task)
            try:
                data = await task
            finally:
                self.operations.discard(task)
            return _ok(data)
        except asyncio.CancelledError:
            if self.plugin.closed:
                return _err("群间见闻正在重载，请稍后重试。", status_code=503)
            raise
        except PermissionError:
            return _err("需要 Dashboard 管理员登录", status_code=403)
        except ReviewConflict as exc:
            message = {
                "review_identity_mismatch": "该记录不属于当前角色，请刷新。",
                "stale_review_revision": "提案已有更新，请刷新后重试。",
                "reading_task_cancelled": "该提案对应的阅读任务已停止。",
            }.get(str(exc), str(exc))
            return _err(message, status_code=409)
        except (ValueError, KeyError) as exc:
            return _err(str(exc), status_code=400)
        except Exception as exc:
            logger.warning(
                "[DynamicsHorizons] Web API failed type=%s", type(exc).__name__
            )
            return _err(f"请求失败：{type(exc).__name__}", status_code=400)

    def _resolve_active_identity(
        self,
        requested: str,
        *,
        known_identities: list[str] | None = None,
        fallback_identity: str = "",
    ) -> tuple[str, list[dict[str, str]]]:
        eng = self.plugin.engine
        try:
            personas = eng.world_bridge.discover_personas(
                eng.store, eng.sessions, known_identities=known_identities
            )
        except TypeError:
            personas = eng.world_bridge.discover_personas(eng.store, eng.sessions)
        effective_req = str(requested or "").strip()
        if not effective_req and fallback_identity:
            effective_req = str(fallback_identity).strip()
        if effective_req and effective_req != "all":
            active = normalize_identity(effective_req)
            if all(p["identity"] != active for p in personas):
                pid, inst = unpack_identity(active)
                personas.insert(
                    0,
                    {
                        "identity": active,
                        "persona_id": pid,
                        "label": pid if pid == inst else f"{pid} ({inst})",
                    },
                )
        else:
            active = personas[0]["identity"]
        return active, personas

    # ------------------------------------------------------------------
    # GET /overview
    # ------------------------------------------------------------------

    async def overview_get(self):
        async def op(_body: dict[str, Any], _actor: str):
            eng = self.plugin.engine
            now = eng.clock()

            req_id = _query_param("identity", "")
            req_scope = _query_param("scope", "") or _query_param("umo", "")
            scope_identity = ""
            if not req_id and req_scope:
                binding = await run_io(eng.store.get_scope_binding, req_scope)
                if binding and binding.get("identity"):
                    scope_identity = str(binding["identity"])
                else:
                    scope_identity = eng.projection.resolve_identity(umo=req_scope)
            known_ids = await run_io(eng.store.known_identities)
            active_identity, personas = self._resolve_active_identity(
                req_id, known_identities=known_ids, fallback_identity=scope_identity
            )

            counts = await run_io(
                eng.store.summary_counts,
                active_identity,
                now=now,
                half_life_days=eng.half_life_days,
            )
            daily_used = await run_io(
                eng.store.count_today_digests, active_identity, now, tz=eng.timezone
            )
            interests = await run_io(
                eng.store.list_interests,
                active_identity,
                active_only=False,
                half_life_days=eng.half_life_days,
                now=now,
            )
            questions = await run_io(
                eng.store.list_questions_with_umo_states,
                active_identity,
                active_only=True,
                configured_umos=eng.sessions,
                now=now,
            )
            books = await run_io(eng.store.list_books, active_identity)
            wake = await eng.world_bridge.inspect_wake_and_profile(
                active_identity,
                now=now,
                quiet_hours=eng.quiet_hours,
                sessions=eng.sessions,
                tz=eng.timezone,
            )

            outreach_diag = await run_io(
                eng.store.list_outreach_diagnostics, active_identity, now=now
            )
            pending_corrections = await run_io(
                eng.store.list_pending_corrections, active_identity
            )
            health_obj = await eng.health_async(active_identity, wake)
            presets_list = [
                {
                    "id": pid,
                    "label": spec["label"],
                    "summary": spec["summary"],
                    "values": dict(spec["values"]),
                }
                for pid, spec in getattr(eng, "HORIZONS_PRESETS", {}).items()
            ]
            runtime_cfg = {
                "daily_digest_quota": int(eng.daily_quota),
                "daily_model_call_quota": int(
                    self.plugin.config.get("daily_model_call_quota", 50) or 50
                ),
                "interest_half_life_days": float(eng.half_life_days),
                "question_ttl_hours": float(eng.question_ttl_hours),
                "poll_interval_seconds": int(
                    self.plugin.config.get("poll_interval_seconds", 900) or 900
                ),
                "digest_interval_seconds": int(
                    self.plugin.config.get("digest_interval_seconds", 3600) or 3600
                ),
                "review_mode": eng.review_mode,
                "anli_daily_per_member": int(
                    self.plugin.config.get("anli_daily_per_member", 10) or 10
                ),
                "anli_daily_per_group": int(
                    self.plugin.config.get("anli_daily_per_group", 30) or 30
                ),
            }

            return {
                "health": health_obj,
                "enabled": bool(self.plugin.config.get("enabled", True)),
                "ready": bool(self.plugin.ready and not self.plugin.closed),
                "review_mode": eng.review_mode,
                "personas": personas,
                "active_identity": active_identity,
                "runtime_config": runtime_cfg,
                "presets": presets_list,
                "outreach_diagnostics": outreach_diag,
                "stats": {
                    "sources_total": counts["sources_total"],
                    "sources_enabled": counts["sources_enabled"],
                    "candidates_unread": counts["candidates_unread"],
                    "logs_total": counts["logs_total"],
                    "books_reading": counts["books_reading"],
                    "active_interests": counts["active_interests"],
                    "pending_questions": counts["pending_questions"],
                    "pending_reviews": counts["pending_reviews"],
                    "held_reviews": int(counts.get("held_reviews", 0) or 0),
                    "pending_corrections": len(pending_corrections),
                    "daily_quota": eng.daily_quota,
                    "daily_used": daily_used,
                    "last_poll_at": eng.last_poll_at,
                    "last_digest_at": eng.last_digest_at,
                    "analysis_status": eng.analysis_status,
                    "review_status": eng.review_status,
                },
                "staged_interests": [
                    {
                        "interest_id": i["interest_id"],
                        "topic": i["topic"],
                        "summary": i["summary"],
                        "heat_score": i["heat_score"],
                        "raw_heat_score": i.get("raw_heat_score", i["heat_score"]),
                        "last_reinforced_at": i.get("last_reinforced_at", 0.0),
                        "half_life_days": eng.half_life_days,
                        "expires_at": i["expires_at"],
                        "status": i["status"],
                    }
                    for i in interests
                ],
                "discussion_questions": [
                    {
                        "question_id": q["question_id"],
                        "topic": q["topic"],
                        "question": q["question"],
                        "status": q["status"],
                        "created_at": q["created_at"],
                        "expires_at": q["expires_at"],
                        "umo_states": q["umo_states"],
                    }
                    for q in questions
                ],
                "books": [
                    {
                        "book_id": b["book_id"],
                        "title": b["title"],
                        "creator": b["creator"],
                        "source_kind": b["source_kind"],
                        "provider": b["provider"],
                        "chapter_cursor": b["chapter_cursor"],
                        "total_chapters": b["total_chapters"],
                        "status": b["status"],
                    }
                    for b in books
                ],
                "world_link": wake["world_link"],
            }

        return await self._serve(op)

    # ------------------------------------------------------------------
    # GET /sources & POST /sources
    # ------------------------------------------------------------------

    async def sources_get(self):
        async def op(_body: dict[str, Any], _actor: str):
            eng = self.plugin.engine
            req_id = _query_param("identity", "")
            active_identity, _ = self._resolve_active_identity(req_id)
            if _query_param("candidates", ""):
                from .management import archive_page

                return await run_io(
                    archive_page,
                    eng.store,
                    active_identity,
                    _query_param("page", "1"),
                    _query_param("query", ""),
                    _query_param("archived", "1") != "0",
                )
            filter_id = "" if req_id == "all" else active_identity
            sources = await run_io(eng.store.list_sources, filter_id, mask=True)
            books = await run_io(eng.store.list_books, filter_id)
            return {
                "identity": active_identity,
                "sources": [
                    {
                        "source_id": s["source_id"],
                        "source_kind": s["source_kind"],
                        "identity": s["identity"],
                        "name": s["name"],
                        "enabled": s["enabled"],
                        "status": s["status"],
                        "last_error": s["last_error"],
                        "error_advice": eng.format_source_error_advice(
                            s["last_error"], s["source_kind"]
                        )
                        if s["last_error"] and hasattr(eng, "format_source_error_advice")
                        else "",
                        "last_poll_at": s["last_poll_at"],
                        "cooldown_until": s["cooldown_until"],
                        "config": s["config"],
                    }
                    for s in sources
                ],
                "books": books,
            }

        return await self._serve(op)

    async def sources_post(self):
        async def op(body: dict[str, Any], _actor: str):
            eng = self.plugin.engine
            action = str(body.get("op") or body.get("action") or "").strip()
            identity = body.get("identity") or ""

            if action in {"create", "add_public_book"}:
                active_id, _ = self._resolve_active_identity(
                    str(body.get("identity") or "")
                )
                kind = str(
                    body.get("source_kind")
                    or ("ebook" if action == "add_public_book" else "")
                ).strip()
                if kind not in SOURCE_KINDS:
                    raise ValueError(f"不支持的信息源类型: {kind}")
                raw_cfg = (
                    body.get("config") if isinstance(body.get("config"), dict) else {}
                )
                if action == "add_public_book" and not raw_cfg:
                    raw_cfg = {
                        k: body[k]
                        for k in (
                            "provider",
                            "title",
                            "creator",
                            "page",
                            "book_id",
                            "id",
                            "url",
                            "download_url",
                        )
                        if k in body
                    }
                validator = (
                    validate_source_config
                    if callable(validate_source_config)
                    else eng.validate_source_config
                )
                validated_cfg = await run_io(validator, kind, raw_cfg)
                name = str(
                    body.get("name") or validated_cfg.get("title") or f"{kind} 订阅源"
                ).strip()[:120]
                enabled = bool(body.get("enabled", True))
                created = await run_io(
                    eng.store.create_source,
                    identity=active_id,
                    source_kind=kind,
                    name=name,
                    config=validated_cfg,
                    enabled=enabled,
                    now=eng.clock(),
                )
                book_record = None
                if kind == "ebook" and (
                    validated_cfg.get("title")
                    or validated_cfg.get("page")
                    or validated_cfg.get("book_id")
                    or validated_cfg.get("gutenberg_id")
                ):
                    prov = str(
                        validated_cfg.get("provider")
                        or validated_cfg.get("mode")
                        or "ebook"
                    )
                    ext_id = str(
                        validated_cfg.get("book_id")
                        or validated_cfg.get("gutenberg_id")
                        or validated_cfg.get("page")
                        or validated_cfg.get("title")
                        or name
                    )
                    book_record = await run_io(
                        eng.store.ensure_shelf_book,
                        identity=active_id,
                        source_id=created["source_id"],
                        provider=prov,
                        external_id=ext_id,
                        title=str(
                            validated_cfg.get("title")
                            or validated_cfg.get("page")
                            or name
                        ),
                        creator=str(validated_cfg.get("creator") or ""),
                        raw_url=str(validated_cfg.get("url") or ""),
                        now=eng.clock(),
                    )
                    await eng.refresh_projection()
                return {"source": created, "book": book_record}

            if action == "update":
                source_id = clean_text(body.get("source_id"), "source_id", 80)

                def update():
                    with eng.store.lock, eng.store.db:
                        config = body.get("config")
                        current, merged = eng.store.get_merged_config_for_update(
                            source_id,
                            config if isinstance(config, dict) else {},
                            expected_identity=identity,
                        )
                        kind = current["source_kind"]
                        if body.get("source_kind") and body["source_kind"] != kind:
                            raise ValueError("信息源类型已确定，请刷新。")
                        validator = validate_source_config or eng.validate_source_config
                        return eng.store.update_source(
                            source_id,
                            name=str(body["name"])
                            if body.get("name") is not None
                            else None,
                            enabled=bool(body["enabled"])
                            if body.get("enabled") is not None
                            else None,
                            config=validator(kind, merged)
                            if isinstance(config, dict)
                            else None,
                            now=eng.clock(),
                        )

                updated = await run_io(update)
                return {"source": updated}

            if action == "toggle":
                source_id = clean_text(body.get("source_id"), "source_id", 80)
                explicit_enabled = (
                    body.get("enabled")
                    if isinstance(body.get("enabled"), bool)
                    else None
                )
                toggled = await run_io(
                    eng.store.toggle_source,
                    source_id,
                    explicit_enabled,
                    eng.clock(),
                    expected_identity=identity,
                )
                return {"source": toggled}

            if action == "delete":
                source_id = clean_text(body.get("source_id"), "source_id", 80)
                deleted = await run_io(
                    eng.store.delete_source, source_id, expected_identity=identity
                )
                return {"deleted": deleted, "source_id": source_id}

            if action == "poll_now":
                source_id = str(body.get("source_id") or "").strip()
                if source_id:
                    return await eng.poll_one_source(
                        source_id, force=True, expected_identity=identity
                    )
                active_id, _ = self._resolve_active_identity(
                    str(body.get("identity") or "")
                )
                res = await eng.poll_all_due_sources(identity=active_id, force=True)
                return {"polled": res["polled_sources"] > 0, **res}

            if action == "manual_feed":
                active_id, _ = self._resolve_active_identity(
                    str(body.get("identity") or "")
                )
                title = str(body.get("title") or "").strip()[:160]
                creator = str(body.get("creator") or "管理员投喂").strip()[:80]
                text_val = str(body.get("text") or "").strip()
                url_val = str(body.get("url") or "").strip()
                if not text_val and not url_val:
                    raise ValueError("请填写剪报正文或单篇公开文章 URL")

                manual_src = await run_io(
                    eng.store.ensure_manual_source, active_id, eng.clock()
                )
                raw_items: list[dict[str, Any]] = []
                if text_val:
                    raw_items.append(
                        {
                            "title": title or text_val[:36],
                            "creator": creator,
                            "summary": text_val[:260],
                            "full_text": text_val[:4000],
                            "raw_url": url_val,
                            "content_ready": True,
                            "published_at": eng.clock(),
                        }
                    )
                else:
                    import inspect

                    from .contract import sanitize_readonly_bilibili_cookie

                    poller = (
                        poll_source
                        if callable(poll_source)
                        else eng.poll_source_adapter
                    )
                    shared_cookie = sanitize_readonly_bilibili_cookie(
                        str(self.plugin.config.get("bilibili_shared_cookie", "") or "")
                    )
                    poll_kwargs: dict[str, Any] = {"timeout": 20.0}
                    if shared_cookie:
                        try:
                            sig = inspect.signature(poller)
                            params = sig.parameters
                            has_varkw = any(
                                p.kind == inspect.Parameter.VAR_KEYWORD
                                for p in params.values()
                            )
                        except (TypeError, ValueError):
                            params = {}
                            has_varkw = False
                        if has_varkw or "shared_cookie" in params:
                            poll_kwargs["shared_cookie"] = shared_cookie
                    poll_res = await poller(
                        {
                            "source_id": manual_src["source_id"],
                            "source_kind": "manual",
                            "identity": active_id,
                            "name": manual_src["name"],
                            "enabled": True,
                            "config": {
                                "url": url_val,
                                "title": title,
                                "creator": creator,
                            },
                        },
                        {},
                        **poll_kwargs,
                    )
                    raw_items = list((poll_res or {}).get("items") or [])
                    if not raw_items:
                        is_bili_clip_url = False
                        try:
                            from ..adapters.bilibili import is_bilibili_video_url

                            is_bili_clip_url = bool(is_bilibili_video_url(url_val))
                        except Exception:
                            is_bili_clip_url = False
                        if is_bili_clip_url:
                            err_detail = str(
                                (poll_res or {}).get("error")
                                or (poll_res or {}).get("status")
                                or "B站视频解析失败"
                            ).strip()
                            raise ValueError(err_detail)
                        fetcher = (
                            fetch_item_content
                            if callable(fetch_item_content)
                            else eng.fetch_item_content_adapter
                        )
                        fetched = await fetcher(
                            {
                                "source_id": manual_src["source_id"],
                                "source_kind": "manual",
                                "identity": active_id,
                                "config": {"url": url_val},
                            },
                            {"raw_url": url_val, "title": title},
                            timeout=20.0,
                        )
                        body_txt = str((fetched or {}).get("full_text") or "").strip()
                        if not body_txt:
                            raise ValueError("未能从该 URL 提取出有效正文")
                        raw_items.append(
                            {
                                "title": title or body_txt[:36],
                                "creator": creator,
                                "summary": body_txt[:260],
                                "full_text": body_txt[:4000],
                                "raw_url": url_val,
                                "content_ready": True,
                                "published_at": eng.clock(),
                            }
                        )

                inserted = await run_io(
                    eng.store.insert_candidates,
                    raw_items,
                    source_id=manual_src["source_id"],
                    source_kind="manual",
                    identity=active_id,
                    now=eng.clock(),
                )
                return {
                    "accepted": len(inserted) > 0,
                    "added": len(inserted),
                    "candidate": inserted[0] if inserted else None,
                }

            if action == "upload_ebook":
                active_id, _ = self._resolve_active_identity(
                    str(body.get("identity") or "")
                )
                filename = clean_text(body.get("filename"), "filename", 200)
                if not filename.lower().endswith((".txt", ".epub")):
                    raise ValueError("仅支持上传 .epub 或 .txt 电子书")
                b64 = str(body.get("content_base64") or "").strip()
                if not b64:
                    raise ValueError("缺少电子书文件内容 (content_base64)")
                if "," in b64 and b64.split(",", 1)[0].startswith("data:"):
                    b64 = b64.split(",", 1)[1]
                try:
                    raw_bytes = base64.b64decode(b64, validate=False)
                except Exception as exc:
                    raise ValueError("电子书 Base64 解码失败") from exc
                if not raw_bytes or len(raw_bytes) > _MAX_EBOOK_BYTES:
                    raise ValueError("电子书体积为空或超过 10MB 限制")

                parser = (
                    parse_local_ebook
                    if callable(parse_local_ebook)
                    else eng.parse_local_ebook_adapter
                )
                parsed = await run_io(parser, filename, raw_bytes, chunk_chars=3200)
                book_title = str(
                    body.get("title")
                    or parsed.get("title")
                    or filename.rsplit(".", 1)[0]
                ).strip()
                book_creator = str(
                    body.get("creator") or parsed.get("creator") or ""
                ).strip()
                chapters = parsed.get("chapters") if isinstance(parsed, dict) else []
                book = await run_io(
                    eng.store.create_book_with_chapters,
                    identity=active_id,
                    title=book_title,
                    creator=book_creator,
                    chapters=chapters if isinstance(chapters, list) else [],
                    provider="local",
                    now=eng.clock(),
                )
                await eng.refresh_projection()
                return {"book": book, "chapters_count": book["total_chapters"]}

            if action == "search_public_books":
                provider = str(body.get("provider") or "wikisource").strip().lower()
                if provider not in {"wikisource", "gutendex"}:
                    raise ValueError("仅支持检索 wikisource 或 gutendex 公版书库")
                q = clean_text(body.get("query"), "query", 120)
                lang = str(body.get("language") or "zh").strip()[:16] or "zh"
                limit = _coerce_int(body.get("limit"), 10, 1, 25)
                if callable(search_public_ebooks):
                    books = await search_public_ebooks(
                        provider, q, language=lang, limit=limit
                    )
                else:
                    books = await eng.search_public_ebooks_adapter(
                        provider, q, language=lang, limit=limit
                    )
                return {"books": books if isinstance(books, list) else []}

            if action == "test_source":
                kind = str(body.get("source_kind") or "").strip()
                raw_cfg = (
                    body.get("config") if isinstance(body.get("config"), dict) else {}
                )
                return await eng.test_source_connection(kind, raw_cfg)

            if action == "delete_book":
                book_id = clean_text(body.get("book_id"), "book_id", 80)
                deleted = await run_io(
                    eng.store.delete_book,
                    book_id,
                    expected_identity=identity,
                    now=eng.clock(),
                )
                await eng.refresh_projection()
                return deleted

            if action == "update_book_status":
                book_id = clean_text(body.get("book_id"), "book_id", 80)
                raw_status = str(body.get("status") or "").strip()
                if raw_status:
                    if raw_status not in BOOK_STATUSES:
                        raise ValueError(f"无效的书籍状态: {raw_status}")
                    status = raw_status
                else:
                    existing_book = await run_io(eng.store.get_book, book_id)
                    if existing_book is None:
                        raise ValueError("书籍不存在")
                    status = str(existing_book.get("status") or "reading")
                raw_cursor = body.get("chapter_cursor")
                cursor: int | None = None
                if raw_cursor is not None and raw_cursor != "":
                    if isinstance(raw_cursor, bool):
                        raise ValueError("章节游标必须是非负整数")
                    try:
                        cursor = max(0, int(raw_cursor))
                    except (TypeError, ValueError) as exc:
                        raise ValueError("章节游标必须是非负整数") from exc
                updated = await run_io(
                    eng.store.update_book_status,
                    book_id,
                    status,
                    chapter_cursor=cursor,
                    now=eng.clock(),
                    expected_identity=identity,
                )
                await eng.refresh_projection()
                return {"book": updated}

            raise ValueError(f"未知 sources 操作: {action}")

        return await self._serve(op, max_body=_MAX_UPLOAD_BODY, check_csrf=True)

    # ------------------------------------------------------------------
    # GET /logs
    # ------------------------------------------------------------------

    async def logs_get(self):
        async def op(_body: dict[str, Any], _actor: str):
            eng = self.plugin.engine
            req_id = _query_param("identity", "")
            active_identity, _ = self._resolve_active_identity(req_id)
            filter_id = "" if req_id == "all" else active_identity
            page = _coerce_int(_query_param("page", "1"), 1, 1, 10000)
            source_kind = _query_param("source_kind", "")
            query = _query_param("query", "")
            result = await run_io(
                eng.store.list_logs,
                identity=filter_id,
                source_kind=source_kind,
                query=query,
                page=page,
                page_size=20,
            )
            items = [_normalize_media_fields(it) for it in result.get("items") or []]
            return {"identity": active_identity, **result, "items": items}

        return await self._serve(op)

    # ------------------------------------------------------------------
    # GET /reviews & POST /reviews
    # ------------------------------------------------------------------

    async def reviews_get(self):
        async def op(_body: dict[str, Any], _actor: str):
            eng = self.plugin.engine
            req_id = _query_param("identity", "")
            active_identity, _ = self._resolve_active_identity(req_id)
            filter_id = "" if req_id == "all" else active_identity
            state = _query_param("state", "")
            source_kind = _query_param("source_kind", "")
            query = _query_param("query", "")
            page = _coerce_int(_query_param("page", "1"), 1, 1, 10000)
            result = await run_io(
                eng.store.list_proposals,
                identity=filter_id,
                state=state,
                source_kind=source_kind,
                query=query,
                page=page,
                page_size=20,
            )
            blocked = await run_io(eng.store.list_blocked_topics, filter_id)
            corrections = await run_io(eng.store.list_pending_corrections, filter_id)
            items = [_normalize_media_fields(it) for it in result.get("items") or []]
            return {
                "identity": active_identity,
                "items": items,
                "blocked_topics": blocked,
                "corrections": corrections,
                "corrections_count": len(corrections),
                "page": result["page"],
                "page_size": result["page_size"],
                "total": result["total"],
            }

        return await self._serve(op)

    async def reviews_post(self):
        async def op(body: dict[str, Any], actor: str):
            eng = self.plugin.engine
            action = str(body.get("op") or body.get("action") or "").strip()
            identity = body.get("identity") or ""
            revision = body.get("revision")
            if revision is not None and (type(revision) is not int or revision < 0):
                raise ValueError("审查版本无效")

            from .management import operate

            managed = await operate(eng, body, actor)
            if managed is not None:
                return managed

            if action == "approve":
                pid = clean_text(
                    body.get("proposal_id") or body.get("id"), "proposal_id", 80
                )
                reason = str(body.get("reason") or "管理员人工批准").strip()[:300]
                committed = await eng.approve_proposal(
                    pid,
                    reviewed_by=actor,
                    reason=reason,
                    expected_revision=revision,
                    expected_identity=identity,
                )
                return {"proposal": committed["proposal"], "log": committed["log"]}

            if action == "reject":
                pid = clean_text(
                    body.get("proposal_id") or body.get("id"), "proposal_id", 80
                )
                reason = str(body.get("reason") or "管理员人工拒绝").strip()[:300]
                rejected = await run_io(
                    eng.store.record_review_verdict,
                    pid,
                    state="rejected",
                    expected_revision=revision,
                    expected_identity=identity,
                    verdict="reject",
                    reason=reason,
                    risks=[],
                    reviewed_by=actor,
                    now=eng.clock(),
                )
                await eng.refresh_projection()
                return {"proposal": rejected}

            if action == "revoke_interest":
                iid = clean_text(
                    body.get("interest_id") or body.get("id"), "interest_id", 80
                )
                res = await run_io(
                    eng.store.revoke_interest,
                    iid,
                    eng.clock(),
                    expected_identity=identity,
                )
                await eng.refresh_projection()
                return res

            if action == "extend_interest":
                iid = clean_text(
                    body.get("interest_id") or body.get("id"), "interest_id", 80
                )
                days = float(body.get("days") or eng.half_life_days)
                res = await run_io(
                    eng.store.extend_interest,
                    iid,
                    days=days,
                    half_life_days=eng.half_life_days,
                    now=eng.clock(),
                    expected_identity=identity,
                )
                await eng.refresh_projection()
                return res

            if action == "archive_question":
                qid = clean_text(
                    body.get("question_id") or body.get("id"), "question_id", 80
                )
                res = await run_io(
                    eng.store.archive_question,
                    qid,
                    expected_identity=identity,
                )
                await eng.refresh_projection()
                return res

            if action == "block_topic":
                raw_topic = body.get("topic") or body.get("keyword")
                if isinstance(raw_topic, dict):
                    raw_topic = raw_topic.get("topic")
                topic = clean_text(raw_topic, "topic", 80)
                ident = (
                    normalize_identity(body["identity"]) if body.get("identity") else ""
                )
                blocked = await run_io(eng.store.block_topic, topic, ident, eng.clock())
                await eng.refresh_projection()
                return {"blocked_topics": blocked, "topic": topic}

            if action == "unblock_topic":
                raw_topic = body.get("topic") or body.get("keyword") or body.get("id")
                if isinstance(raw_topic, dict):
                    raw_topic = raw_topic.get("topic")
                topic = clean_text(raw_topic, "topic", 80)
                ident = (
                    normalize_identity(body["identity"]) if body.get("identity") else ""
                )
                blocked = await run_io(eng.store.unblock_topic, topic, ident)
                await eng.refresh_projection()
                return {"blocked_topics": blocked, "topic": topic}

            if action == "edit_proposal":
                pid = clean_text(
                    body.get("proposal_id") or body.get("id"), "proposal_id", 80
                )
                edited = await run_io(
                    eng.store.edit_proposal,
                    pid,
                    digest=body.get("digest") if "digest" in body else None,
                    reflection=body.get("reflection") if "reflection" in body else None,
                    staged_interest=body.get("staged_interest")
                    if "staged_interest" in body
                    else None,
                    questions=body.get("questions") if "questions" in body else None,
                    actor=actor,
                    expected_revision=revision,
                    expected_identity=identity,
                    now=eng.clock(),
                )
                await eng.refresh_projection()
                return {"proposal": edited}

            if action in {"batch_approve", "batch_reject"}:
                raw_items = body.get("items")
                targets: list[tuple[str, int | None]] = []
                if isinstance(raw_items, list):
                    for entry in raw_items[:50]:
                        if isinstance(entry, dict) and entry.get("proposal_id"):
                            ev_rev = entry.get("revision")
                            targets.append(
                                (
                                    str(entry["proposal_id"]).strip()[:80],
                                    ev_rev if type(ev_rev) is int and ev_rev >= 0 else None,
                                )
                            )
                        elif isinstance(entry, str) and entry.strip():
                            targets.append((entry.strip()[:80], None))
                elif isinstance(body.get("proposal_ids"), list):
                    for pid_val in body["proposal_ids"][:50]:
                        if isinstance(pid_val, str) and pid_val.strip():
                            targets.append((pid_val.strip()[:80], None))
                if not targets:
                    raise ValueError("请先勾选要批量处理的提案")
                reason = str(
                    body.get("reason")
                    or ("管理员批量批准" if action == "batch_approve" else "管理员批量拒绝")
                ).strip()[:300]
                succeeded: list[str] = []
                errors: list[dict[str, str]] = []
                for pid_val, rev_val in targets:
                    try:
                        if action == "batch_approve":
                            await eng.approve_proposal(
                                pid_val,
                                reviewed_by=actor,
                                reason=reason,
                                expected_revision=rev_val,
                                expected_identity=identity,
                            )
                        else:
                            await run_io(
                                eng.store.record_review_verdict,
                                pid_val,
                                state="rejected",
                                expected_revision=rev_val,
                                expected_identity=identity,
                                verdict="reject",
                                reason=reason,
                                risks=[],
                                reviewed_by=actor,
                                now=eng.clock(),
                            )
                        succeeded.append(pid_val)
                    except Exception as exc:
                        errors.append({"proposal_id": pid_val, "error": str(exc)[:160]})
                await eng.refresh_projection()
                return {
                    "processed": len(targets),
                    "succeeded": len(succeeded),
                    "failed": len(errors),
                    "succeeded_ids": succeeded,
                    "errors": errors,
                }

            if action == "cancel_intent":
                iid = clean_text(body.get("intent_id"), "intent_id", 120)
                res = await run_io(
                    eng.store.update_intent,
                    iid,
                    "cancelled",
                    now=eng.clock(),
                )
                return res

            if action == "restore_member_outreach":
                target_id = clean_text(
                    body.get("identity") or "", "identity", 120
                )
                target_umo = clean_text(body.get("umo"), "umo", 240)
                target_member = clean_text(body.get("member_id"), "member_id", 128)
                target_topic = str(body.get("topic") or "").strip()[:80]
                res = await run_io(
                    eng.store.restore_member_outreach,
                    target_id,
                    target_umo,
                    target_member,
                    topic=target_topic,
                    now=eng.clock(),
                )
                await eng.refresh_projection()
                return res

            if action == "apply_preset":
                preset_id = clean_text(body.get("preset_id"), "preset_id", 32)
                applied = eng.apply_preset(preset_id)
                await eng.refresh_projection()
                return applied

            if action == "trigger_digest":
                ident = str(body.get("identity") or "").strip()
                norm_id = normalize_identity(ident) if ident and ident != "all" else ""
                digest_res = await eng.run_digest_cycle(identity=norm_id)
                return {"triggered": True, **digest_res}

            raise ValueError(f"未知 reviews 操作: {action}")

        return await self._serve(op, max_body=_MAX_STD_BODY, check_csrf=True)
