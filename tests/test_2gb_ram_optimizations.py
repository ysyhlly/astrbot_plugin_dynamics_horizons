"""Focused 2GB RAM optimization tests for astrbot_plugin_dynamics_horizons."""
from __future__ import annotations

import asyncio
import base64
import importlib
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image


def _ensure_offline_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return
    try:
        importlib.import_module("astrbot.api")
        return
    except ModuleNotFoundError:
        pass

    astrbot_mod = types.ModuleType("astrbot")
    astrbot_mod.__path__ = []  # type: ignore[attr-defined]
    api_mod = types.ModuleType("astrbot.api")

    class _NullLogger:
        def info(self, *a, **kw):
            pass

        def warning(self, *a, **kw):
            pass

        def error(self, *a, **kw):
            pass

        def debug(self, *a, **kw):
            pass

    api_mod.logger = _NullLogger()  # type: ignore[attr-defined]

    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.json_response = lambda data, status_code=200: {"data": data, "status_code": status_code}  # type: ignore[attr-defined]
    web_mod.request = None  # type: ignore[attr-defined]

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.web"] = web_mod


_ensure_offline_astrbot_stub()

# Load modules cleanly without triggering optional bilibili_api import in adapters/__init__.py
from astrbot_plugin_dynamics_horizons import series_panels  # noqa: E402
from astrbot_plugin_dynamics_horizons.core import feedback as feedback_mod  # noqa: E402
from astrbot_plugin_dynamics_horizons.core import projection as proj_mod  # noqa: E402
from astrbot_plugin_dynamics_horizons.core import runtime as runtime_mod  # noqa: E402
from astrbot_plugin_dynamics_horizons.core.contract import make_identity  # noqa: E402
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore  # noqa: E402


def _load_adapter_module(subname: str):
    """Import an adapters submodule directly even when optional bilibili_api is absent."""
    pkg_name = "astrbot_plugin_dynamics_horizons.adapters"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(Path(__file__).parents[1] / "adapters")]  # type: ignore[attr-defined]
        sys.modules[pkg_name] = pkg
    return importlib.import_module(f"{pkg_name}.{subname}")


vm = _load_adapter_module("video_media")
ebook_mod = _load_adapter_module("ebook")
from astrbot_plugin_dynamics_horizons.core import web_api as web_api_mod  # noqa: E402

IDENT = make_identity("p_opt", "p_opt")


def _make_profile_ctx(profile: str):
    owner = NS(
        get_series_shared_config=lambda: {
            "enabled": True,
            "resource_profile": profile,
        }
    )
    star = NS(activated=True, star_cls=owner, name="astrbot_plugin_chat_dynamics")
    return NS(
        get_all_stars=lambda: {"astrbot_plugin_chat_dynamics": star},
        get_registered_star=lambda name: star if name == "astrbot_plugin_chat_dynamics" else None,
    )


def test_video_media_temp_dir_uses_plugin_disk_data_dir(tmp_path):
    prev_root = vm._MEDIA_TEMP_ROOT
    prev_ctx = vm._MEDIA_CONTEXT
    try:
        disk_root = tmp_path / "plugin_data"
        vm.configure_media_runtime(data_dir=disk_root)
        td = vm.make_temp_dir("BV1test")
        assert (disk_root / "cache" / "tmp").resolve() in td.resolve().parents
        assert td.is_dir()
        vm.cleanup_temp_dir(td)
        assert not td.exists()

        # Backward compat: system tempdir horizons_bili_* still cleaned up safely
        sys_td = Path(tempfile.mkdtemp(prefix="horizons_bili_compat_"))
        assert sys_td.is_dir()
        vm.cleanup_temp_dir(sys_td)
        assert not sys_td.exists()
    finally:
        vm._MEDIA_TEMP_ROOT = prev_root
        vm._MEDIA_CONTEXT = prev_ctx


