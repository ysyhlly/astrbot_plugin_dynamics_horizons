# ruff: noqa: E402 -- owned module refresh must precede imports on hot reload.
from __future__ import annotations

from .reload_bootstrap import ensure_current

ensure_current(
    __package__,
    __name__,
    __file__,
    checks=[
        ("core.engine", "HorizonsEngine", "_horizons_contract_v1"),
        ("core.engine", "HorizonsEngine", "_video_vision_v1"),
        ("core.runtime", "EngineLifecycle", "proactive_scope"),
        ("core.world_bridge", "WorldBridge", "_group_wake_scope_v2"),
    ],
    version="0.2.0-beta.2.reliability.1.expression.1.allchats.1.bili.3.vision.1",
)

import asyncio
import base64
import inspect
import os
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, StarTools

from .core.contract import (
    PLUGIN_ID,
    PLUGIN_VERSION,
    SCHEMA_VERSION,
    decode_model_object,
    encode_json,
)
from .core.engine import HorizonsEngine
from .core.io import run_io
from .core.projection import _empty_result
from .core.store import HorizonsStore
from .core.web_api import HorizonsWebAPI
from .series_panels import get_series_defaults, inherit_provider


class _BindingChangedError(ValueError):
    """Raised when provider binding changes mid-call; never swallowed by media fallback."""


def _detect_video_mime(data: bytes) -> str:
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand == b"qt  ":
            return "video/mov"
        if brand.startswith(b"3g"):
            return "video/3gpp"
        return "video/mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "video/avi"
    if data.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return "video/mpeg"
    if data.startswith(b"FLV"):
        return "video/x-flv"
    raise ValueError("unsupported_video_format")


def _read_video_bytes(path_str: str, max_bytes: int) -> tuple[bytes, str]:
    path = Path(path_str)
    if not path.is_file():
        raise FileNotFoundError("video_file_missing")
    size = path.stat().st_size
    if size <= 0 or size > max_bytes:
        raise ValueError(f"video_size_exceeds_limit:{size}>{max_bytes}")
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes or not data:
        raise ValueError("video_size_exceeds_limit")
    return data, _detect_video_mime(data)


def _resolve_genai_generate_fn(provider: Any):
    client = getattr(provider, "client", None)
    if client is None:
        return None
    aio = getattr(client, "aio", None)
    if aio is not None:
        models = getattr(aio, "models", None)
        fn = getattr(models, "generate_content", None)
        if callable(fn):
            return fn
    models = getattr(client, "models", None)
    fn = getattr(models, "generate_content", None)
    if callable(fn):
        return fn
    return None


def is_native_video_provider(provider: Any, provider_id: str = "") -> bool:
    """Return True if provider supports Gemini native inline video/mp4."""
    if provider is None:
        return False
    cls_name = type(provider).__name__.lower()
    mod_name = type(provider).__module__.lower()
    prov_cfg = getattr(provider, "provider_config", None)
    cfg_type = (
        str(prov_cfg.get("type") or "").strip().lower()
        if isinstance(prov_cfg, dict)
        else ""
    )
    if (
        cfg_type == "googlegenai_chat_completion"
        or "googlegenai" in cls_name
        or "gemini_source" in mod_name
        or _resolve_genai_generate_fn(provider) is not None
    ):
        return True
    model_name = ""
    get_model = getattr(provider, "get_model", None)
    if callable(get_model):
        try:
            model_name = str(get_model() or "").strip().lower()
        except Exception:
            model_name = ""
    if not model_name:
        model_name = str(getattr(provider, "model_name", "") or "").strip().lower()
    if model_name.startswith(("gemini-", "models/gemini-")) or "/gemini-" in model_name:
        return True
    pid_lower = str(provider_id or "").strip().lower()
    if pid_lower.startswith(("gemini/", "gemini_chat/")):
        return True
    return False


