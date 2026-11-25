"""Background lifecycle helpers; never invoked by synchronous reply projection."""

from __future__ import annotations

import asyncio
import inspect
import math

from .. import series_panels as _series_panels
from ..series_panels import is_provider_budget_busy_error, is_series_group_excluded
from .contract import chat_umo_kind, make_identity
from .io import run_io

try:
    from ..adapters.video_media import cleanup_stale_temp_dirs
except Exception:  # pragma: no cover
    cleanup_stale_temp_dirs = None


def provider_circuit_remaining(context, provider_id: str) -> float:
    """Defensive read-only pre-flight lookup for shared provider circuit breaker remaining seconds."""
    pid = str(provider_id or "").strip()
    if context is None or not pid:
        return 0.0
    fn = getattr(_series_panels, "provider_circuit_remaining", lambda _c, _p: 0.0)
    if not callable(fn):
        return 0.0
    try:
        raw = float(fn(context, pid) or 0.0)
    except Exception:
        return 0.0
    if not math.isfinite(raw) or raw <= 0.0:
        return 0.0
    return raw


def busy_retry_after(
    exc: BaseException | None, *, default_min: float = 120.0
) -> float:
    """Extract structured ``retry_after`` from a budget-busy error with a safe minimum floor."""
    floor = max(0.0, float(default_min or 0.0))
    if exc is None:
        return floor
    try:
        raw = float(getattr(exc, "retry_after", 0.0) or 0.0)
    except (TypeError, ValueError):
        raw = 0.0
    if not math.isfinite(raw) or raw < 0.0:
        raw = 0.0
    return max(raw, floor)


def _caller_accepts_media_payload(fn) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == "media_payload":
            return True
    return False


