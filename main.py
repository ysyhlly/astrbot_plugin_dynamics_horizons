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
    version="0.2.0-rc.9",
)

import asyncio
import base64
import hashlib
from importlib import import_module, reload
import inspect
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, StarTools

from .adapters.video_media import cleanup_stale_temp_dirs
from .core.contract import (
    PLUGIN_ID,
    PLUGIN_VERSION,
    SCHEMA_VERSION,
    decode_model_object,
    encode_json,
    make_identity,
    normalize_identity,
    shanghai_day_bounds,
    unpack_identity,
)
from .core.engine import HorizonsEngine
from .core.io import run_io
from .core.projection import _empty_result
from .core.store import HorizonsStore
from .core.web_api import HorizonsWebAPI
from . import series_panels as _series_panels_mod
from .series_panels import (
    find_provider_silently,
    get_series_defaults,
    is_provider_budget_busy_error,
    is_series_group_excluded,
    is_series_in_quiet_hours,
    resolve_series_provider,
    resolve_series_timezone,
    series_llm_call,
)


def _file_sha256(path_str: Any) -> str:
    if not path_str:
        return ""
    try:
        return hashlib.sha256(Path(str(path_str)).read_bytes()).hexdigest()
    except OSError:
        return ""


_SERIES_PANELS_INITIAL_HASH = _file_sha256(getattr(_series_panels_mod, "__file__", None))
if _SERIES_PANELS_INITIAL_HASH:
    _series_panels_mod._horizons_loaded_source_hash = _SERIES_PANELS_INITIAL_HASH


def _ensure_fresh_series_panels():
    global find_provider_silently, get_series_defaults, is_provider_budget_busy_error
    global is_series_group_excluded, is_series_in_quiet_hours, resolve_series_provider
    global resolve_series_timezone, series_llm_call, _series_panels_mod

    sp_name = f"{__package__}.series_panels" if __package__ else "series_panels"
    sp_mod = import_module(sp_name)
    disk_hash = _file_sha256(getattr(sp_mod, "__file__", None))
    loaded_hash = getattr(sp_mod, "_horizons_loaded_source_hash", None)
    if disk_hash and loaded_hash != disk_hash:
        sp_mod = reload(sp_mod)
        sp_mod._horizons_loaded_source_hash = disk_hash
        _series_panels_mod = sp_mod
        find_provider_silently = getattr(sp_mod, "find_provider_silently", find_provider_silently)
        get_series_defaults = getattr(sp_mod, "get_series_defaults", get_series_defaults)
        is_provider_budget_busy_error = getattr(
            sp_mod, "is_provider_budget_busy_error", is_provider_budget_busy_error
        )
        is_series_group_excluded = getattr(
            sp_mod, "is_series_group_excluded", is_series_group_excluded
        )
        is_series_in_quiet_hours = getattr(
            sp_mod, "is_series_in_quiet_hours", is_series_in_quiet_hours
        )
        resolve_series_provider = getattr(
            sp_mod, "resolve_series_provider", resolve_series_provider
        )
        resolve_series_timezone = getattr(
            sp_mod, "resolve_series_timezone", resolve_series_timezone
        )
        series_llm_call = getattr(sp_mod, "series_llm_call", series_llm_call)
    return sp_mod

_OPENAI_INLINE_VIDEO_MAX_BYTES = 8 * 1024 * 1024  # 8 MB cap for base64 inline proxy payloads