def _extract_genai_text(result: Any) -> str:
    candidates = getattr(result, "candidates", None) or []
    if candidates:
        cand = candidates[0]
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        texts = [
            str(part.text)
            for part in parts
            if getattr(part, "text", None) and not getattr(part, "thought", False)
        ]
        if texts:
            return "".join(texts)
    text = getattr(result, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    completion_text = getattr(result, "completion_text", None)
    if isinstance(completion_text, str) and completion_text.strip():
        return completion_text
    raise ValueError("empty_native_video_response")


async def _invoke_genai_native_video(
    provider: Any,
    *,
    instruction: str,
    payload: str,
    video_bytes: bytes,
    mime_type: str,
) -> str:
    from google.genai import types

    generate_fn = _resolve_genai_generate_fn(provider)
    if generate_fn is None:
        raise ValueError("genai_client_unavailable")
    get_model = getattr(provider, "get_model", None)
    model = (
        str(get_model() or "")
        if callable(get_model)
        else str(getattr(provider, "model_name", "") or "")
    )
    video_part = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
    text_part = types.Part.from_text(text=payload)
    contents = [types.UserContent(parts=[video_part, text_part])]

    prepare_cfg = getattr(provider, "_prepare_query_config", None)
    if callable(prepare_cfg):
        config = await prepare_cfg(
            {"messages": [], "model": model},
            None,
            "auto",
            instruction,
            ["TEXT"],
        )
    else:
        config = types.GenerateContentConfig(
            system_instruction=instruction,
            safety_settings=getattr(provider, "safety_settings", None) or None,
        )

    res = generate_fn(model=model, contents=contents, config=config)
    if inspect.isawaitable(res):
        res = await res
    return _extract_genai_text(res)


async def _invoke_openai_gemini_native_video(
    provider: Any,
    *,
    instruction: str,
    payload: str,
    video_bytes: bytes,
    mime_type: str,
) -> str:
    get_model = getattr(provider, "get_model", None)
    model = (
        str(get_model() or "")
        if callable(get_model)
        else str(getattr(provider, "model_name", "") or "")
    )
    b64 = base64.b64encode(video_bytes).decode("ascii")
    messages = [
        {"role": "system", "content": instruction},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": payload},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{b64}"},
                },
            ],
        },
    ]
    query_fn = getattr(provider, "_query", None)
    if callable(query_fn):
        resp = await query_fn({"messages": messages, "model": model}, None)
        text = getattr(resp, "completion_text", None)
        if isinstance(text, str) and text.strip():
            return text
        raise ValueError("empty_openai_gemini_video_response")
    client = getattr(provider, "client", None)
    chat = getattr(client, "chat", None)
    completions = getattr(chat, "completions", None)
    create_fn = getattr(completions, "create", None)
    if callable(create_fn):
        completion = create_fn(model=model, messages=messages, stream=False)
        if inspect.isawaitable(completion):
            completion = await completion
        choices = getattr(completion, "choices", None) or []
        if choices:
            msg = getattr(choices[0], "message", None)
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                return content
        raise ValueError("empty_openai_gemini_video_response")
    raise ValueError("openai_gemini_client_unavailable")


def _filter_image_urls(urls: Any) -> list[str]:
    if not isinstance(urls, (list, tuple)):
        return []
    out: list[str] = []
    for item in urls:
        if not isinstance(item, str):
            continue
        val = item.strip()
        if not val:
            continue
        if val.startswith("data:image/") or os.path.isfile(val):
            out.append(val)
        if len(out) >= 16:
            break
    return out