def test_video_media_ffmpeg_threads_nokey_sem1_and_heavy_gate(tmp_path, monkeypatch):
    prev_root = vm._MEDIA_TEMP_ROOT
    prev_ctx = vm._MEDIA_CONTEXT
    ctx = _make_profile_ctx("low_memory")
    vm.configure_media_runtime(data_dir=tmp_path / "data", context=ctx)
    try:
        video_file = tmp_path / "sample.mp4"
        video_file.write_bytes(b"fake-mp4")

        monkeypatch.setattr(vm.shutil, "which", lambda name: f"/usr/bin/{name}")
        commands: list[list[str]] = []
        active = {"cur": 0, "peak": 0}

        async def fake_run(args, *, timeout):
            commands.append(list(args))
            active["cur"] += 1
            active["peak"] = max(active["peak"], active["cur"])
            await asyncio.sleep(0.01)
            active["cur"] -= 1
            if str(args[-1]).endswith(".jpg"):
                out = Path(args[-1])
                Image.new("RGB", (120, 80), (10, 20, 30)).save(out, format="JPEG")
            elif str(args[-1]).endswith(".mp4"):
                Path(args[-1]).write_bytes(b"x" * 1024)
            return 0, b"120.0", b"pts_time:12.5\npts_time:45.0\n"

        monkeypatch.setattr(vm, "_run_subprocess", fake_run)

        # 1) detect_scene_timestamps uses -threads 1 and -skip_frame nokey
        pts = asyncio.run(vm.detect_scene_timestamps(video_file, duration_sec=120.0))
        assert pts == [12.5, 45.0]
        scene_cmd = commands[-1]
        assert "-threads" in scene_cmd and scene_cmd[scene_cmd.index("-threads") + 1] == "1"
        assert "-skip_frame" in scene_cmd and scene_cmd[scene_cmd.index("-skip_frame") + 1] == "nokey"

        # 2) detect_scene_timestamps skips >10 min videos in low_memory*
        before_n = len(commands)
        pts_long = asyncio.run(vm.detect_scene_timestamps(video_file, duration_sec=900.0))
        assert pts_long == []
        assert len(commands) == before_n

        # 3) extract_keyframes_from_video enforces Semaphore(1) & -threads 1 & acquires heavy gate
        gate_before = series_panels.get_heavy_subprocess_snapshot(ctx)["total_acquired"]
        frames = asyncio.run(
            vm.extract_keyframes_from_video(
                video_file,
                tmp_path / "frames",
                duration_sec=120.0,
                max_frames=4,
                use_scene_detect=False,
            )
        )
        assert len(frames) == 4
        assert active["peak"] == 1
        assert series_panels.get_heavy_subprocess_snapshot(ctx)["total_acquired"] == gate_before + 1
        frame_cmds = [c for c in commands if str(c[-1]).endswith(".jpg")]
        assert all("-threads" in c and c[c.index("-threads") + 1] == "1" for c in frame_cmds)

        # 4) mux_and_compress_native_video uses -threads 1
        out_mp4 = tmp_path / "native.mp4"
        size = asyncio.run(
            vm.mux_and_compress_native_video(video_file, None, out_mp4, duration_sec=60.0)
        )
        assert size == 1024
        mux_cmd = commands[-1]
        assert "-threads" in mux_cmd and mux_cmd[mux_cmd.index("-threads") + 1] == "1"
    finally:
        vm._MEDIA_TEMP_ROOT = prev_root
        vm._MEDIA_CONTEXT = prev_ctx


def test_stamp_font_cache_and_rgb_stamping(tmp_path):
    vm._load_stamp_font.cache_clear()
    f1 = vm._load_stamp_font(16)
    f2 = vm._load_stamp_font(16)
    assert f1 is f2
    assert vm._load_stamp_font.cache_info().hits >= 1

    img_path = tmp_path / "frame.jpg"
    Image.new("RGB", (320, 180), (50, 90, 130)).save(img_path, format="JPEG")
    vm.stamp_timestamp_on_image(img_path, "01:23")
    with Image.open(img_path) as stamped:
        assert stamped.mode == "RGB"
        assert stamped.size == (320, 180)


