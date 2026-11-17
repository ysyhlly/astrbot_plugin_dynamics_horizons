"""Background lifecycle helpers; never invoked by synchronous reply projection."""

from __future__ import annotations

import asyncio
from .io import run_io

from .contract import chat_umo_kind, make_identity


class EngineLifecycle:
    async def call_model(self, role, instruction, payload, *, identity):
        cid = await run_io(
            self.store.reserve_call,
            identity,
            role,
            max(1, int(self.config.get("daily_model_call_quota", 50))),
            self.clock(),
        )
        try:
            value = await self.model_caller(role, instruction, payload)
            if not isinstance(value, dict):
                raise ValueError("model_output_not_object")
        except BaseException:
            await asyncio.shield(run_io(self.store.finish_call, cid, "failed"))
            raise
        await run_io(self.store.finish_call, cid, "success")
        return value

    def binding_revision(self):
        return (
            self.config.get("enabled", True),
            self.config.get("analysis_provider", ""),
            self.config.get("review_provider", ""),
            self.config.get("review_mode", "llm"),
            bool(self.config.get("allow_same_provider_for_review", False)),
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
        await run_io(
            self.store.archive_candidates,
            self.clock(),
            max(1, int(self.config.get("archive_after_days", 90))),
            max(1, int(self.config.get("active_candidate_limit", 5000))),
        )
        if not self.config.get("enabled", True) or self.closed:
            return
        for pid in await run_io(self.store.due_reviews, self.clock()):
            try:
                await self.review_proposal(pid)
            except Exception as exc:
                self.last_error = type(exc).__name__
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
        while not self.closed:
            try:
                async with self.lock:
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

    async def process_feedback_jobs(self):
        from .feedback import process_jobs

        await process_jobs(self)

    def wake_identity(self, wake):
        scope = wake.get("scope") or {}
        return make_identity(scope.get("persona_id", ""), scope.get("instance_id", ""))

    def health(self, identity, wake=None):
        proactive_scope = self.proactive_scope
        reasons = [wake["skip_reason"]] if wake and wake.get("skip_reason") else []
        if not self.config.get("enabled", True):
            reasons.append("disabled")
        if not self.config.get("analysis_provider"):
            reasons.append("analysis_provider_missing")
        if self.review_mode != "human" and not self.config.get("review_provider"):
            reasons.append("review_provider_missing")
        if (
            self.review_mode != "human"
            and not self.config.get("allow_same_provider_for_review", False)
            and self.config.get("analysis_provider")
            and self.config.get("analysis_provider")
            == self.config.get("review_provider")
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
            "analysis_provider": self.config.get("analysis_provider", ""),
            "review_provider": self.config.get("review_provider", ""),
            "review_context_policy": "independent_calls"
            if self.config.get("allow_same_provider_for_review", False)
            else "distinct_providers",
            "model_call_quota": self.config.get("daily_model_call_quota", 50),
            "storage_warning_mb": self.config.get("storage_warning_mb", 512),
            "proactive_enabled": proactive_scope != "disabled",
            "proactive_scope": proactive_scope,
            "diagnostics": self.store.diagnostics(identity, self.clock()),
            "next_poll_at": self.next_poll_at,
            "next_digest_at": self.next_digest_at,
        }