def _encode_base64_ascii(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


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
    if len(video_bytes) > _OPENAI_INLINE_VIDEO_MAX_BYTES:
        raise ValueError(
            f"openai_inline_video_exceeds_limit:{len(video_bytes)}>{_OPENAI_INLINE_VIDEO_MAX_BYTES}"
        )
    get_model = getattr(provider, "get_model", None)
    model = (
        str(get_model() or "")
        if callable(get_model)
        else str(getattr(provider, "model_name", "") or "")
    )
    b64 = await asyncio.to_thread(_encode_base64_ascii, video_bytes)
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


def extract_persona_system_prompt(
    instruction: str | None,
    data: Any,
) -> tuple[str, Any]:
    """Promote stable ``character.core_profile`` from dynamic user JSON into ``system_prompt``.

    Returns ``(effective_instruction, cleaned_data)`` without mutating caller inputs.
    """
    base = str(instruction or "")
    if not isinstance(data, dict):
        return base, data
    char = data.get("character")
    if not isinstance(char, dict) or "core_profile" not in char:
        return base, data
    raw_profile = str(char.get("core_profile") or "").strip()[:1500]
    cleaned_char = {k: v for k, v in char.items() if k != "core_profile"}
    cleaned_data = {**data, "character": cleaned_char}
    if not raw_profile or raw_profile in base:
        return base, cleaned_data
    name = str(char.get("name") or "").strip()
    header = (
        f"【角色固定核心设定】\n角色名：{name}\n"
        if name
        else "【角色固定核心设定】\n"
    )
    return f"{header}{raw_profile}\n\n【任务说明】\n{base}", cleaned_data


def build_cache_anchor_contexts(
    instruction: str | None,
    *,
    task: str = "dynamics_horizons",
) -> list[dict[str, str]]:
    """Build a tiny deterministic 2-message static anchor keyed only on system_prompt hash."""
    text = str(instruction or "")
    if not text.strip():
        return []
    sha12 = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return [
        {"role": "user", "content": f"[cache_anchor task={task} prefix={sha12}]"},
        {"role": "assistant", "content": "OK"},
    ]


def _accepts_kwarg(fn: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(fn).parameters
    except (ValueError, TypeError):
        return False
    return name in parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    )


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
        _ensure_fresh_series_panels().register_series_panels(self, PLUGIN_ID)
        await run_io(cleanup_stale_temp_dirs)
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
            flush_fn = getattr(self.engine, "flush_pending_ingests", None)
            if callable(flush_fn):
                await flush_fn()
            refresh_task = getattr(self.engine, "_projection_refresh_task", None)
            if refresh_task is not None and not refresh_task.done():
                refresh_task.cancel()
                await asyncio.gather(refresh_task, return_exceptions=True)
            self.engine.clear_media_checkpoints()
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
    # Series v2.0 Cross-Plugin Read-Only Hooks (Sync, Standalone-Safe)
    # ------------------------------------------------------------------

    def _resolve_scope_identity_readonly(
        self, *, persona: str = "", scope: str = ""
    ) -> str:
        eng = self.engine
        if eng is None:
            return ""
        clean_persona = str(persona or "").strip()
        if clean_persona:
            resolved = eng.projection.resolve_identity(persona=clean_persona)
            return resolved or normalize_identity(clean_persona)
        clean_scope = str(scope or "").strip()
        if clean_scope:
            binding = eng.store.get_scope_binding(clean_scope)
            if binding and binding.get("identity"):
                return str(binding["identity"])
            resolved = eng.projection.resolve_identity(umo=clean_scope)
            if resolved:
                return resolved
        return ""

    def _resolve_query_identity_readonly(
        self, *, persona: str = "", scope: str = ""
    ) -> tuple[str, bool]:
        """Distinguish explicit global queries from unresolved per-scope persona lookups.

        Returns ``(identity, allow_all)``. Only when both ``persona`` and ``scope``
        are empty is ``allow_all`` True. When a specific scope is requested but has no
        bound persona yet, returns ``("", False)`` so callers and the store never widen
        the query to other personas.
        """
        clean_persona = str(persona or "").strip()
        clean_scope = str(scope or "").strip()
        if not clean_persona and not clean_scope:
            return "", True
        identity = self._resolve_scope_identity_readonly(
            persona=clean_persona, scope=clean_scope
        )
        return identity, False

    def get_series_summary(self) -> dict[str, Any]:
        """Synchronous, read-only badge/pulse summary for series_presence v2."""
        try:
            if self.closed or not self.ready or self.engine is None:
                return {
                    "badge": {"count": 0, "tone": "muted", "label": "未就绪"},
                    "pulse": {"headline": "群间见闻尚未就绪", "updated_at": ""},
                }
            if not bool(self.config.get("enabled", True)):
                return {
                    "badge": {"count": 0, "tone": "muted", "label": "已停用"},
                    "pulse": {"headline": "群间见闻已停用", "updated_at": ""},
                }
            eng = self.engine
            now = eng.clock()
            summary = eng.projection.summary()
            if not summary:
                summary = eng.store.summary_counts(
                    allow_all=True, now=now, half_life_days=eng.half_life_days
                )
            today_logs, latest_log_ts = eng.store.count_today_logs_and_latest(
                now=now, allow_all=True, tz=eng.timezone
            )
            pending_reviews = int(summary.get("pending_reviews", 0) or 0)
            active_interests = int(summary.get("active_interests", 0) or 0)
            sources_enabled = int(
                summary.get("sources_enabled", summary.get("sources", 0)) or 0
            )
            recent_title = str(summary.get("recent_title", "") or "").strip()

            if pending_reviews > 0:
                badge = {
                    "count": pending_reviews,
                    "tone": "warm",
                    "label": f"{pending_reviews} 项待审",
                }
            elif today_logs > 0:
                badge = {
                    "count": today_logs,
                    "tone": "accent",
                    "label": f"{today_logs} 篇新精读",
                }
            elif active_interests > 0:
                badge = {
                    "count": active_interests,
                    "tone": "info",
                    "label": f"{active_interests} 项兴趣",
                }
            else:
                badge = {"count": 0, "tone": "accent", "label": "就绪"}

            if pending_reviews > 0:
                headline = (
                    f"{pending_reviews} 项待审提案 · {active_interests} 项活跃兴趣"
                )
            elif recent_title:
                headline = (
                    f"最近精读《{recent_title[:24]}》 · {active_interests} 项活跃兴趣"
                )
            else:
                headline = (
                    f"启用 {sources_enabled} 个信息源 · {active_interests} 项活跃兴趣"
                )

            latest_ts = max(
                float(latest_log_ts or 0.0),
                float(eng.last_digest_at or 0.0),
                float(eng.last_poll_at or 0.0),
            )
            tz = resolve_series_timezone(getattr(self, "context", None))
            updated_at = (
                datetime.fromtimestamp(latest_ts, tz).isoformat()
                if latest_ts > 0
                else ""
            )
            return {
                "badge": badge,
                "pulse": {"headline": headline, "updated_at": updated_at},
            }
        except Exception:
            return {
                "badge": {"count": 0, "tone": "muted", "label": "暂不可用"},
                "pulse": {"headline": "群间见闻状态读取异常", "updated_at": ""},
            }

    def get_series_health(self) -> dict[str, Any]:
        """Synchronous, read-only health diagnostics for series_panels.read_plugin_health."""
        try:
            plugin_enabled = bool(self.config.get("enabled", True))
            eng = self.engine
            is_ready = bool(self.ready and not self.closed and eng is not None)
            review_mode = (
                eng.review_mode
                if eng is not None
                else str(self.config.get("review_mode", "llm") or "llm").strip()
            )
            allow_same_review = bool(
                self.config.get("allow_same_provider_for_review", False)
            )
            issues: list[str] = []
            providers: list[dict[str, str]] = []

            if self.closed:
                issues.append("plugin_closed")
            elif not is_ready:
                issues.append("engine_not_ready")

            analysis_pid = self._effective_provider("analysis_provider")
            review_pid = self._effective_provider("review_provider")
            video_explicit = str(self.config.get("video_provider", "") or "").strip()
            video_pid = self._effective_provider("video_provider")
            stt_pid = self._effective_provider("video_audio_stt_provider")

            def _check_chat_provider(
                role: str,
                pid: str,
                *,
                required: bool,
                detail_ok: str = "available",
                detail_empty: str = "not_configured",
                empty_status: str = "missing",
            ) -> None:
                if not pid:
                    providers.append(
                        {
                            "role": role,
                            "provider_id": "",
                            "status": empty_status,
                            "detail": detail_empty,
                        }
                    )
                    if required and plugin_enabled:
                        issues.append(f"{role}_provider_missing")
                    return
                prov = find_provider_silently(self.context, pid)
                if prov is None:
                    providers.append(
                        {
                            "role": role,
                            "provider_id": pid,
                            "status": "missing",
                            "detail": "provider_not_found",
                        }
                    )
                    if plugin_enabled:
                        issues.append(f"{role}_provider_not_found:{pid}")
                    return
                if (
                    not callable(getattr(prov, "text_chat", None))
                    or getattr(prov, "is_native_decision_provider", False)
                    or getattr(prov, "is_systemone_provider", False)
                ):
                    providers.append(
                        {
                            "role": role,
                            "provider_id": pid,
                            "status": "invalid",
                            "detail": "unsupported_chat_provider",
                        }
                    )
                    if plugin_enabled:
                        issues.append(f"{role}_provider_invalid:{pid}")
                    return
                providers.append(
                    {
                        "role": role,
                        "provider_id": pid,
                        "status": "ok",
                        "detail": detail_ok,
                    }
                )

            _check_chat_provider("analysis", analysis_pid, required=True)
            _check_chat_provider(
                "review",
                review_pid,
                required=(review_mode != "human"),
                detail_empty=(
                    "human_review_mode" if review_mode == "human" else "not_configured"
                ),
                empty_status="disabled" if review_mode == "human" else "missing",
            )
            _check_chat_provider(
                "video",
                video_pid,
                required=bool(video_explicit),
                detail_ok=("available" if video_explicit else "fallback_to_analysis"),
                detail_empty="fallback_to_analysis",
                empty_status="unconfigured",
            )

            if not stt_pid:
                providers.append(
                    {
                        "role": "stt",
                        "provider_id": "",
                        "status": "unconfigured",
                        "detail": "optional",
                    }
                )
            else:
                stt_prov = find_provider_silently(self.context, stt_pid)
                if stt_prov is None:
                    providers.append(
                        {
                            "role": "stt",
                            "provider_id": stt_pid,
                            "status": "missing",
                            "detail": "provider_not_found",
                        }
                    )
                    if plugin_enabled:
                        issues.append(f"stt_provider_not_found:{stt_pid}")
                elif not callable(getattr(stt_prov, "get_text", None)) and not callable(
                    getattr(stt_prov, "text_chat", None)
                ):
                    providers.append(
                        {
                            "role": "stt",
                            "provider_id": stt_pid,
                            "status": "invalid",
                            "detail": "unsupported_stt_provider",
                        }
                    )
                    if plugin_enabled:
                        issues.append(f"stt_provider_invalid:{stt_pid}")
                else:
                    providers.append(
                        {
                            "role": "stt",
                            "provider_id": stt_pid,
                            "status": "ok",
                            "detail": (
                                "native_stt"
                                if callable(getattr(stt_prov, "get_text", None))
                                else "audio_chat_stt"
                            ),
                        }
                    )

            if (
                plugin_enabled
                and review_mode != "human"
                and not allow_same_review
                and analysis_pid
                and analysis_pid == review_pid
            ):
                issues.append("independent_review_provider_required")

            summary = eng.projection.summary() if eng is not None else {}
            now_ts = float(eng.clock()) if eng is not None else 0.0
            active_cooldowns = 0
            if eng is not None:
                for dom, until in dict(
                    getattr(eng, "_domain_cooldowns", {}) or {}
                ).items():
                    if float(until or 0.0) > now_ts:
                        active_cooldowns += 1
                        if plugin_enabled:
                            issues.append(f"domain_cooldown:{dom}")
                if eng.last_error and plugin_enabled:
                    issues.append(f"last_error:{str(eng.last_error)[:120]}")

            metrics = {
                "enabled": plugin_enabled,
                "review_mode": review_mode,
                "sources": int(summary.get("sources", 0) or 0),
                "active_interests": int(summary.get("active_interests", 0) or 0),
                "pending_reviews": int(summary.get("pending_reviews", 0) or 0),
                "active_domain_cooldowns": active_cooldowns,
                "media_checkpoints": len(
                    getattr(eng, "_media_checkpoints", {}) or {}
                )
                if eng is not None
                else 0,
                "projection_generation": int(
                    getattr(eng.projection, "generation", 0) or 0
                )
                if eng is not None
                else 0,
                "last_poll_at": float(getattr(eng, "last_poll_at", 0.0) or 0.0)
                if eng is not None
                else 0.0,
                "last_digest_at": float(getattr(eng, "last_digest_at", 0.0) or 0.0)
                if eng is not None
                else 0.0,
            }
            return {
                "ready": is_ready,
                "degraded": bool(not is_ready or (plugin_enabled and issues)),
                "providers": providers,
                "issues": issues,
                "metrics": metrics,
            }
        except Exception as exc:
            return {
                "ready": False,
                "degraded": True,
                "providers": [],
                "issues": [f"health_error:{type(exc).__name__}"],
                "metrics": {},
            }

    def get_series_scope_status(self, umo: str) -> dict[str, Any]:
        """Synchronous, read-only per-UMO scope status for Chat Dynamics control matrix."""
        try:
            clean_umo = str(umo or "").strip()
            if self.closed or not self.ready or self.engine is None:
                return {
                    "enabled": False,
                    "supported": False,
                    "persona_id": "",
                    "activity_today": None,
                    "summary": "群间见闻未就绪",
                    "version": PLUGIN_VERSION,
                }
            eng = self.engine
            now = eng.clock()
            tz = eng.timezone
            excluded_by_core = bool(
                clean_umo and is_series_group_excluded(self.context, umo=clean_umo)
            )
            plugin_enabled = bool(self.config.get("enabled", True))
            sessions = eng.sessions
            if excluded_by_core:
                scope_enabled = False
            elif not clean_umo:
                scope_enabled = plugin_enabled
            elif sessions:
                scope_enabled = plugin_enabled and (clean_umo in sessions)
            else:
                scope_enabled = plugin_enabled

            identity, allow_all = self._resolve_query_identity_readonly(scope=clean_umo)
            persona_id = unpack_identity(identity)[0] if identity else ""
            if not scope_enabled or (not identity and not allow_all):
                activity_today = 0
                counts = eng.store.summary_counts(
                    "", allow_all=False, now=now, half_life_days=eng.half_life_days
                )
                today_logs = 0
            else:
                activity_today = eng.store.count_scope_activity_today(
                    clean_umo, identity=identity, allow_all=allow_all, now=now, tz=tz
                )
                counts = eng.store.summary_counts(
                    identity,
                    allow_all=allow_all,
                    now=now,
                    half_life_days=eng.half_life_days,
                )
                today_logs, _ = eng.store.count_today_logs_and_latest(
                    identity, now=now, allow_all=allow_all, tz=tz
                )

            if excluded_by_core:
                summary_text = "主控排除（群间见闻已静默跳过）"
            elif not plugin_enabled:
                summary_text = "插件已停用"
            elif not scope_enabled:
                summary_text = "未在当前会话白名单内"
            elif clean_umo and not identity:
                summary_text = "尚未绑定角色 · 活跃兴趣 0 项 · 今日精读 0 篇"
            else:
                parts = [
                    f"活跃兴趣 {int(counts.get('active_interests', 0))} 项",
                    f"今日精读 {today_logs} 篇",
                ]
                if clean_umo and eng.outreach_allowed(clean_umo):
                    parts.append("主动分享开启")
                summary_text = " · ".join(parts)

            result = {
                "enabled": scope_enabled,
                "supported": False,
                "persona_id": persona_id,
                "activity_today": int(activity_today),
                "summary": summary_text,
                "version": int(eng.projection.generation),
            }
            if excluded_by_core:
                result["excluded_by_core"] = True
            return result
        except Exception:
            return {
                "enabled": None,
                "supported": False,
                "persona_id": "",
                "activity_today": None,
                "summary": "状态读取异常",
                "version": PLUGIN_VERSION,
            }

    def series_search_items(
        self, query: str, scope: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        """Synchronous, read-only search hook for federated Ctrl+K command palette."""
        try:
            q_str = str(query or "").strip()
            if (
                not q_str
                or self.closed
                or not self.ready
                or self.engine is None
                or not bool(self.config.get("enabled", True))
            ):
                return []
            eng = self.engine
            clean_scope = str(scope or "").strip()
            if clean_scope and eng.sessions and clean_scope not in eng.sessions:
                return []
            identity, allow_all = self._resolve_query_identity_readonly(
                scope=clean_scope
            )
            if not identity and not allow_all:
                return []
            try:
                lim = max(1, min(50, int(limit)))
            except (TypeError, ValueError):
                lim = 20
            return eng.store.search_items_readonly(
                q_str,
                identity=identity,
                allow_all=allow_all,
                limit=lim,
                half_life_days=eng.half_life_days,
                now=eng.clock(),
            )
        except Exception:
            return []

    def get_journal_events(
        self,
        persona: str = "",
        scope: str = "",
        date: str = "today",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Synchronous, read-only reading & interest events for World's Living Journal feed."""
        try:
            if (
                self.closed
                or not self.ready
                or self.engine is None
                or not bool(self.config.get("enabled", True))
            ):
                return []
            eng = self.engine
            clean_scope = str(scope or "").strip()
            if clean_scope and eng.sessions and clean_scope not in eng.sessions:
                return []

            now = eng.clock()
            tz = eng.timezone
            raw_date = str(date or "today").strip().lower()
            if not raw_date or raw_date == "today":
                start_ts, end_ts = shanghai_day_bounds(now, tz=tz)
            elif raw_date == "yesterday":
                start_ts, end_ts = shanghai_day_bounds(now - 86400.0, tz=tz)
            elif raw_date in {"all", "recent"}:
                start_ts, end_ts = now - 7 * 86400.0, now + 60.0
            else:
                try:
                    parsed = datetime.strptime(raw_date[:10], "%Y-%m-%d").replace(
                        tzinfo=tz
                    )
                    start_ts = parsed.timestamp()
                    end_ts = start_ts + 86400.0
                except ValueError:
                    start_ts, end_ts = shanghai_day_bounds(now, tz=tz)

            identity, allow_all = self._resolve_query_identity_readonly(
                persona=str(persona or "").strip(), scope=clean_scope
            )
            if not identity and not allow_all:
                return []
            try:
                lim = max(1, min(100, int(limit)))
            except (TypeError, ValueError):
                lim = 20
            return eng.store.list_journal_events_readonly(
                identity=identity,
                allow_all=allow_all,
                start_ts=start_ts,
                end_ts=end_ts,
                limit=lim,
                tz=tz,
            )
        except Exception:
            return []

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
        scope_umo = ""
        if isinstance(scope, str):
            scope_umo = scope.strip()
        elif isinstance(scope, dict):
            scope_umo = str(scope.get("umo") or "").strip()
        if not scope_umo and isinstance(character_scope, dict):
            scope_umo = str(character_scope.get("umo") or "").strip()
        if scope_umo and is_series_group_excluded(self.context, umo=scope_umo):
            return _empty_result("excluded_by_core")
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

    def _precheck_dynamics_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Pure-memory O(1) validation matching core.feedback.enqueue() without SQLite I/O."""
        engine = self.engine
        if engine is None:
            return {"accepted": False}
        scope = event.get("character_scope") or (
            event.get("scope") if isinstance(event.get("scope"), dict) else {}
        )
        umo = str(
            event.get("umo")
            or (
                event.get("scope")
                if isinstance(event.get("scope"), str)
                else scope.get("umo")
            )
            or ""
        ).strip()
        if not umo or (engine.sessions and umo not in engine.sessions):
            return {"accepted": False}
        if is_series_group_excluded(getattr(engine, "context", None), umo=umo):
            return {"accepted": False, "reason": "excluded_by_core"}
        if scope.get("umo") and scope["umo"] != umo:
            return {"accepted": False, "reason": "scope_mismatch"}
        ident = (
            make_identity(
                scope["persona_id"], scope.get("instance_id") or scope["persona_id"]
            )
            if scope.get("persona_id")
            else None
        )
        if not ident and event.get("identity"):
            ident = normalize_identity(event["identity"])
        if not ident:
            ident = engine.projection.resolve_identity(
                umo=umo, persona=str(event.get("persona") or "")
            )
        if not ident:
            return {"accepted": False, "reason": "unresolved_identity"}
        member = str(event.get("member") or "").strip()[:128]
        reply_obj = event.get("reply") if isinstance(event.get("reply"), dict) else {}
        eid = str(
            event.get("event_id")
            or event.get("turn_id")
            or reply_obj.get("message_id")
            or ""
        )
        if not eid or not member:
            return {"accepted": False, "reason": "missing_event_or_member"}
        kind = str(event.get("kind") or event.get("type") or "")
        reply = str(event.get("reply_text") or reply_obj.get("text") or "")[:2000]
        delivered = bool(
            (event.get("delivered") is True or reply_obj.get("delivered") is True)
            and kind in {"delivery.success", "turn_completed"}
        )
        qids = list(event.get("question_ids") or [])[:8]
        topics = [str(t) for t in (event.get("topics") or []) if t][:12]
        will_queue = bool(
            delivered
            and reply
            and (qids or topics or event.get("log_ids") or event.get("intent_id"))
        )
        return {"accepted": True, "queued": will_queue}

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

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            pre = self._precheck_dynamics_event(event)
            if not pre.get("accepted"):
                return {"schema_version": SCHEMA_VERSION, **pre}
            engine = self.engine
            event_copy = dict(event)

            async def _bg_ingest() -> None:
                try:
                    res = await run_io(enqueue, engine, event_copy)
                    if isinstance(res, dict) and res.get("projection_dirty"):
                        engine.schedule_projection_refresh()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug(
                        "[DynamicsHorizons] Async ingest_dynamics_event failed",
                        exc_info=True,
                    )

            task = loop.create_task(_bg_ingest())
            engine.track_ingest_task(task)
            return {
                "schema_version": SCHEMA_VERSION,
                **pre,
                "async_offloaded": True,
            }

        result = enqueue(self.engine, event)
        if result.pop("projection_dirty", False):
            self.engine.refresh_projection_sync()
        return {"schema_version": SCHEMA_VERSION, **result}

    # ------------------------------------------------------------------
    # Proactive Outreach Candidate Gate (Consumed by Chat Dynamics)
    # ------------------------------------------------------------------

    def horizons_outreach_allowed(self, umo: str) -> bool:
        clean_umo = str(umo or "")
        return bool(
            not self.closed
            and self.ready
            and self.engine
            and not is_series_group_excluded(self.context, umo=clean_umo)
            and self.engine.outreach_allowed(clean_umo)
        )

    def list_due_intents(self, now: float | None = None) -> list[dict[str, Any]]:
        if (
            self.closed
            or not self.ready
            or self.engine is None
            or not self.config.get("enabled", True)
            or is_series_in_quiet_hours(self.context)
        ):
            return []
        stamp = float(now) if isinstance(now, (int, float)) else self.engine.clock()
        return self.engine.store.list_due_intents(
            stamp,
            proactive_sessions=self.engine.effective_proactive_sessions(),
            quiet_hours=self.engine.quiet_hours,
            tz=self.engine.timezone,
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
            return resolve_series_provider(
                self.context,
                "",
                fallback_provider=HorizonsEngine._host_default_provider_id(self.context),
                role="reply",
            )
        if role == "review_provider":
            defaults = get_series_defaults(self.context)
            decision_p = str(defaults.get("decision_provider") or "").strip()
            allow_same = bool(self.config.get("allow_same_provider_for_review", False))
            if decision_p and (
                allow_same or decision_p != self._effective_provider("analysis_provider")
            ):
                return decision_p
            if allow_same:
                return resolve_series_provider(
                    self.context,
                    "",
                    fallback_provider=HorizonsEngine._host_default_provider_id(
                        self.context
                    ),
                    role="reply",
                )
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
        effective_instruction, effective_data = extract_persona_system_prompt(
            instruction, data
        )
        payload = encode_json(effective_data)
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
                use_genai = _resolve_genai_generate_fn(provider) is not None
                read_max_bytes = (
                    max_mb * 1024 * 1024
                    if use_genai
                    else min(max_mb * 1024 * 1024, _OPENAI_INLINE_VIDEO_MAX_BYTES)
                )
                try:
                    video_bytes, mime_type = await asyncio.to_thread(
                        _read_video_bytes, native_video_path, read_max_bytes
                    )
                    async with asyncio.timeout(effective_timeout):
                        if use_genai:
                            content = await series_llm_call(
                                self.context,
                                provider_id,
                                PLUGIN_ID,
                                f"horizons_{role}_video",
                                lambda: _invoke_genai_native_video(
                                    provider,
                                    instruction=effective_instruction,
                                    payload=payload,
                                    video_bytes=video_bytes,
                                    mime_type=mime_type,
                                ),
                                acquire_timeout=min(15.0, float(effective_timeout)),
                            )
                        else:
                            content = await series_llm_call(
                                self.context,
                                provider_id,
                                PLUGIN_ID,
                                f"horizons_{role}_video",
                                lambda: _invoke_openai_gemini_native_video(
                                    provider,
                                    instruction=effective_instruction,
                                    payload=payload,
                                    video_bytes=video_bytes,
                                    mime_type=mime_type,
                                ),
                                acquire_timeout=min(15.0, float(effective_timeout)),
                            )
                    self._assert_binding_current(role, provider_id, provider)
                    return decode_model_object(content)
                except asyncio.CancelledError:
                    raise
                except _BindingChangedError as exc:
                    raise ValueError(str(exc)) from None
                except Exception as exc:
                    if is_provider_budget_busy_error(exc):
                        raise
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
                        if fallback_labels and isinstance(effective_data, dict):
                            fb_data = dict(effective_data)
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
                    if fallback_labels and isinstance(effective_data, dict):
                        fb_data = dict(effective_data)
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
        anchor_contexts = build_cache_anchor_contexts(
            effective_instruction, task="dynamics_horizons"
        )
        call_kwargs: dict[str, Any] = {
            "prompt": payload,
            "system_prompt": effective_instruction,
        }
        if _accepts_kwarg(provider.text_chat, "contexts"):
            call_kwargs["contexts"] = anchor_contexts
        if image_urls:
            call_kwargs["image_urls"] = image_urls
            async with asyncio.timeout(effective_timeout):
                response = await series_llm_call(
                    self.context,
                    provider_id,
                    PLUGIN_ID,
                    f"horizons_{role}",
                    lambda: provider.text_chat(**call_kwargs),
                    acquire_timeout=min(15.0, float(effective_timeout)),
                )
        else:
            async with asyncio.timeout(base_timeout):
                response = await series_llm_call(
                    self.context,
                    provider_id,
                    PLUGIN_ID,
                    f"horizons_{role}",
                    lambda: provider.text_chat(**call_kwargs),
                    acquire_timeout=min(15.0, float(base_timeout)),
                )
        try:
            self._assert_binding_current(role, provider_id, provider)
        except _BindingChangedError as exc:
            raise ValueError(str(exc)) from None
        content = getattr(response, "completion_text", None)
        return decode_model_object(content)