def test_aggressive_profile_prefers_videoshot_with_subtitles(tmp_path, monkeypatch):
    prev_root = vm._MEDIA_TEMP_ROOT
    prev_ctx = vm._MEDIA_CONTEXT
    ctx = _make_profile_ctx("low_memory_aggressive")
    vm.configure_media_runtime(data_dir=tmp_path / "data", context=ctx)
    try:
        async def fake_videoshot(bvid, cid, out_dir, **_kw):
            out_dir.mkdir(parents=True, exist_ok=True)
            p = out_dir / "videoshot_01.jpg"
            Image.new("RGB", (320, 180), (10, 10, 10)).save(p, format="JPEG")
            return [{"path": str(p), "timestamp_sec": 5.0, "label": "00:05", "source": "videoshot"}]

        async def _forbid_dash(*_a, **_kw):
            raise AssertionError("DASH download must be skipped when aggressive prefers videoshot+subtitle")

        monkeypatch.setattr(vm, "fetch_videoshot_frames", fake_videoshot)
        video_obj = NS(get_download_url=_forbid_dash)
        bundle = asyncio.run(
            vm.acquire_video_media(
                video_obj=video_obj,
                bvid="BV1agg",
                cid=12345,
                duration_sec=300,
                has_subtitle=True,
                subtitle_source="ai_subtitle",
                media_options={"visual_enabled": True, "danmaku_enabled": False},
            )
        )
        try:
            assert bundle["watch_mode"] == "videoshot_subtitle"
            assert bundle["has_visual"] is True
            assert bundle["frame_count"] == 1
        finally:
            vm.cleanup_media_bundle(bundle)
    finally:
        vm._MEDIA_TEMP_ROOT = prev_root
        vm._MEDIA_CONTEXT = prev_ctx


def test_ebook_disk_cache_and_profile_limits(tmp_path):
    prev_dir = ebook_mod._REMOTE_EBOOK_CACHE_DIR
    prev_ctx = ebook_mod._EBOOK_CONTEXT
    ctx = _make_profile_ctx("low_memory_aggressive")
    try:
        cache_dir = ebook_mod.configure_ebook_runtime(data_dir=tmp_path / "plugin_data", context=ctx)
        assert cache_dir == tmp_path / "plugin_data" / "cache" / "ebooks"
        # low_memory_aggressive: cache max = 1, max_mb = 5 MiB
        ebook_mod._write_remote_ebook_cache("k1", b"book-one", "text/plain")
        ebook_mod._write_remote_ebook_cache("k2", b"book-two", "text/plain")
        assert len(list(cache_dir.glob("*.bin"))) == 1
        with pytest.raises(ValueError, match="5MB"):
            ebook_mod.parse_local_ebook("big.txt", b"a" * (6 * 1024 * 1024))
    finally:
        ebook_mod._REMOTE_EBOOK_CACHE_DIR = prev_dir
        ebook_mod._EBOOK_CONTEXT = prev_ctx


