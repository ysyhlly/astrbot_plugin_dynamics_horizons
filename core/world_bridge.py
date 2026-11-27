"""Bidirectional companion bridge to Dynamics World (群间世界) with AstrBot persona fallback."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from typing import Any

from ..series_panels import (
    get_series_shared_config,
    is_series_in_quiet_hours,
    resolve_series_timezone,
)
from .contract import (
    is_in_quiet_hours,
    make_identity,
    normalize_identity,
    unpack_identity,
)
from .sanitize import strip_raw_urls


def _iter_star_instances(context: Any):
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return
    raw = getter()
    pairs = raw.items() if isinstance(raw, dict) else [("", row) for row in (raw or [])]
    for key, row in pairs:
        val = (
            row.get
            if isinstance(row, dict)
            else lambda k, default=None: getattr(row, k, default)
        )
        if val("activated", True) is False:
            continue
        plugin = val("star_cls") or val("star") or val("plugin")
        if plugin is None or isinstance(plugin, type):
            continue
        name = str(val("name") or val("root_dir_name") or key or "")
        yield name, plugin


async def _call_maybe_async(fn, *args, timeout: float = 5.0):
    res = fn(*args)
    if inspect.isawaitable(res):
        return await asyncio.wait_for(res, timeout=timeout)
    return res


class WorldBridge:
    """Read-only wake/character perception and lightweight reading bookmark sync."""

    _group_wake_scope_v2 = True

    def __init__(self, context: Any, projection_cache: Any = None) -> None:
        self.context = context
        self.projection = projection_cache

    def iter_plugins(self):
        return list(_iter_star_instances(self.context) or ())

    def world_installed(self):
        return any(
            name == "astrbot_plugin_dynamics_world" for name, _ in self.iter_plugins()
        )

    def _inspect_installed_world_state(self) -> tuple[bool, bool | None, bool | None]:
        """Return `(installed, enabled_flag, ready_flag)` for an activated World plugin."""
        for name, plugin in _iter_star_instances(self.context) or ():
            if name != "astrbot_plugin_dynamics_world":
                continue
            getter = getattr(plugin, "get_dynamics_capabilities", None)
            if not callable(getter) or inspect.iscoroutinefunction(getter):
                return True, None, None
            try:
                caps = getter()
            except Exception:
                return True, None, None
            if not isinstance(caps, dict):
                return True, None, None
            return (
                True,
                caps.get("enabled") if isinstance(caps.get("enabled"), bool) else None,
                caps.get("ready") if isinstance(caps.get("ready"), bool) else None,
            )
        return False, None, None

    def find_world_plugin(self) -> Any | None:
        found: list[Any] = []
        for name, plugin in _iter_star_instances(self.context) or ():
            getter = getattr(plugin, "get_dynamics_capabilities", None)
            if not callable(getter) or inspect.iscoroutinefunction(getter):
                continue
            try:
                caps = getter()
            except Exception:
                continue
            if not isinstance(caps, dict) or caps.get("schema_version") != 1:
                continue
            if caps.get("enabled") is not True or caps.get("ready") is not True:
                continue
            cdict = caps.get("capabilities")
            is_world = (
                caps.get("provider_id") == "astrbot_plugin_dynamics_world"
                or name == "astrbot_plugin_dynamics_world"
                or (
                    isinstance(cdict, dict)
                    and cdict.get("character") == 1
                    and cdict.get("life") == 1
                )
            )
            if is_world and all(existing is not plugin for existing in found):
                found.append(plugin)
        return found[0] if len(found) == 1 else None

    def _host_personas(self) -> list[dict[str, str]]:
        """Extract personas configured on the AstrBot host context if available."""
        results: list[dict[str, str]] = []
        for holder_attr in ("persona_manager", "provider_manager"):
            holder = getattr(self.context, holder_attr, None)
            if holder is None:
                continue
            raw_list = getattr(holder, "personas", None)
            if callable(raw_list):
                try:
                    raw_list = raw_list()
                except Exception:
                    raw_list = None
            if isinstance(raw_list, dict):
                items = list(raw_list.values())
            elif isinstance(raw_list, (list, tuple)):
                items = list(raw_list)
            else:
                continue
            for item in items:
                val = (
                    item.get
                    if isinstance(item, dict)
                    else lambda k, d="": getattr(item, k, d)
                )
                pid = str(val("persona_id") or val("name") or "").strip()
                prompt = str(val("prompt") or val("system_prompt") or "").strip()
                if not pid:
                    continue
                ident = make_identity(pid, pid)
                results.append(
                    {
                        "identity": ident,
                        "persona_id": pid,
                        "label": pid,
                        "prompt": prompt[:2000],
                    }
                )
                if self.projection is not None:
                    fp = hashlib.sha256(f"{pid}\0{prompt}".encode()).hexdigest()
                    self.projection.bind_persona_alias(fp, ident)
                    self.projection.bind_persona_alias(pid, ident)
        return results

    def _shared_persona_id(self) -> str:
        if self.context is None:
            return ""
        try:
            shared = get_series_shared_config(self.context)
            if isinstance(shared, dict):
                pid = str(shared.get("persona_id") or "").strip()
                if pid:
                    return pid[:128]
        except Exception:
            pass
        return ""

    def discover_personas(
        self,
        store: Any,
        sessions: list[str] | None = None,
        *,
        known_identities: list[str] | None = None,
    ) -> list[dict[str, str]]:
        by_identity: dict[str, dict[str, str]] = {}

        # 1. Inspect Dynamics World store cards if loaded
        world = self.find_world_plugin()
        if world is not None:
            try:
                w_store = getattr(getattr(world, "world", None), "store", None)
                cards = getattr(w_store, "cards", None)
                if isinstance(cards, dict):
                    for cid, card in cards.items():
                        c_id = str(
                            getattr(card, "card_id", None)
                            or (card.get("card_id") if isinstance(card, dict) else cid)
                            or ""
                        ).strip()
                        c_name = str(
                            getattr(card, "name", None)
                            or (card.get("name") if isinstance(card, dict) else c_id)
                            or c_id
                        ).strip()
                        if c_id:
                            pid = str(
                                (
                                    card.get("persona_id")
                                    if isinstance(card, dict)
                                    else getattr(card, "persona_id", "")
                                )
                                or f"wb_{c_id}"
                            )
                            ident = make_identity(pid, pid)
                            by_identity[ident] = {
                                "identity": ident,
                                "persona_id": pid,
                                "label": c_name or c_id,
                            }
            except Exception:
                pass

        # 2. Host AstrBot personas
        for hp in self._host_personas():
            by_identity.setdefault(
                hp["identity"],
                {
                    "identity": hp["identity"],
                    "persona_id": hp["persona_id"],
                    "label": hp["label"],
                },
            )

        # 3. Persisted identities in Horizons SQLite store
        persisted_ids: list[str] = []
        if known_identities is not None:
            persisted_ids = list(known_identities)
        elif store is not None and callable(getattr(store, "known_identities", None)):
            persisted_ids = list(store.known_identities())
        for ident in persisted_ids:
            norm = normalize_identity(ident)
            pid, inst = unpack_identity(norm)
            label = pid if pid == inst else f"{pid} ({inst})"
            by_identity.setdefault(
                norm,
                {
                    "identity": norm,
                    "persona_id": pid,
                    "label": label,
                },
            )

        # 4. Inherit series shared persona_id as preferred default when configured
        shared_pid = self._shared_persona_id()
        if shared_pid:
            matched_key = next(
                (
                    k
                    for k, v in by_identity.items()
                    if v.get("persona_id") == shared_pid
                ),
                "",
            )
            if not matched_key:
                shared_ident = make_identity(shared_pid, shared_pid)
                by_identity[shared_ident] = {
                    "identity": shared_ident,
                    "persona_id": shared_pid,
                    "label": shared_pid,
                }
                matched_key = shared_ident
            if matched_key in by_identity:
                preferred = by_identity.pop(matched_key)
                ordered = [preferred, *by_identity.values()]
                return ordered

        # 5. Always provide a deterministic fallback persona
        if not by_identity:
            default_ident = make_identity("default", "default")
            by_identity[default_ident] = {
                "identity": default_ident,
                "persona_id": "default",
                "label": "默认角色",
            }

        return list(by_identity.values())

    def _scope_for_identity(
        self, identity: str, sessions: list[str] | None = None
    ) -> dict[str, str]:
        persona_id, instance_id = unpack_identity(identity)
        umo = ""
        if self.projection is not None:
            for k_umo, v_id in getattr(self.projection, "_umo_identities", {}).items():
                if v_id == identity:
                    umo = k_umo
                    break
        # Background reading always asks for the character's global scope.
        umo = ""
        platform = umo.rsplit(":", 2)[0] if ":" in umo else ""
        return {
            "platform": platform,
            "umo": umo,
            "persona_id": persona_id,
            "instance_id": instance_id,
        }

    async def inspect_wake_and_profile(
        self,
        identity: str,
        *,
        now: float | None = None,
        quiet_hours: str = "00:00-08:00",
        sessions: list[str] | None = None,
        tz: Any = None,
    ) -> dict[str, Any]:
        norm_id = normalize_identity(identity)
        persona_id, _ = unpack_identity(norm_id)
        stamp = float(now if now is not None else time.time())
        scope = self._scope_for_identity(norm_id, sessions)

        world = self.find_world_plugin()
        if world is not None:
            char_ctx: dict[str, Any] = {}
            snap: dict[str, Any] = {}
            get_char = getattr(world, "get_character_context", None)
            if callable(get_char):
                try:
                    res = await _call_maybe_async(get_char, scope, timeout=5.0)
                    if isinstance(res, dict):
                        char_ctx = res
                        resolved_scope = res.get("scope")
                        if isinstance(resolved_scope, dict) and resolved_scope.get(
                            "persona_id"
                        ):
                            scope = {
                                "platform": str(
                                    resolved_scope.get("platform") or scope["platform"]
                                ),
                                "umo": str(resolved_scope.get("umo") or scope["umo"]),
                                "persona_id": str(resolved_scope.get("persona_id")),
                                "instance_id": str(
                                    resolved_scope.get("instance_id")
                                    or resolved_scope.get("persona_id")
                                ),
                            }
                            if scope["umo"] and self.projection is not None:
                                self.projection.bind_umo_identity(
                                    scope["umo"],
                                    make_identity(
                                        scope["persona_id"], scope["instance_id"]
                                    ),
                                )
                except Exception:
                    char_ctx = {}

            get_snap = getattr(world, "get_snapshot", None)
            if callable(get_snap):
                try:
                    # Query global character snapshot first (umo='') if scoped fails, or scoped directly
                    res = await _call_maybe_async(get_snap, scope, timeout=5.0)
                    if isinstance(res, dict):
                        snap = res
                except Exception:
                    try:
                        global_scope = dict(scope, umo="", platform="")
                        res = await _call_maybe_async(
                            get_snap, global_scope, timeout=5.0
                        )
                        if isinstance(res, dict):
                            snap = res
                    except Exception:
                        snap = {}

            # Group pause is authoritative too, but never borrow another
            # character's binding merely because it is the first configured UMO.
            known = set(sessions or [])
            world_config = getattr(world, "config", {})
            if hasattr(world_config, "get"):
                known.update(world_config.get("narrative_sessions", []) or [])
            if self.projection is not None:
                known.update(
                    k
                    for k, v in self.projection._umo_identities.items()
                    if v == norm_id
                )
            for group_umo in sorted(known):
                parts = str(group_umo).rsplit(":", 2)
                if len(parts) != 3 or parts[1].lower() != "groupmessage":
                    continue
                if not callable(get_char) or not callable(get_snap):
                    break
                requested = dict(
                    scope, umo=group_umo, platform=group_umo.rsplit(":", 2)[0]
                )
                try:
                    scoped_char = await _call_maybe_async(
                        get_char, requested, timeout=5
                    )
                    actual = (
                        scoped_char.get("scope", {})
                        if isinstance(scoped_char, dict)
                        else {}
                    )
                    if not scoped_char.get("available") or any(
                        actual.get(k) != scope.get(k)
                        for k in ("persona_id", "instance_id")
                    ):
                        continue
                    scoped_snap = await _call_maybe_async(
                        get_snap, requested, timeout=5
                    )
                    if (
                        isinstance(scoped_snap, dict)
                        and scoped_snap.get("managed") is False
                    ):
                        continue
                    if not isinstance(scoped_snap, dict) or not scoped_snap.get(
                        "availability"
                    ):
                        snap = {}
                        break
                    if (
                        scoped_snap.get("paused")
                        or scoped_snap.get("availability") != "available"
                    ):
                        snap = scoped_snap
                        break
                except Exception:
                    snap = {}
                    break

            valid_character = bool(
                char_ctx.get("available") is True
                and not char_ctx.get("disabled")
                and scope.get("persona_id") == persona_id
                and scope.get("instance_id") == unpack_identity(norm_id)[1]
            )
            valid_snapshot = bool(
                snap and snap.get("managed", True) and snap.get("availability")
            )
            availability = str(snap.get("availability") or "unknown")
            paused = bool(snap.get("paused") is True)
            asleep = availability == "asleep"
            spatial = (
                snap.get("spatial") if isinstance(snap.get("spatial"), dict) else {}
            )
            in_transit = bool(
                spatial.get("journey")
                or spatial.get("in_transit")
                or spatial.get("status") == "travelling"
                or spatial.get("phase") == "travelling"
            )
            activity = str(snap.get("activity") or "").strip()

            skip_reason = ""
            if not valid_character or not valid_snapshot:
                skip_reason = "world_state_unavailable"
            elif paused:
                skip_reason = "world_paused"
            elif asleep:
                skip_reason = "world_asleep"
            elif in_transit:
                skip_reason = "world_in_transit"

            if not skip_reason and availability != "available":
                skip_reason = "world_busy"
            awake = not bool(skip_reason)
            char_name = str(char_ctx.get("name") or persona_id)[:80]
            char_prompt = str(
                char_ctx.get("core_prompt")
                or char_ctx.get("personality")
                or char_ctx.get("description")
                or ""
            )[:2400]

            if not char_prompt.strip():
                awake = False
                skip_reason = "character_profile_missing"

            prefs: list[str] = []
            humanlike = (
                snap.get("humanlike") if isinstance(snap.get("humanlike"), dict) else {}
            )
            for key in ("preferences", "interests"):
                for item in (humanlike.get(key) or [])[:6]:
                    if isinstance(item, dict):
                        txt = str(
                            item.get("summary")
                            or item.get("text")
                            or item.get("topic")
                            or ""
                        ).strip()
                        if txt:
                            prefs.append(txt[:100])
                    elif isinstance(item, str) and item.strip():
                        prefs.append(item.strip()[:100])

            status_summary = (
                "生活已暂停（暂不阅读）"
                if paused
                else "熟睡休息中（暂不阅读）"
                if asleep
                else "旅途奔波中（暂不阅读）"
                if in_transit
                else f"已联动 · {activity or availability}"
            )
            return {
                "world_connected": valid_character and valid_snapshot,
                "awake": awake,
                "asleep": asleep,
                "paused": paused,
                "in_transit": in_transit,
                "skip_reason": skip_reason,
                "availability": availability,
                "activity": activity,
                "character_name": char_name,
                "character_prompt": char_prompt,
                "preferences": prefs,
                "scope": scope,
                "world_link": {
                    "connected": valid_character and valid_snapshot,
                    "asleep": asleep,
                    "paused": paused,
                    "availability": availability,
                    "summary": status_summary[:120],
                },
            }

        # Fallback: AstrBot host persona + configured quiet_hours (or shared quiet hours)
        eff_tz = tz or resolve_series_timezone(self.context)
        in_quiet = is_in_quiet_hours(stamp, quiet_hours, tz=eff_tz) or is_series_in_quiet_hours(
            self.context
        )
        host_prompt = ""
        host_personas = self._host_personas()
        for hp in host_personas:
            if hp["persona_id"] == persona_id or hp["identity"] == norm_id:
                host_prompt = hp.get("prompt", "")
                break
        if not host_prompt and persona_id == "default":
            shared_pid = self._shared_persona_id()
            if shared_pid:
                for hp in host_personas:
                    if hp["persona_id"] == shared_pid:
                        host_prompt = hp.get("prompt", "")
                        break

        installed, world_enabled, world_ready = self._inspect_installed_world_state()
        has_host = bool(host_prompt.strip())
        # Allow graceful fallback to host persona when World is disabled or not yet ready,
        # instead of permanently deadlocking reading in 'world_unavailable'.
        blocked = (installed and world_enabled is True and world_ready is True) or not has_host
        if blocked:
            skip_reason = "world_unavailable" if installed else "character_profile_missing"
        elif in_quiet:
            skip_reason = "quiet_hours"
        else:
            skip_reason = ""

        if installed and not blocked:
            state_label = "已停用" if world_enabled is False else "未就绪"
            mode_summary = (
                f"群间世界{state_label} · 静默时段({quiet_hours})免读中"
                if in_quiet
                else f"群间世界{state_label} · 降级按宿主角色与静默时段运行"
            )
        else:
            mode_summary = (
                f"未接入群间世界 · 静默时段({quiet_hours})免读中"
                if in_quiet
                else "未接入群间世界 · 按宿主角色与静默时段运行"
            )

        return {
            "world_connected": False,
            "awake": not in_quiet and not blocked,
            "asleep": in_quiet,
            "paused": False,
            "in_transit": False,
            "skip_reason": skip_reason,
            "availability": "asleep" if in_quiet else "available",
            "activity": "",
            "character_name": persona_id,
            "character_prompt": host_prompt,
            "preferences": [],
            "scope": scope,
            "world_link": {
                "connected": False,
                "asleep": in_quiet,
                "paused": False,
                "availability": "asleep" if in_quiet else "available",
                "summary": mode_summary,
            },
        }

    async def sync_reading_receipt(
        self,
        identity: str,
        log_record: dict[str, Any],
        *,
        sessions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Push approved lightweight reading bookmark to Dynamics World if supported."""
        world = self.find_world_plugin()
        if world is None:
            return {"synced": False, "reason": "world_offline"}
        recorder = getattr(world, "record_horizon_reading", None)
        if not callable(recorder):
            return {"synced": False, "reason": "method_absent"}

        norm_id = normalize_identity(identity)
        scope = self._scope_for_identity(norm_id, sessions)
        cit = (
            log_record.get("citation")
            if isinstance(log_record.get("citation"), dict)
            else {}
        )
        payload = {
            "log_id": str(log_record.get("log_id") or ""),
            "source_kind": str(log_record.get("source_kind") or "manual"),
            "title": strip_raw_urls(str(cit.get("title") or ""))[:160],
            "creator": strip_raw_urls(str(cit.get("creator") or ""))[:80],
            "chapter_label": strip_raw_urls(str(cit.get("chapter_label") or ""))[:80],
            "digest": strip_raw_urls(str(log_record.get("digest") or ""))[:200],
            "reflection": strip_raw_urls(str(log_record.get("reflection") or ""))[:150],
            "occurred_at": float(log_record.get("created_at") or time.time()),
        }
        try:
            res = await _call_maybe_async(recorder, scope, payload, timeout=5.0)
            return {"synced": True, "result": res}
        except Exception as exc:
            return {"synced": False, "reason": type(exc).__name__}

    async def sync_outbox(self, row, *, sessions=None):
        import json

        world = self.find_world_plugin()
        if world is None:
            return {"synced": False, "reason": "world_offline"}
        recorder = getattr(world, "record_horizon_reading", None)
        if not callable(recorder):
            return {"synced": False, "reason": "method_absent"}
        payload = json.loads(row["payload_json"])
        payload.update(operation=row["operation"], revision=row["revision"])
        try:
            result = await _call_maybe_async(
                recorder, self._scope_for_identity(row["identity"], sessions), payload
            )
            return {
                "synced": isinstance(result, dict) and result.get("ok") is True,
                "reason": ""
                if isinstance(result, dict) and result.get("ok")
                else "receipt_rejected",
            }
        except Exception as exc:
            return {"synced": False, "reason": type(exc).__name__}

    # ------------------------------------------------------------------
    # Wave 2: Learning & Memory Zero-Coupling Companion Bridges
    # ------------------------------------------------------------------

    def find_learning_plugin(self) -> Any | None:
        for name, plugin in _iter_star_instances(self.context) or ():
            if name == "astrbot_plugin_dynamics_learning" or callable(
                getattr(plugin, "get_horizons_social_hints", None)
            ):
                return plugin
        return None

    def find_memory_plugin(self) -> Any | None:
        for name, plugin in _iter_star_instances(self.context) or ():
            if name == "astrbot_plugin_dynamics_memory":
                return plugin
        return None

    async def fetch_learning_social_hints(
        self, umos: list[str], *, member_id: str = ""
    ) -> dict[str, Any]:
        """Zero-coupling read of `dynamics_learning.get_horizons_social_hints(umo, member_id)`."""
        empty = {"group_interests": [], "member_taboos": {}}
        learning = self.find_learning_plugin()
        if learning is None:
            return empty
        getter = getattr(learning, "get_horizons_social_hints", None)
        if not callable(getter):
            return empty

        clean_umos = [str(u).strip() for u in (umos or []) if str(u or "").strip()]
        if not clean_umos:
            return empty

        from .sanitize import sanitize_untrusted_text

        group_interests: list[str] = []
        seen_interests: set[str] = set()
        taboos: dict[tuple[str, str], set[str]] = {}
        clean_member = str(member_id or "").strip()

        for umo in dict.fromkeys(clean_umos[:12]):
            try:
                res = await _call_maybe_async(getter, umo, clean_member, timeout=3.0)
            except TypeError:
                try:
                    res = await _call_maybe_async(getter, umo, timeout=3.0)
                except Exception:
                    continue
            except Exception:
                continue
            if not isinstance(res, dict):
                continue
            for raw_topic in (res.get("group_interests") or [])[:12]:
                clean_t = strip_raw_urls(
                    sanitize_untrusted_text(str(raw_topic or ""), max_chars=48)["text"]
                ).strip()
                key = clean_t.casefold()
                if len(clean_t) >= 2 and key not in seen_interests:
                    seen_interests.add(key)
                    group_interests.append(clean_t)
            for field in ("member_dislikes", "member_boundaries"):
                mapping = res.get(field)
                if not isinstance(mapping, dict):
                    continue
                for mid, raw_items in mapping.items():
                    m_key = str(mid or "").strip()
                    if not m_key:
                        continue
                    items_iter = (
                        raw_items
                        if isinstance(raw_items, (list, tuple, set, frozenset))
                        else [raw_items]
                    )
                    bucket = taboos.setdefault((umo, m_key), set())
                    for term in items_iter:
                        ct = strip_raw_urls(
                            sanitize_untrusted_text(str(term or ""), max_chars=60)["text"]
                        ).strip()
                        if len(ct) >= 2:
                            bucket.add(ct.casefold())

        frozen_taboos = {k: frozenset(v) for k, v in taboos.items() if v}
        if self.projection is not None and hasattr(self.projection, "update_member_taboos"):
            # Merge with existing cached taboos for other UMOs when querying a subset
            merged = dict(getattr(self.projection, "_member_taboos", {}) or {})
            for umo in clean_umos[:12]:
                for k in [key for key in merged if key[0] == umo and (not clean_member or key[1] == clean_member)]:
                    merged.pop(k, None)
            merged.update(frozen_taboos)
            self.projection.update_member_taboos(merged)

        return {
            "group_interests": group_interests[:12],
            "member_taboos": frozen_taboos,
        }

    async def notify_learning_reflection(
        self,
        *,
        umo: str,
        member_id: str,
        topic: str,
        summary: str,
        sentiment: str = "positive",
    ) -> dict[str, Any]:
        """Push verified deep discussion reflection back to `dynamics_learning`."""
        learning = self.find_learning_plugin()
        if learning is None:
            return {"synced": False, "reason": "learning_offline"}
        ingestor = getattr(learning, "ingest_horizons_reflection", None)
        if not callable(ingestor):
            return {"synced": False, "reason": "method_absent"}
        clean_umo = str(umo or "").strip()
        clean_member = str(member_id or "").strip()
        clean_topic = strip_raw_urls(str(topic or "")).strip()[:64]
        clean_summary = strip_raw_urls(str(summary or "")).strip()[:200]
        if not clean_umo or not clean_member or not clean_topic or not clean_summary:
            return {"synced": False, "reason": "invalid_payload"}
        try:
            res = ingestor(
                clean_umo,
                clean_member,
                clean_topic,
                clean_summary,
                sentiment=str(sentiment or "positive"),
            )
            if inspect.isawaitable(res):
                res = await asyncio.wait_for(res, timeout=3.0)
            return {"synced": True, "result": res}
        except Exception as exc:
            return {"synced": False, "reason": type(exc).__name__}

    async def sync_memory_reading_note(
        self,
        *,
        identity: str,
        log_record: dict[str, Any],
        topic: str = "",
    ) -> dict[str, Any]:
        """Lightly archive an approved reading journal note into `dynamics_memory` if supported."""
        memory = self.find_memory_plugin()
        if memory is None:
            return {"synced": False, "reason": "memory_offline"}
        method_name = ""
        ingestor = None
        for cand in (
            "ingest_horizons_note",
            "record_horizons_note",
            "ingest_external_note",
            "add_memory_note",
        ):
            fn = getattr(memory, cand, None)
            if callable(fn):
                method_name = cand
                ingestor = fn
                break
        if ingestor is None:
            return {"synced": False, "reason": "method_absent"}

        norm_id = normalize_identity(identity)
        persona_id, _ = unpack_identity(norm_id)
        cit = (
            log_record.get("citation")
            if isinstance(log_record.get("citation"), dict)
            else {}
        )
        payload = {
            "source_plugin": "astrbot_plugin_dynamics_horizons",
            "fact_kind": "external_media",
            "identity": norm_id,
            "persona_id": persona_id,
            "log_id": str(log_record.get("log_id") or ""),
            "topic": strip_raw_urls(str(topic or "")).strip()[:48],
            "title": strip_raw_urls(str(cit.get("title") or "")).strip()[:120],
            "creator": strip_raw_urls(str(cit.get("creator") or "")).strip()[:60],
            "digest": strip_raw_urls(str(log_record.get("digest") or "")).strip()[:200],
            "reflection": strip_raw_urls(str(log_record.get("reflection") or "")).strip()[:150],
            "created_at": float(log_record.get("created_at") or time.time()),
        }
        try:
            res = ingestor(payload)
            if inspect.isawaitable(res):
                res = await asyncio.wait_for(res, timeout=3.0)
            return {"synced": True, "method": method_name, "result": res}
        except Exception as exc:
            return {"synced": False, "reason": type(exc).__name__}