class Main(Star):
    """Dynamics Horizons (群间见闻): isolated external reading, dual-LLM review, zero-I/O chat projection."""

    def __init__(
        self, context: Context, config: AstrBotConfig | dict[str, Any] | None = None
    ):
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        self.engine: HorizonsEngine | None = None
        self.web_api = HorizonsWebAPI(self)
        self.tasks: list[asyncio.Task[Any]] = []
        self.ready = False
        self.closed = False

    def _resolve_data_dir(self) -> Path:
        override = getattr(self, "_data_dir_override", None)
        if override:
            return Path(override)
        try:
            return Path(StarTools.get_data_dir(PLUGIN_ID))
        except Exception:
            try:
                return Path(StarTools.get_data_dir())
            except Exception:
                return Path(__file__).resolve().parent / "data"

    async def initialize(self) -> None:
        from .series_panels import register_series_panels

        register_series_panels(self, PLUGIN_ID)
        data_dir = self._resolve_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "horizons.sqlite3"
        store = await run_io(HorizonsStore, db_path)
        self.engine = HorizonsEngine(
            store, self.config, self._model, context=self.context
        )
        await self.engine.refresh_projection()
        self.web_api.register()
        self.tasks = [
            asyncio.create_task(self.engine.poll_loop()),
            asyncio.create_task(self.engine.digest_loop()),
            asyncio.create_task(self.engine.maintenance_loop()),
        ]
        self.ready = True
        logger.info("[DynamicsHorizons] Ready; db=%s", db_path)

    async def terminate(self) -> None:
        panel = getattr(self, "_series_panels", None)
        if panel is not None:
            panel.close()
        self.web_api.unregister()
        self.ready = False
        self.closed = True
        if self.engine is not None:
            self.engine.closed = True
        for t in self.tasks:
            t.cancel()
        await self.web_api.drain()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if self.engine is not None:
            async with self.engine.lock:
                await run_io(self.engine.store.close)

    # ------------------------------------------------------------------
    # Companion Discovery & Capability Contracts
    # ------------------------------------------------------------------

    def get_dynamics_capabilities(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", True))
        is_ready = bool(self.ready and not self.closed and self.engine is not None)
        summary = self.engine.projection.summary() if self.engine is not None else {}
        return {
            "schema_version": SCHEMA_VERSION,
            "version": PLUGIN_VERSION,
            "provider_id": PLUGIN_ID,
            "display_name": "群间见闻",
            "enabled": enabled,
            "ready": is_ready,
            "capabilities": {"horizons": 1},
            "summary": {
                "sources": int(summary.get("sources", 0)),
                "books_reading": int(summary.get("books_reading", 0)),
                "active_interests": int(summary.get("active_interests", 0)),
                "pending_questions": int(summary.get("pending_questions", 0)),
                "pending_reviews": int(summary.get("pending_reviews", 0)),
                "recent_title": str(summary.get("recent_title", "")),
            },
        }

    def horizons_capabilities(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", True))
        is_ready = bool(self.ready and not self.closed and self.engine is not None)
        return {
            "schema_version": SCHEMA_VERSION,
            "version": PLUGIN_VERSION,
            "enabled": enabled,
            "ready": is_ready,
            "proactive_enabled": bool(
                enabled and self.engine and self.engine.proactive_scope != "disabled"
            ),
            "capabilities": [
                "decision_context_v1",
                "reply_projection_v1",
                "turn_feedback_v1",
                "proactive_candidates_v1",
                "world_sync_v1",
                "verified_feedback_v2",
                "discussion_reflections_v1",
                "outreach_all_managed_v1",
                "review_evidence_v2",
                "video_vision_v1",
            ],
        }

    # ------------------------------------------------------------------
    # Zero-I/O Synchronous Context Projection (Chat Dynamics Reply Path)
    # ------------------------------------------------------------------

    def get_horizons_context(
        self,
        scope: str | dict[str, Any] = "",
        persona: str = "",
        member: str = "",
        query: str = "",
        nearby_text: str = "",
        character_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pure in-memory lookup; performs ZERO SQLite, disk, network, or LLM I/O."""
        if self.closed or not self.ready or self.engine is None:
            return _empty_result("not_ready")
        return self.engine.projection.query(
            enabled=bool(self.config.get("enabled", True)),
            ready=bool(self.ready and not self.closed),
            allowed_sessions=self.engine.sessions,
            scope=scope,
            persona=persona,
            member=member,
            query=query,
            nearby_text=nearby_text,
            character_scope=character_scope,
            now=self.engine.clock(),
        )

    # ------------------------------------------------------------------
    # Turn Feedback & Per-UMO Question Consumption
    # ------------------------------------------------------------------

    def ingest_dynamics_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if (
            self.closed
            or not self.ready
            or self.engine is None
            or not self.config.get("enabled", True)
        ):
            return {"schema_version": SCHEMA_VERSION, "accepted": False}
        if not isinstance(event, dict):
            return {"schema_version": SCHEMA_VERSION, "accepted": False}

        from .core.feedback import enqueue

        result = enqueue(self.engine, event)
        if result.pop("projection_dirty", False):
            self.engine.refresh_projection_sync()
        return {"schema_version": SCHEMA_VERSION, **result}

    # ------------------------------------------------------------------
    # Proactive Outreach Candidate Gate (Consumed by Chat Dynamics)
    # ------------------------------------------------------------------

    def horizons_outreach_allowed(self, umo: str) -> bool:
        return bool(
            not self.closed
            and self.ready
            and self.engine
            and self.engine.outreach_allowed(str(umo or ""))
        )

    def list_due_intents(self, now: float | None = None) -> list[dict[str, Any]]:
        if (
            self.closed
            or not self.ready
            or self.engine is None
            or not self.config.get("enabled", True)
        ):
            return []
        stamp = float(now) if isinstance(now, (int, float)) else self.engine.clock()
        return self.engine.store.list_due_intents(
            stamp,
            proactive_sessions=self.engine.effective_proactive_sessions(),
            quiet_hours=self.engine.quiet_hours,
        )

    def get_intent(self, intent_id: str) -> dict[str, Any]:
        if self.closed or not self.ready or self.engine is None:
            return {"schema_version": SCHEMA_VERSION, "available": False, "item": None}
        return self.engine.store.get_intent(
            str(intent_id or ""),
            now=self.engine.clock(),
            proactive_sessions=self.engine.effective_proactive_sessions(),
        )

    def update_intent(
        self,
        intent_id: str,
        status: str,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.closed or not self.ready or self.engine is None:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "unavailable",
                "changed": False,
            }
        return self.engine.store.update_intent(
            str(intent_id or ""),
            str(status or ""),
            receipt=receipt,
            now=self.engine.clock(),
            proactive_sessions=self.engine.effective_proactive_sessions(),
        )

    def cancel_intent(self, intent_id: str) -> dict[str, Any]:
        return self.update_intent(intent_id, "cancelled")

    # ------------------------------------------------------------------
    # Dual-LLM Provider Invocation
    # ------------------------------------------------------------------

    def _effective_provider(self, role: str) -> str:
        if self.engine is not None:
            return self.engine.effective_provider(role)
        explicit = str(self.config.get(role, "") or "").strip()
        if explicit:
            return explicit
        if role == "video_provider":
            return self._effective_provider("analysis_provider")
        if role == "analysis_provider":
            return inherit_provider("", self.context, role="reply")
        if role == "review_provider":
            defaults = get_series_defaults(self.context)
            decision_p = str(defaults.get("decision_provider") or "").strip()
            allow_same = bool(self.config.get("allow_same_provider_for_review", False))
            if decision_p and (
                allow_same or decision_p != self._effective_provider("analysis_provider")
            ):
                return decision_p
            if allow_same:
                return inherit_provider("", self.context, role="reply")
        return ""

    def _assert_binding_current(self, role: str, provider_id: str, provider: Any) -> None:
        if (
            self.closed
            or not self.config.get("enabled", True)
            or self.context.get_provider_by_id(provider_id) is not provider
            or self._effective_provider(role) != provider_id
        ):
            raise _BindingChangedError("模型绑定已改变，本次结果作废")

    async def _model(
        self,
        role: str,
        instruction: str,
        data: dict[str, Any],
        *,
        media_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        provider_id = self._effective_provider(role)
        if not provider_id:
            if role == "video_provider":
                label = "视频多模态精读模型"
            elif role == "analysis_provider":
                label = "阅读与分析模型"
            else:
                label = "独立审查模型"
            raise ValueError(f"请先配置{label} ({role})")
        provider = self.context.get_provider_by_id(provider_id)
        if (
            provider is None
            or not callable(getattr(provider, "text_chat", None))
            or getattr(provider, "is_native_decision_provider", False)
            or getattr(provider, "is_systemone_provider", False)
        ):
            raise ValueError("所选模型不可用或不是普通聊天模型")
        base_timeout = max(5, min(180, int(self.config.get("model_timeout", 60) or 60)))
        payload = encode_json(data)
        if len(payload) > 140_000:
            raise ValueError("阅读材料超出单次模型输入预算")

        media = media_payload if isinstance(media_payload, dict) else None
        native_video_path = (
            str(media.get("native_video_path") or "").strip() if media else ""
        )
        image_urls = (
            _filter_image_urls(
                media.get("image_urls")
                or media.get("frame_paths")
                or media.get("image_paths")
                or []
            )
            if media
            else []
        )
        has_media = bool(native_video_path or image_urls)
        effective_timeout = (
            min(240, max(base_timeout, 120)) if has_media else base_timeout
        )

        # Channel A: Native inline video/mp4 (Gemini GenAI or OpenAI-compatible Gemini proxy)
        if native_video_path and os.path.isfile(native_video_path):
            if is_native_video_provider(provider, provider_id):
                max_mb = max(
                    1, min(50, int(self.config.get("video_native_max_mb", 15) or 15))
                )
                try:
                    video_bytes, mime_type = await asyncio.to_thread(
                        _read_video_bytes, native_video_path, max_mb * 1024 * 1024
                    )
                    async with asyncio.timeout(effective_timeout):
                        if _resolve_genai_generate_fn(provider) is not None:
                            content = await _invoke_genai_native_video(
                                provider,
                                instruction=instruction,
                                payload=payload,
                                video_bytes=video_bytes,
                                mime_type=mime_type,
                            )
                        else:
                            content = await _invoke_openai_gemini_native_video(
                                provider,
                                instruction=instruction,
                                payload=payload,
                                video_bytes=video_bytes,
                                mime_type=mime_type,
                            )
                    self._assert_binding_current(role, provider_id, provider)
                    return decode_model_object(content)
                except asyncio.CancelledError:
                    raise
                except _BindingChangedError as exc:
                    raise ValueError(str(exc)) from None
                except Exception as exc:
                    logger.warning(
                        "[DynamicsHorizons] Native video channel failed (%s); attempting keyframe fallback",
                        type(exc).__name__,
                    )
                    fallback_urls = list(image_urls)
                    fallback_labels: list[str] = []
                    ensure_fn = (
                        media.get("ensure_fallback_frames") if media else None
                    )
                    if not fallback_urls and callable(ensure_fn):
                        try:
                            extracted = await ensure_fn()
                            if isinstance(extracted, list):
                                fallback_urls = _filter_image_urls(
                                    [
                                        f.get("path", "")
                                        if isinstance(f, dict)
                                        else str(f or "")
                                        for f in extracted
                                    ]
                                )
                                fallback_labels = [
                                    str(f.get("label") or "")
                                    for f in extracted
                                    if isinstance(f, dict) and f.get("label")
                                ]
                        except Exception as fb_exc:
                            logger.warning(
                                "[DynamicsHorizons] Keyframe fallback extraction failed (%s)",
                                type(fb_exc).__name__,
                            )
                    if fallback_urls:
                        if media is not None:
                            media["fallback_used"] = True
                            media["effective_frame_count"] = len(fallback_urls)
                        image_urls = fallback_urls
                        if fallback_labels and isinstance(data, dict):
                            fb_data = dict(data)
                            meta = dict(fb_data.get("metadata") or {})
                            meta["watch_mode"] = (
                                "keyframes_subtitle"
                                if media
                                and (
                                    media.get("has_subtitle")
                                    or media.get("has_audio_transcript")
                                )
                                else "keyframes_only"
                            )
                            meta["frame_timestamps"] = fallback_labels[:16]
                            fb_data["metadata"] = meta
                            payload = encode_json(fb_data)
                    elif media and (
                        media.get("has_subtitle") or media.get("has_audio_transcript")
                    ):
                        media["fallback_used"] = True
                        media["effective_frame_count"] = 0
                        media["visual_dropped"] = True
                    else:
                        raise
            else:
                # Non-Gemini provider cannot consume native video; ensure keyframes or enforce degradation guard
                fallback_urls = list(image_urls)
                fallback_labels = []
                ensure_fn = media.get("ensure_fallback_frames") if media else None
                if not fallback_urls and callable(ensure_fn):
                    try:
                        extracted = await ensure_fn()
                        if isinstance(extracted, list):
                            fallback_urls = _filter_image_urls(
                                [
                                    f.get("path", "")
                                    if isinstance(f, dict)
                                    else str(f or "")
                                    for f in extracted
                                ]
                            )
                            fallback_labels = [
                                str(f.get("label") or "")
                                for f in extracted
                                if isinstance(f, dict) and f.get("label")
                            ]
                    except Exception as fb_exc:
                        logger.warning(
                            "[DynamicsHorizons] Non-native keyframe fallback extraction failed (%s)",
                            type(fb_exc).__name__,
                        )
                if fallback_urls:
                    image_urls = fallback_urls
                    if media is not None:
                        media["fallback_used"] = True
                        media["effective_frame_count"] = len(fallback_urls)
                    if fallback_labels and isinstance(data, dict):
                        fb_data = dict(data)
                        meta = dict(fb_data.get("metadata") or {})
                        meta["watch_mode"] = (
                            "keyframes_subtitle"
                            if media
                            and (
                                media.get("has_subtitle")
                                or media.get("has_audio_transcript")
                            )
                            else "keyframes_only"
                        )
                        meta["frame_timestamps"] = fallback_labels[:16]
                        fb_data["metadata"] = meta
                        payload = encode_json(fb_data)
                elif media and (
                    media.get("has_subtitle") or media.get("has_audio_transcript")
                ):
                    media["fallback_used"] = True
                    media["effective_frame_count"] = 0
                    media["visual_dropped"] = True
                else:
                    raise ValueError(
                        "native_video_unsupported_and_no_fallback_frames"
                    )

        # Channel B: Universal Keyframes VLM via image_urls (or standard text chat)
        if image_urls:
            async with asyncio.timeout(effective_timeout):
                response = await provider.text_chat(
                    prompt=payload,
                    system_prompt=instruction,
                    image_urls=image_urls,
                    contexts=[],
                )
        else:
            async with asyncio.timeout(base_timeout):
                response = await provider.text_chat(
                    prompt=payload, system_prompt=instruction, contexts=[]
                )
        try:
            self._assert_binding_current(role, provider_id, provider)
        except _BindingChangedError as exc:
            raise ValueError(str(exc)) from None
        content = getattr(response, "completion_text", None)
        return decode_model_object(content)