def test_web_api_upload_ebook_checks_b64_len_before_decode_and_uses_run_io(tmp_path, monkeypatch):
    ctx = _make_profile_ctx("low_memory_aggressive")  # 5 MiB cap
    store = HorizonsStore(tmp_path / "h.sqlite3", context=ctx)
    try:
        refreshed = {"n": 0}

        async def _refresh():
            refreshed["n"] += 1

        eng = NS(
            context=ctx,
            store=store,
            clock=lambda: 1_800_000_000.0,
            refresh_projection=_refresh,
            parse_local_ebook_adapter=ebook_mod.parse_local_ebook,
        )
        plugin = NS(
            engine=eng,
            config={},
            context=ctx,
            closed=False,
            _resolve_active_identity=lambda _raw: (IDENT, "p_opt"),
        )
        api = web_api_mod.HorizonsWebAPI(plugin)
        monkeypatch.setattr(api, "_resolve_active_identity", lambda _raw="": (IDENT, "p_opt"))
        monkeypatch.setattr(
            api,
            "_authorize",
            lambda *, check_csrf=False: asyncio.sleep(0, result="human:admin"),
        )
        monkeypatch.setattr(web_api_mod, "parse_local_ebook", ebook_mod.parse_local_ebook)

        # 1) Oversized base64 rejected BEFORE b64decode is ever called
        huge_b64 = "A" * (8 * 1024 * 1024)
        monkeypatch.setattr(
            web_api_mod,
            "_read_json_body",
            lambda _max=0: asyncio.sleep(
                0,
                result={
                    "op": "upload_ebook",
                    "filename": "huge.txt",
                    "content_base64": huge_b64,
                },
            ),
        )
        orig_b64decode = base64.b64decode

        def _forbid_decode(*_a, **_kw):
            raise AssertionError("b64decode must not be called for oversized payload")

        monkeypatch.setattr(web_api_mod.base64, "b64decode", _forbid_decode)
        resp_big = asyncio.run(api.sources_post())
        assert resp_big["status_code"] == 400

        # 2) Valid small upload calls b64decode via run_io
        monkeypatch.setattr(web_api_mod.base64, "b64decode", orig_b64decode)
        run_io_calls: list[object] = []
        orig_run_io = web_api_mod.run_io

        async def _track_run_io(fn, *args, **kwargs):
            run_io_calls.append(fn)
            return await orig_run_io(fn, *args, **kwargs)

        monkeypatch.setattr(web_api_mod, "run_io", _track_run_io)
        small_txt = "第一章 测试\n\n这是一段用于验证异步解码的短电子书正文内容。" * 5
        monkeypatch.setattr(
            web_api_mod,
            "_read_json_body",
            lambda _max=0: asyncio.sleep(
                0,
                result={
                    "op": "upload_ebook",
                    "filename": "small.txt",
                    "content_base64": base64.b64encode(small_txt.encode("utf-8")).decode("ascii"),
                },
            ),
        )
        resp_ok = asyncio.run(api.sources_post())
        assert resp_ok["status_code"] == 200
        assert orig_b64decode in run_io_calls
    finally:
        store.close()


def test_store_pragmas_checkpoint_vacuum_and_mutation_seq(tmp_path):
    ctx = _make_profile_ctx("low_memory")
    store = HorizonsStore(tmp_path / "h.sqlite3", context=ctx)
    try:
        with store.lock:
            assert store.db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # INCREMENTAL
            assert store.db.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 200
            assert store.db.execute("PRAGMA journal_size_limit").fetchone()[0] == 2 * 1024 * 1024
            assert store.db.execute("PRAGMA temp_store").fetchone()[0] == 1  # FILE
            assert store.db.execute("PRAGMA mmap_size").fetchone()[0] == 0

        seq0 = store.mutation_seq
        # No-op settle_decay must NOT bump mutation_seq
        store.settle_decay(now=1_800_000_000.0)
        assert store.mutation_seq == seq0

        # Real write bumps mutation_seq
        store.bind_scope("qq:GroupMessage:100", IDENT, True, 1_800_000_000.0)
        assert store.mutation_seq == seq0 + 1

        res = store.checkpoint_and_vacuum()
        assert res["wal_checkpointed"] is True
        assert res["vacuumed"] is True
        assert res["auto_vacuum"] == 2
    finally:
        store.close()


