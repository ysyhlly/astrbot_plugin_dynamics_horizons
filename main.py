# ruff: noqa: E402 -- owned module refresh must precede imports on hot reload.
from __future__ import annotations

from .reload_bootstrap import ensure_current

ensure_current(
    __package__,
    __name__,
    __file__,
    checks=[
        ("core.engine", "HorizonsEngine", "_horizons_contract_v1"),
        ("core.runtime", "EngineLifecycle", "proactive_scope"),
        ("core.world_bridge", "WorldBridge", "_group_wake_scope_v2"),
    ],
    version='0.2.0-beta.2.reliability.1.expression.1.allchats.1.bili.2.discussion.1',
)

import asyncio
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

    async def _model(
        self, role: str, instruction: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        provider_id = str(self.config.get(role, "") or "").strip()
        if not provider_id:
            label = "阅读与分析模型" if role == "analysis_provider" else "独立审查模型"
            raise ValueError(f"请先配置{label} ({role})")
        provider = self.context.get_provider_by_id(provider_id)
        if (
            provider is None
            or not callable(getattr(provider, "text_chat", None))
            or getattr(provider, "is_native_decision_provider", False)
            or getattr(provider, "is_systemone_provider", False)
        ):
            raise ValueError("所选模型不可用或不是普通聊天模型")
        timeout = max(5, min(180, int(self.config.get("model_timeout", 60) or 60)))
        payload = encode_json(data)
        if len(payload) > 140_000:
            raise ValueError("阅读材料超出单次模型输入预算")
        async with asyncio.timeout(timeout):
            response = await provider.text_chat(
                prompt=payload, system_prompt=instruction, contexts=[]
            )
        if (
            self.closed
            or not self.config.get("enabled", True)
            or self.context.get_provider_by_id(provider_id) is not provider
            or str(self.config.get(role, "") or "").strip() != provider_id
        ):
            raise ValueError("模型绑定已改变，本次结果作废")
        content = getattr(response, "completion_text", None)
        return decode_model_object(content)