class EngineLifecycle:
    async def call_model(
        self,
        role,
        instruction,
        payload,
        *,
        identity,
        lane="reading",
        media_payload=None,
    ):
        cid = await run_io(
            self.store.reserve_call,
            identity,
            role,
            max(1, int(self.config.get("daily_model_call_quota", 50))),
            self.clock(),
            lane=lane,
            tz=self.timezone,
        )
        try:
            if media_payload is not None and _caller_accepts_media_payload(
                self.model_caller
            ):
                value = await self.model_caller(
                    role, instruction, payload, media_payload=media_payload
                )
            else:
                value = await self.model_caller(role, instruction, payload)
            if not isinstance(value, dict):
                raise ValueError("model_output_not_object")
        except BaseException as exc:
            fail_status = (
                "exempt"
                if (
                    is_provider_budget_busy_error(exc)
                    or isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                )
                else "failed"
            )
            await asyncio.shield(run_io(self.store.finish_call, cid, fail_status))
            raise
        await run_io(self.store.finish_call, cid, "success")
        return value

    def _provider_id(self, role: str) -> str:
        fn = getattr(self, "effective_provider", None)
        if callable(fn):
            return str(fn(role) or "").strip()
        return str(self.config.get(role, "") or "").strip()

    def binding_revision(self):
        return (
            self.config.get("enabled", True),
            self._provider_id("analysis_provider"),
            self._provider_id("video_provider"),
            self._provider_id("review_provider"),
            self.config.get("review_mode", "llm"),
            bool(self.config.get("allow_same_provider_for_review", False)),
            bool(self.config.get("merge_same_provider_review", True)),
        )

    async def assert_reading_current(self, identity, wake, revision, source_id=""):
        if (
            self.closed
            or not self.config.get("enabled", True)
            or revision != self.binding_revision()
        ):
            raise ValueError("reading_binding_changed")
        current = await self.world_bridge.inspect_wake_and_profile(
            identity,
            now=self.clock(),
            quiet_hours=self.quiet_hours,
            sessions=self.sessions,
            tz=self.timezone,
        )
        if (
            not current["awake"]
            or current.get("character_prompt") != wake.get("character_prompt")
            or current.get("scope", {}).get("persona_id")
            != wake.get("scope", {}).get("persona_id")
        ):
            raise ValueError("reading_character_changed")
        if source_id:
            src = await run_io(self.store.get_source, source_id)
            if not src or not src["enabled"] or src["identity"] != identity:
                raise ValueError("reading_source_changed")

    async def process_maintenance(self):
        now = self.clock()
        prune_cp = getattr(self, "prune_media_checkpoints", None)
        if callable(prune_cp):
            prune_cp(now=now)
        if callable(cleanup_stale_temp_dirs):
            active_dirs_fn = getattr(self, "active_media_checkpoint_dirs", None)
            protected = active_dirs_fn() if callable(active_dirs_fn) else ()
            await run_io(cleanup_stale_temp_dirs, protected_dirs=protected)
        await run_io(
            self.store.archive_candidates,
            now,
            max(1, int(self.config.get("archive_after_days", 90))),
            max(1, int(self.config.get("active_candidate_limit", 5000))),
        )
        await run_io(
            self.store.prune_terminal_records,
            now,
            retention_days=max(
                7.0, float(self.config.get("terminal_retention_days", 30) or 30)
            ),
        )
        if not self.config.get("enabled", True) or self.closed:
            return
        review_mode = getattr(self, "review_mode", self.config.get("review_mode", "llm"))
        review_pid = self._provider_id("review_provider")
        review_circuit_rem = (
            0.0
            if review_mode == "human"
            else provider_circuit_remaining(getattr(self, "context", None), review_pid)
        )
        if review_circuit_rem <= 0.0:
            due_pids = await run_io(self.store.due_reviews, self.clock())
            for idx, pid in enumerate(due_pids):
                self._last_review_budget_busy_retry_after = 0.0
                busy_delay = 0.0
                try:
                    await self.review_proposal(pid)
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    if is_provider_budget_busy_error(exc):
                        busy_delay = busy_retry_after(exc, default_min=120.0)
                        await run_io(
                            self.store.defer_review,
                            pid,
                            self.clock(),
                            "provider_budget_busy",
                            delay=120.0,
                            min_delay=busy_delay,
                            backoff=True,
                        )
                if busy_delay <= 0.0:
                    busy_delay = float(
                        getattr(self, "_last_review_budget_busy_retry_after", 0.0)
                        or 0.0
                    )
                if busy_delay > 0.0:
                    remaining = due_pids[idx + 1 :]
                    if remaining:
                        target_next = self.clock() + max(busy_delay, 120.0)
                        await run_io(
                            self.store.reschedule_reviews,
                            remaining,
                            target_next,
                            "provider_budget_busy",
                        )
                    break
        await self.process_feedback_jobs()
        for row in await run_io(self.store.due_world, self.clock()):
            result = await self.world_bridge.sync_outbox(row, sessions=self.sessions)
            await run_io(
                self.store.finish_world,
                row,
                result.get("synced", False),
                str(result.get("reason", "")),
                self.clock(),
            )

    async def maintenance_loop(self):
        maint_lock = getattr(self, "_maintenance_lock", None) or self.lock
        while not self.closed:
            try:
                async with maint_lock:
                    await self.process_maintenance()
                    await self.refresh_projection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
            await asyncio.sleep(30)

    @property
    def proactive_scope(self) -> str:
        enabled = self.config.get("proactive_enabled")
        if not self.config.get("enabled", True) or enabled is False:
            return "disabled"
        if enabled is True:
            return "explicit" if self.proactive_sessions else "all_managed_chats"
        legacy_scope = self.config.get("proactive_scope", "explicit")
        if legacy_scope in ("all_managed_groups", "all_managed_chats"):
            return legacy_scope
        return "explicit" if self.proactive_sessions else "disabled"

    def outreach_allowed(self, umo: str) -> bool:
        scope = self.proactive_scope
        kind = chat_umo_kind(umo)
        managed_sessions = self.sessions
        if (
            scope == "disabled"
            or not kind
            or (managed_sessions and umo not in managed_sessions)
            or is_series_group_excluded(getattr(self, "context", None), umo=umo)
        ):
            return False
        if scope == "all_managed_groups":
            return kind == "groupmessage"
        if scope == "explicit":
            return umo in self.proactive_sessions and (
                self.config.get("proactive_enabled") is True or kind == "groupmessage"
            )
        return True

    def effective_proactive_sessions(self, identity: str = "") -> list[str]:
        scope = self.proactive_scope
        if scope == "disabled":
            return []
        if scope == "explicit" and self.config.get("proactive_enabled") is not True:
            # Older callers could configure a group before any captured scope existed.
            return [umo for umo in self.proactive_sessions if self.outreach_allowed(umo)]
        return [
            row["umo"]
            for row in self.store.known_managed_scopes(identity)
            if self.outreach_allowed(row["umo"])
        ]

    async def effective_proactive_sessions_async(self, identity: str = "") -> list[str]:
        scope = self.proactive_scope
        if scope == "disabled":
            return []
        if scope == "explicit" and self.config.get("proactive_enabled") is not True:
            return [umo for umo in self.proactive_sessions if self.outreach_allowed(umo)]
        rows = await run_io(self.store.known_managed_scopes, identity)
        return [row["umo"] for row in rows if self.outreach_allowed(row["umo"])]

    async def process_feedback_jobs(self):
        from .feedback import process_jobs

        flush_fn = getattr(self, "flush_pending_ingests", None)
        if callable(flush_fn):
            await flush_fn()
        await process_jobs(self)

    def wake_identity(self, wake):
        scope = wake.get("scope") or {}
        return make_identity(scope.get("persona_id", ""), scope.get("instance_id", ""))

    def _build_health(self, wake, diagnostics):
        proactive_scope = self.proactive_scope
        reasons = [wake["skip_reason"]] if wake and wake.get("skip_reason") else []
        if not self.config.get("enabled", True):
            reasons.append("disabled")
        analysis_provider = self._provider_id("analysis_provider")
        video_provider_explicit = str(self.config.get("video_provider", "") or "").strip()
        video_provider = self._provider_id("video_provider")
        video_provider_fallback = bool(not video_provider_explicit and video_provider)
        video_audio_stt_provider = str(
            self.config.get("video_audio_stt_provider", "") or ""
        ).strip()
        review_provider = self._provider_id("review_provider")
        if not analysis_provider:
            reasons.append("analysis_provider_missing")
        if self.review_mode != "human" and not review_provider:
            reasons.append("review_provider_missing")
        if (
            self.review_mode != "human"
            and not self.config.get("allow_same_provider_for_review", False)
            and analysis_provider
            and analysis_provider == review_provider
        ):
            reasons.append("independent_review_provider_required")
        plugins = dict(self.world_bridge.iter_plugins())
        names = set(plugins)
        chat = plugins.get("astrbot_plugin_chat_dynamics")
        if chat is not None and not callable(
            getattr(chat, "horizons_integration_capabilities", None)
        ):
            reasons.append("chat_dynamics_bridge_missing")
        if "astrbot_plugin_chat_dynamics" not in names:
            reasons.append("chat_dynamics_missing")
        world = self.world_bridge.find_world_plugin()
        if world is not None and not callable(
            getattr(world, "record_horizon_reading", None)
        ):
            reasons.append("world_bookmark_capability_missing")
        return {
            "blockers": reasons,
            "last_error": self.last_error,
            "analysis_provider": analysis_provider,
            "video_provider": video_provider,
            "video_provider_explicit": video_provider_explicit,
            "video_provider_fallback": video_provider_fallback,
            "video_audio_stt_provider": video_audio_stt_provider,
            "review_provider": review_provider,
            "review_context_policy": (
                "merged_self_review"
                if (
                    self.config.get("allow_same_provider_for_review", False)
                    and self.config.get("merge_same_provider_review", True)
                    and analysis_provider
                    and analysis_provider == review_provider
                    and self.review_mode != "human"
                )
                else (
                    "independent_calls"
                    if self.config.get("allow_same_provider_for_review", False)
                    else "distinct_providers"
                )
            ),
            "model_call_quota": self.config.get("daily_model_call_quota", 50),
            "storage_warning_mb": self.config.get("storage_warning_mb", 512),
            "proactive_enabled": proactive_scope != "disabled",
            "proactive_scope": proactive_scope,
            "diagnostics": diagnostics,
            "next_poll_at": self.next_poll_at,
            "next_digest_at": self.next_digest_at,
        }

    def health(self, identity, wake=None):
        diagnostics = self.store.diagnostics(
            identity, self.clock(), tz=getattr(self, "timezone", None)
        )
        return self._build_health(wake, diagnostics)

    async def health_async(self, identity, wake=None):
        diagnostics = await run_io(
            self.store.diagnostics,
            identity,
            self.clock(),
            tz=getattr(self, "timezone", None),
        )
        return self._build_health(wake, diagnostics)