def test_lifecycle_prune_physical_deletions_preserve_active_log_links(tmp_path):
    store = HorizonsStore(tmp_path / "gc.sqlite3")
    now = 1_800_000_000.0
    aged_70d = now - 70 * 86400
    aged_100d = now - 100 * 86400
    try:
        with store.lock, store.db:
            # Archived unpinned candidate > 90d -> deleted; pinned -> preserved
            store.db.execute(
                "INSERT INTO candidate_items(candidate_id,source_id,external_id,identity,source_kind,title,fingerprint,status,archived,pinned,created_at) "
                "VALUES('c_old','s1','e1',?,'feed','old','fp1','unread',1,0,?)",
                (IDENT, aged_100d),
            )
            store.db.execute(
                "INSERT INTO candidate_items(candidate_id,source_id,external_id,identity,source_kind,title,fingerprint,status,archived,pinned,created_at) "
                "VALUES('c_pin','s1','e2',?,'feed','pin','fp2','unread',1,1,?)",
                (IDENT, aged_100d),
            )
            # Terminal task+proposal > 60d NOT linked to active reading_log -> deleted
            store.db.execute(
                "INSERT INTO reading_tasks(task_id,identity,source_id,candidate_id,book_id,chapter_index,content_hash,status,proposal_id,evidence_json,created_at,updated_at) "
                "VALUES('t_del',?,'s1','c_x','',NULL,'h1','rejected','p_del','{}',?,?)",
                (IDENT, aged_70d, aged_70d),
            )
            store.db.execute(
                "INSERT INTO proposals(proposal_id,identity,candidate_id,book_id,chapter_index,source_kind,state,title,digest,reflection,task_id,created_at,updated_at) "
                "VALUES('p_del',?,'c_x','',NULL,'feed','rejected','old','d','r','t_del',?,?)",
                (IDENT, aged_70d, aged_70d),
            )
            # Terminal task+proposal > 60d LINKED to active reading_log -> preserved!
            store.db.execute(
                "INSERT INTO reading_tasks(task_id,identity,source_id,candidate_id,book_id,chapter_index,content_hash,status,proposal_id,evidence_json,created_at,updated_at) "
                "VALUES('t_keep',?,'s1','c_y','',NULL,'h2','approved','p_keep','{}',?,?)",
                (IDENT, aged_70d, aged_70d),
            )
            store.db.execute(
                "INSERT INTO proposals(proposal_id,identity,candidate_id,book_id,chapter_index,source_kind,state,title,digest,reflection,applied_log_id,task_id,created_at,updated_at) "
                "VALUES('p_keep',?,'c_y','',NULL,'feed','approved','keep','d','r','log_active','t_keep',?,?)",
                (IDENT, aged_70d, aged_70d),
            )
            store.db.execute(
                "INSERT INTO reading_logs(log_id,proposal_id,identity,source_kind,citation_json,digest,reflection,status,created_at) "
                "VALUES('log_active','p_keep',?,'feed','{}','d','r','active',?)",
                (IDENT, aged_70d),
            )
            # Aged review_audit > 60d -> deleted
            store.db.execute(
                "INSERT INTO review_audit(proposal_id,from_state,to_state,actor,reason,created_at) "
                "VALUES('p_del','pending_review','rejected','llm','no',?)",
                (aged_70d,),
            )

        removed = store.prune_terminal_records(now, retention_days=30, archive_after_days=90, terminal_delete_days=60)
        assert removed["archived_candidates"] == 1
        assert removed["reading_tasks"] == 1
        assert removed["proposals"] == 1
        assert removed["review_audit"] == 1

        assert store.db.execute("SELECT 1 FROM candidate_items WHERE candidate_id='c_old'").fetchone() is None
        assert store.db.execute("SELECT 1 FROM candidate_items WHERE candidate_id='c_pin'").fetchone() is not None
        assert store.db.execute("SELECT 1 FROM reading_tasks WHERE task_id='t_del'").fetchone() is None
        assert store.db.execute("SELECT 1 FROM proposals WHERE proposal_id='p_del'").fetchone() is None
        assert store.db.execute("SELECT 1 FROM reading_tasks WHERE task_id='t_keep'").fetchone() is not None
        assert store.db.execute("SELECT 1 FROM proposals WHERE proposal_id='p_keep'").fetchone() is not None
    finally:
        store.close()


def test_runtime_decouples_hourly_maintenance_and_skips_noop_refresh(tmp_path, monkeypatch):
    now = [1_800_000_000.0]
    store = HorizonsStore(tmp_path / "rt.sqlite3")
    try:
        counts = {"archive": 0, "prune": 0, "ckpt": 0, "temp": 0, "refresh": 0, "decay": 0}
        orig_archive = store.archive_candidates
        orig_prune = store.prune_terminal_records
        orig_ckpt = store.checkpoint_and_vacuum
        orig_decay = store.settle_decay

        def _arch(*a, **kw):
            counts["archive"] += 1
            return orig_archive(*a, **kw)

        def _prune(*a, **kw):
            counts["prune"] += 1
            return orig_prune(*a, **kw)

        def _ckpt(*a, **kw):
            counts["ckpt"] += 1
            return orig_ckpt(*a, **kw)

        def _decay(*a, **kw):
            counts["decay"] += 1
            return orig_decay(*a, **kw)

        monkeypatch.setattr(store, "archive_candidates", _arch)
        monkeypatch.setattr(store, "prune_terminal_records", _prune)
        monkeypatch.setattr(store, "checkpoint_and_vacuum", _ckpt)
        monkeypatch.setattr(store, "settle_decay", _decay)
        monkeypatch.setattr(runtime_mod, "cleanup_stale_temp_dirs", lambda **_kw: counts.__setitem__("temp", counts["temp"] + 1))

        class _DummyEngine(runtime_mod.EngineLifecycle):
            def __init__(self):
                self.store = store
                self.config = {"enabled": False}
                self.closed = False
                self.clock = lambda: now[0]
                self.half_life_days = 6.0
                self._last_heavy_maintenance_at = None
                self._last_projection_mutation_seq = store.mutation_seq
                self._last_decay_settled_at = now[0]

            async def refresh_projection(self):
                counts["refresh"] += 1
                self._last_projection_mutation_seq = store.mutation_seq
                self._last_decay_settled_at = now[0]

        eng = _DummyEngine()

        # 1st maintenance call runs heavy pass once
        asyncio.run(eng.process_maintenance())
        assert counts["archive"] == 1 and counts["prune"] == 1 and counts["ckpt"] == 1 and counts["temp"] == 1

        # 2nd maintenance call 60s later skips heavy pass!
        now[0] += 60.0
        asyncio.run(eng.process_maintenance())
        assert counts["archive"] == 1 and counts["prune"] == 1 and counts["ckpt"] == 1 and counts["temp"] == 1

        # No-op refresh check within 300s skips both decay and refresh!
        ran = asyncio.run(eng._maybe_refresh_projection_after_maintenance())
        assert ran is False
        assert counts["refresh"] == 0 and counts["decay"] == 0

        # After 300s with no expiring rows, settle_decay runs once, mutation_seq unchanged -> still skips refresh!
        now[0] += 305.0
        ran = asyncio.run(eng._maybe_refresh_projection_after_maintenance())
        assert ran is False
        assert counts["decay"] == 1 and counts["refresh"] == 0

        # After a real DB mutation, refresh_projection runs!
        store.bind_scope("qq:GroupMessage:m1", IDENT, True, now[0])
        ran = asyncio.run(eng._maybe_refresh_projection_after_maintenance())
        assert ran is True
        assert counts["refresh"] == 1

        # After 3600s, heavy maintenance runs again
        now[0] += 3700.0
        asyncio.run(eng.process_maintenance())
        assert counts["archive"] == 2 and counts["prune"] == 2 and counts["ckpt"] == 2 and counts["temp"] == 2
    finally:
        store.close()


def test_feedback_process_jobs_offloads_sqlite_via_run_io(tmp_path, monkeypatch):
    store = HorizonsStore(tmp_path / "fb.sqlite3")
    now = 1_800_000_000.0
    try:
        with store.lock, store.db:
            store.db.execute(
                "INSERT INTO feedback_jobs(event_id,identity,umo,member_id,payload_json,status,attempts,next_at,created_at) "
                "VALUES('ev1',?,'qq:GroupMessage:g1','u1',?,'pending',0,0,?)",
                (
                    IDENT,
                    '{"kind":"delivery","user_text":"聊聊古建筑斗拱","reply_text":"古建筑的斗拱真巧妙呀","question_ids":[],"log_ids":[],"topics":["古建筑"],"turn_id":"t1","proactive":false}',
                    now,
                ),
            )

        run_io_fns: list[str] = []
        orig_run_io = feedback_mod.run_io

        async def _track_run_io(fn, *args, **kwargs):
            run_io_fns.append(getattr(fn, "__name__", str(fn)))
            return await orig_run_io(fn, *args, **kwargs)

        monkeypatch.setattr(feedback_mod, "run_io", _track_run_io)

        async def _fake_call_model(*_a, **_kw):
            return {
                "topics": [{"topic": "古建筑", "user_quote": "聊聊古建筑斗拱", "reply_quote": "古建筑的斗拱真巧妙"}],
                "questions": [],
                "openings": [],
            }

        eng = NS(
            store=store,
            context=None,
            config={"review_provider": "rev1"},
            clock=lambda: now,
            half_life_days=6.0,
            call_model=_fake_call_model,
        )
        asyncio.run(feedback_mod.process_jobs(eng))
        assert "_load_due_feedback_jobs" in run_io_fns
        assert "_load_feedback_context" in run_io_fns
        assert "_finalize_feedback_job" in run_io_fns
        row = store.db.execute("SELECT status FROM feedback_jobs WHERE event_id='ev1'").fetchone()
        assert row["status"] == "done"
    finally:
        store.close()


def test_projection_precomputes_features_once_per_snapshot(monkeypatch):
    cache = proj_mod.ProjectionCache()
    now = 1_800_000_000.0
    snap = {
        "built_at": now,
        "summary": {},
        "by_identity": {
            IDENT: {
                "blocked": [],
                "corrections": [],
                "member_topics": [],
                "discussion_reflections": [],
                "interests": [
                    {
                        "interest_id": "i1",
                        "topic": "古建筑榫卯",
                        "summary": "研究宋代木构与斗拱结构",
                        "heat_score": 1.5,
                        "status": "active",
                        "expires_at": now + 86400,
                        "evidence_log_ids": [],
                    }
                ],
                "logs": [
                    {
                        "log_id": "l1",
                        "source_kind": "feed",
                        "citation": {"title": "应县木塔斗拱解析", "creator": "营造学社"},
                        "digest": "详解辽代木构建筑的受力特点",
                        "reflection": "斗拱叠涩太精妙了",
                        "created_at": now - 60,
                    },
                    {
                        "log_id": "l2",
                        "source_kind": "feed",
                        "citation": {"title": "手冲咖啡萃取曲线", "creator": "咖啡师"},
                        "digest": "讨论水温与研磨度对风味的影响",
                        "reflection": "浅烘豆子花香明显",
                        "created_at": now - 120,
                    },
                ],
                "questions": [
                    {
                        "question_id": "q1",
                        "log_id": "l1",
                        "topic": "古建筑榫卯",
                        "question": "你最喜欢哪种木构建筑？",
                        "status": "active",
                        "expires_at": now + 86400,
                        "discussed_umos": {},
                    }
                ],
                "books": [],
            }
        },
    }
    cache.bind_umo_identity("qq:GroupMessage:1", IDENT)
    cache.update_snapshot(snap)

    # Snapshot dicts themselves must stay JSON-clean (no frozensets injected into items)
    import json
    json.dumps(snap)

    call_count = {"n": 0}
    orig_extract = proj_mod._extract_features

    def _counted_extract(text: str):
        call_count["n"] += 1
        return orig_extract(text)

    monkeypatch.setattr(proj_mod, "_extract_features", _counted_extract)
    res = cache.query(
        enabled=True,
        ready=True,
        allowed_sessions=["qq:GroupMessage:1"],
        scope="qq:GroupMessage:1",
        query="最近看了应县木塔的斗拱结构",
        nearby_text="木构建筑真厉害",
        now=now,
    )
    assert res["matched"] is True
    # Only query + nearby_text called _extract_features (2 calls total), NOT per-interest/log/question!
    assert call_count["n"] == 2
