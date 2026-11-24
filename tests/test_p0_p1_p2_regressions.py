"""Regression tests for P0 (video risk control/subprocess/temp cleanup/shared cookie),
P1 (quota precheck/transient exemption/2h checkpoint/identity lock/timezone/run_io),
and P2 (native STT via series_llm_call/base64 to_thread/get_series_health).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from astrbot_plugin_dynamics_horizons import main as main_mod
from astrbot_plugin_dynamics_horizons.adapters import bilibili
from astrbot_plugin_dynamics_horizons.adapters import video_media as vm
from astrbot_plugin_dynamics_horizons.adapters.bilibili import (
    BilibiliRiskControlError,
    _classify_bili_exception,
)
from astrbot_plugin_dynamics_horizons.adapters.ssrf import (
    HttpResponse,
    set_dns_resolver_for_testing,
    set_http_transport_for_testing,
)
from astrbot_plugin_dynamics_horizons.core import engine as engine_mod
from astrbot_plugin_dynamics_horizons.core import feedback as feedback_mod
from astrbot_plugin_dynamics_horizons.core import runtime as runtime_mod
from astrbot_plugin_dynamics_horizons.core.contract import (
    PLUGIN_ID,
    make_identity,
    shanghai_day_bounds,
)
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.main import (
    _OPENAI_INLINE_VIDEO_MAX_BYTES,
    Main,
    _invoke_openai_gemini_native_video,
)
from astrbot_plugin_dynamics_horizons.series_panels import (
    ProviderBudgetBusyError,
    read_plugin_health,
)
from astrbot_plugin_dynamics_horizons.tests.test_core_engine import (
    FakeContext,
    FakeProvider,
    FakeWorldPlugin,
    _ts,
)
from astrbot_plugin_dynamics_horizons.tests.test_video_pipeline import (
    MP4_FIXTURE,
    FakeOpenAIGeminiProxyProvider,
)

IDENT_A = make_identity("alice", "alice")
IDENT_B = make_identity("bob", "bob")
UMO = "qq:GroupMessage:1001"


@pytest.fixture(autouse=True)
def _isolated_dns():
    set_dns_resolver_for_testing(lambda host: ["93.184.216.34"])
    set_http_transport_for_testing(None)
    yield
    set_http_transport_for_testing(None)
    set_dns_resolver_for_testing(None)


# ------------------------------------------------------------------
# P0 Regressions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_run_subprocess_kills_and_waits_on_cancel_and_timeout(monkeypatch):
    events: list[str] = []

    class FakeProc:
        def __init__(self, mode: str):
            self.mode = mode
            self.returncode = None

        async def communicate(self):
            if self.mode == "cancel":
                await asyncio.sleep(10)
            elif self.mode == "timeout":
                await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            events.append(f"kill:{self.mode}")
            if self.mode == "already_gone":
                raise ProcessLookupError()

        async def wait(self):
            events.append(f"wait:{self.mode}")
            self.returncode = -9
            return -9

    # 1) CancelledError kills and awaits proc.wait()
    proc_cancel = FakeProc("cancel")

    async def fake_exec_cancel(*args, **kwargs):
        return proc_cancel

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_cancel)
    task = asyncio.create_task(vm._run_subprocess(["ffmpeg"], timeout=5.0))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "kill:cancel" in events and "wait:cancel" in events

    # 2) TimeoutError kills and awaits proc.wait()
    events.clear()
    proc_timeout = FakeProc("timeout")

    async def fake_exec_timeout(*args, **kwargs):
        return proc_timeout

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_timeout)
    with pytest.raises(asyncio.TimeoutError):
        await vm._run_subprocess(["ffmpeg"], timeout=0.02)
    assert "kill:timeout" in events and "wait:timeout" in events

    # 3) ProcessLookupError during kill is handled cleanly
    events.clear()
    proc_gone = FakeProc("already_gone")

    async def _raise_cancel():
        raise asyncio.CancelledError()

    proc_gone.communicate = _raise_cancel  # type: ignore[method-assign]

    async def fake_exec_gone(*args, **kwargs):
        return proc_gone

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_gone)
    with pytest.raises(asyncio.CancelledError):
        await vm._run_subprocess(["ffmpeg"], timeout=5.0)
    assert "kill:already_gone" in events and "wait:already_gone" in events


@pytest.mark.asyncio
async def test_p0_cleanup_stale_temp_dirs_and_maintenance(tmp_path: Path, monkeypatch):
    temp_root = tmp_path / "sys_tmp"
    temp_root.mkdir()
    now = 2_000_000_000.0

    stale = temp_root / "horizons_bili_stale_1"
    fresh = temp_root / "horizons_bili_fresh_2"
    protected = temp_root / "horizons_bili_ckpt_3"
    unrelated = temp_root / "other_app_dir"
    for d in (stale, fresh, protected, unrelated):
        d.mkdir()
        (d / "f.bin").write_bytes(b"123")

    os.utime(stale, (now - 7200, now - 7200))
    os.utime(protected, (now - 7200, now - 7200))
    os.utime(unrelated, (now - 7200, now - 7200))
    os.utime(fresh, (now - 300, now - 300))

    removed = vm.cleanup_stale_temp_dirs(
        max_age_seconds=3600.0,
        now=now,
        temp_root=temp_root,
        protected_dirs=[protected],
    )
    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    assert protected.exists()
    assert unrelated.exists()

    # Also verify maintenance_loop's process_maintenance invokes cleanup_stale_temp_dirs
    called: list[tuple] = []
    monkeypatch.setattr(
        runtime_mod,
        "cleanup_stale_temp_dirs",
        lambda **kw: called.append((kw.get("protected_dirs"),)) or 0,
    )
    store = HorizonsStore(tmp_path / "maint.sqlite3")
    try:
        eng = HorizonsEngine(store, {"enabled": True}, lambda *a, **kw: {}, clock=lambda: now)
        await eng.process_maintenance()
        assert len(called) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_p0_disk_usage_check_rejects_low_free_space(tmp_path: Path, monkeypatch):
    http_called = []

    async def fake_transport(url: str, headers: dict[str, str], timeout: float):
        http_called.append(url)
        return HttpResponse(200, b"data", {"Content-Type": "video/mp4"})

    set_http_transport_for_testing(fake_transport)
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=10**9, used=9 * 10**8, free=100 * 1024 * 1024),
    )

    with pytest.raises(vm.MediaDownloadError, match="磁盘可用空间不足"):
        await vm.download_media_to_file(
            ["https://upos.bilivideo.com/test.mp4"],
            tmp_path / "out.mp4",
            max_bytes=1024 * 1024,
        )
    assert http_called == []


@pytest.mark.asyncio
async def test_p0_global_media_semaphore_serializes_downloads(tmp_path: Path):
    active = 0
    max_active = 0

    async def slow_transport(url: str, headers: dict[str, str], timeout: float):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return HttpResponse(200, MP4_FIXTURE, {"Content-Type": "video/mp4"})

    set_http_transport_for_testing(slow_transport)
    await asyncio.gather(
        vm.download_media_to_file(
            ["https://upos.bilivideo.com/a.mp4"],
            tmp_path / "a.mp4",
            max_bytes=1024 * 1024,
        ),
        vm.download_media_to_file(
            ["https://upos.bilivideo.com/b.mp4"],
            tmp_path / "b.mp4",
            max_bytes=1024 * 1024,
        ),
    )
    assert max_active == 1


@pytest.mark.asyncio
async def test_p0_http_412_aborts_cdn_retries_and_step2_triggers_risk_control(tmp_path: Path):
    seen_urls: list[str] = []

    async def transport_412(url: str, headers: dict[str, str], timeout: float):
        seen_urls.append(url)
        return HttpResponse(412, b"Precondition Failed", {"Content-Type": "text/plain"})

    set_http_transport_for_testing(transport_412)

    with pytest.raises(vm.MediaDownloadError) as exc_info:
        await vm.download_media_to_file(
            [
                "https://upos1.bilivideo.com/primary.m4s",
                "https://upos2.bilivideo.com/backup.m4s",
            ],
            tmp_path / "v.mp4",
            max_bytes=1024 * 1024,
        )
    assert exc_info.value.status_code == 412
    assert exc_info.value.is_transient is False
    # Must abort immediately on first 412 without trying backup CDN or attempt 2
    assert seen_urls == ["https://upos1.bilivideo.com/primary.m4s"]

    # Step 2 inside acquire_video_media must classify 412 -> BilibiliRiskControlError and clean temp dir
    seen_urls.clear()
    created_dirs: list[Path] = []
    orig_make = vm.make_temp_dir

    def _track_make(bvid: str = "") -> Path:
        d = orig_make(bvid)
        created_dirs.append(d)
        return d

    class FakeVideoObj:
        async def get_download_url(self, cid: int | None = None):
            return {
                "timelength": 60_000,
                "dash": {
                    "duration": 60,
                    "video": [
                        {
                            "id": 32,
                            "codecid": 7,
                            "bandwidth": 200_000,
                            "baseUrl": "https://upos1.bilivideo.com/v412.m4s",
                            "backupUrl": ["https://upos2.bilivideo.com/v412_bak.m4s"],
                        }
                    ],
                },
            }

    vm.make_temp_dir = _track_make
    try:
        with pytest.raises(BilibiliRiskControlError) as rc_info:
            await vm.acquire_video_media(
                video_obj=FakeVideoObj(),
                bvid="BV1rc412test",
                cid=999,
                duration_sec=60.0,
                has_subtitle=False,
                subtitle_source="",
                media_options={"visual_enabled": True},
                classify_error=lambda e: _classify_bili_exception(e, []),
            )
    finally:
        vm.make_temp_dir = orig_make

    assert rc_info.value.code == -412
    assert seen_urls == ["https://upos1.bilivideo.com/v412.m4s"]
    assert len(created_dirs) == 1 and not created_dirs[0].exists()


@pytest.mark.asyncio
async def test_p0_domain_cooldowns_sqlite_persistence_and_memory_fallback(tmp_path: Path):
    now_ref = [_ts(14, 0)]
    db_path = tmp_path / "cd.sqlite3"
    store1 = HorizonsStore(db_path)
    try:
        eng1 = HorizonsEngine(store1, {"enabled": True}, lambda *a, **kw: {}, clock=lambda: now_ref[0])
        await eng1._set_domain_cooldown("bilibili", now_ref[0] + 1800.0)
        assert eng1._domain_cooldowns["bilibili"] == now_ref[0] + 1800.0
    finally:
        store1.close()

    # Reopen store & engine: cooldown survives restart
    store2 = HorizonsStore(db_path)
    try:
        eng2 = HorizonsEngine(store2, {"enabled": True}, lambda *a, **kw: {}, clock=lambda: now_ref[0] + 100.0)
        assert eng2._domain_cooldowns["bilibili"] == now_ref[0] + 1800.0

        await eng2._clear_domain_cooldown("bilibili")
        assert "bilibili" not in eng2._domain_cooldowns
        assert store2.load_domain_cooldowns() == {}
    finally:
        store2.close()

    # Pure in-memory fallback when store has no DB persistence methods
    mock_store = SimpleNamespace()
    eng_mem = HorizonsEngine(mock_store, {"enabled": True}, lambda *a, **kw: {}, clock=lambda: now_ref[0])
    await eng_mem._set_domain_cooldown("bilibili", now_ref[0] + 600.0)
    assert eng_mem._domain_cooldowns["bilibili"] == now_ref[0] + 600.0
    await eng_mem._clear_domain_cooldown("bilibili")
    assert "bilibili" not in eng_mem._domain_cooldowns


@pytest.mark.asyncio
async def test_p0_poll_bilibili_source_passes_shared_cookie_readonly(tmp_path: Path, monkeypatch):
    seen_creds: list = []

    class FakeUser:
        def __init__(self, uid: int, credential=None):
            seen_creds.append(credential)

        async def get_videos(self, pn: int = 1, ps: int = 10):
            return {"list": {"vlist": []}}

        async def get_dynamics_new(self, offset: str = ""):
            return {"items": []}

    monkeypatch.setattr(bilibili.user, "User", FakeUser)
    store = HorizonsStore(tmp_path / "shared_cookie.sqlite3")
    try:
        src = store.create_source(
            identity=IDENT_A,
            source_kind="bilibili",
            name="UP源",
            config={"mode": "up", "uid": "946974", "include_videos": True, "include_dynamics": False},
            now=_ts(14, 0),
        )
        eng = HorizonsEngine(
            store,
            {
                "enabled": True,
                "bilibili_shared_cookie": "SESSDATA=shared_sess_val; buvid3=b3_ok; bili_jct=must_strip_csrf",
            },
            lambda *a, **kw: {},
            clock=lambda: _ts(14, 5),
        )
        res = await eng.poll_one_source(src["source_id"])
        assert res["polled"] is True
        assert len(seen_creds) == 1
        assert seen_creds[0] is not None
        assert seen_creds[0].sessdata == "shared_sess_val"
        assert seen_creds[0].buvid3 == "b3_ok"
        assert not getattr(seen_creds[0], "bili_jct", None)
    finally:
        store.close()


# ------------------------------------------------------------------
# P1 Regressions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_quota_precheck_skips_heavy_media_fetch(tmp_path: Path, monkeypatch):
    now_ts = _ts(14, 0)
    store = HorizonsStore(tmp_path / "precheck.sqlite3")
    fetch_calls: list[str] = []
    try:
        world = FakeWorldPlugin()
        ctx = FakeContext(
            stars=[{"name": "astrbot_plugin_dynamics_world", "activated": True, "star": world}]
        )
        eng = HorizonsEngine(
            store,
            {
                "enabled": True,
                "sessions": [UMO],
                "analysis_provider": "a",
                "review_provider": "r",
                "daily_digest_quota": 1,
                "daily_model_call_quota": 20,
            },
            lambda *a, **kw: {},
            context=ctx,
            clock=lambda: now_ts,
        )
        src = store.create_source(
            identity=IDENT_A,
            source_kind="bilibili",
            name="B站",
            config={"mode": "popular", "popular_kind": "hot"},
            now=now_ts,
        )
        cand = store.insert_candidates(
            [
                {
                    "external_id": "BV1quota0001",
                    "title": "配额预检视频",
                    "creator": "UP",
                    "bvid": "BV1quota0001",
                    "summary": "简介",
                    "full_text": "",
                    "content_ready": False,
                    "raw_url": "https://www.bilibili.com/video/BV1quota0001",
                }
            ],
            source_id=src["source_id"],
            source_kind="bilibili",
            identity=IDENT_A,
            now=now_ts,
        )[0]
        # Exhaust reading_attempts quota without creating any approved reading_log
        with store.lock, store.db:
            store.db.execute(
                "INSERT INTO reading_attempts VALUES('att_1', 'prev_task', ?, ?)",
                (IDENT_A, now_ts - 60),
            )

        async def _fake_fetch(*args, **kwargs):
            fetch_calls.append("called")
            return {}

        monkeypatch.setattr(eng, "fetch_item_content_adapter", _fake_fetch)
        res = await eng.run_digest_cycle(identity=IDENT_A)
        assert res["proposals_created"] == 0
        assert fetch_calls == []  # Pre-check prevented heavy download/transcode/STT!
        assert store.get_candidate(cand["candidate_id"])["status"] == "unread"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_p1_transient_failure_exempts_quota_and_resumes_from_2h_checkpoint(
    tmp_path: Path, monkeypatch
):
    now_ref = [_ts(14, 0)]
    store = HorizonsStore(tmp_path / "ckpt.sqlite3")
    try:
        world = FakeWorldPlugin()
        ctx = FakeContext(
            stars=[{"name": "astrbot_plugin_dynamics_world", "activated": True, "star": world}]
        )
        call_num = {"n": 0}

        async def flaky_model(role: str, instruction: str, payload: dict, media_payload=None):
            call_num["n"] += 1
            if call_num["n"] == 1:
                raise ProviderBudgetBusyError("busy queue", provider="v", purpose="reading")
            if role == "review_provider":
                return {"verdict": "approve", "reason": "ok", "risks": []}
            assert media_payload is not None
            assert len(media_payload.get("image_urls") or []) == 1
            assert "转写文本内容" in payload["untrusted_media"]
            return {
                "worth_recording": True,
                "drop_book": False,
                "digest": "基于缓存关键帧与音频转写完成的客观摘要。",
                "reflection": "重试续跑省下了重复下载转码。",
                "visual_observations": ["[00:10] 画面呈现清晰的实验演示步骤"],
                "staged_interest": {"topic": "科学实验", "summary": "喜欢清晰直观的演示"},
                "discussion_questions": [],
            }

        eng = HorizonsEngine(
            store,
            {
                "enabled": True,
                "sessions": [UMO],
                "analysis_provider": "a",
                "video_provider": "v",
                "review_provider": "r",
                "daily_digest_quota": 1,
                "daily_model_call_quota": 10,
            },
            flaky_model,
            context=ctx,
            clock=lambda: now_ref[0],
        )
        src = store.create_source(
            identity=IDENT_A,
            source_kind="bilibili",
            name="B站",
            config={"mode": "popular", "popular_kind": "hot"},
            now=now_ref[0],
        )
        store.insert_candidates(
            [
                {
                    "external_id": "BV1ckpt00001",
                    "title": "检查点续传视频",
                    "creator": "UP",
                    "bvid": "BV1ckpt00001",
                    "summary": "简介",
                    "full_text": "",
                    "content_ready": False,
                    "raw_url": "https://www.bilibili.com/video/BV1ckpt00001",
                }
            ],
            source_id=src["source_id"],
            source_kind="bilibili",
            identity=IDENT_A,
            now=now_ref[0],
        )

        # Create real temp dir under system tempdir so cleanup_temp_dir can remove it later
        td = Path(tempfile.mkdtemp(prefix="horizons_bili_ckpt_test_"))
        frame_file = td / "frame_01.jpg"
        frame_file.write_bytes(b"\xff\xd8\xff\xe0testjpeg")
        fetch_calls = 0

        async def fake_fetch(source, candidate, **kwargs):
            nonlocal fetch_calls
            fetch_calls += 1
            return {
                "full_text": "【音频转写】\n这是一段通过 STT 提取的转写文本内容。",
                "content_origin": "bilibili_vision_subtitle",
                "chapter_label": "",
                "total_chapters": None,
                "media_bundle": {
                    "watch_mode": "keyframes_subtitle",
                    "content_origin": "bilibili_vision_subtitle",
                    "has_visual": True,
                    "has_subtitle": False,
                    "has_audio_transcript": True,
                    "has_danmaku": False,
                    "duration_sec": 120,
                    "native_video_path": "",
                    "frames": [{"path": str(frame_file), "label": "00:10", "timestamp_sec": 10.0}],
                    "frame_paths": [str(frame_file)],
                    "videoshot_paths": [],
                    "frame_count": 1,
                    "temp_dir": str(td),
                    "cleanup": lambda: vm.cleanup_temp_dir(td),
                },
            }

        monkeypatch.setattr(eng, "fetch_item_content_adapter", fake_fetch)

        # 1st attempt: fails transiently with ProviderBudgetBusyError
        res1 = await eng.run_digest_cycle(identity=IDENT_A)
        assert res1["status"] == "error"
        assert fetch_calls == 1
        # Quota was refunded!
        attempts_left = store.db.execute("SELECT count(*) FROM reading_attempts").fetchone()[0]
        assert attempts_left == 0
        assert store.has_reading_quota(IDENT_A, 1, now_ref[0]) is True
        exempt_calls = store.db.execute(
            "SELECT count(*) FROM model_calls WHERE status='exempt'"
        ).fetchone()[0]
        assert exempt_calls == 1
        # Checkpoint & keyframe file retained for 2h retry
        assert len(eng._media_checkpoints) == 1
        assert frame_file.exists()

        # 2nd attempt 30 minutes later: skips fetch_item_content_adapter and resumes LLM directly!
        now_ref[0] += 1800.0
        res2 = await eng.run_digest_cycle(identity=IDENT_A)
        assert res2["status"] == "ok"
        assert res2["proposals_created"] == 1
        assert res2["approved"] == 1
        assert fetch_calls == 1  # Zero re-download/transcode/STT!
        assert len(eng._media_checkpoints) == 0
        assert not td.exists()  # Cleaned up after success!
    finally:
        store.close()


@pytest.mark.asyncio
async def test_p1_per_identity_locks_and_nonblocking_maintenance(tmp_path: Path, monkeypatch):
    now_ts = _ts(14, 0)
    store = HorizonsStore(tmp_path / "locks.sqlite3")
    try:
        world = FakeWorldPlugin()
        ctx = FakeContext(
            stars=[{"name": "astrbot_plugin_dynamics_world", "activated": True, "star": world}]
        )
        eng = HorizonsEngine(
            store,
            {
                "enabled": True,
                "sessions": [UMO],
                "analysis_provider": "a",
                "review_provider": "r",
            },
            lambda *a, **kw: {},
            context=ctx,
            clock=lambda: now_ts,
        )
        entered_a = asyncio.Event()
        release_a = asyncio.Event()
        completed: list[str] = []

        async def fake_digest(ident: str):
            if ident == IDENT_A:
                entered_a.set()
                await release_a.wait()
            completed.append(ident)
            return {"identity": ident, "status": "ok", "proposals_created": 0, "approved": 0}

        monkeypatch.setattr(eng, "_digest_for_identity", fake_digest)

        task_a = asyncio.create_task(eng.run_digest_cycle(identity=IDENT_A))
        await asyncio.wait_for(entered_a.wait(), timeout=2.0)

        # While IDENT_A holds its identity lock:
        # 1) IDENT_B runs immediately without blocking
        res_b = await asyncio.wait_for(eng.run_digest_cycle(identity=IDENT_B), timeout=2.0)
        assert res_b["status"] == "ok"
        assert IDENT_B in completed

        # 2) maintenance runs immediately without blocking on IDENT_A
        async with asyncio.timeout(2.0):
            async with eng._maintenance_lock:
                await eng.process_maintenance()

        # 3) A concurrent run for IDENT_A waits until the first IDENT_A releases
        task_a2 = asyncio.create_task(eng.run_digest_cycle(identity=IDENT_A))
        await asyncio.sleep(0.03)
        assert not task_a2.done()

        release_a.set()
        await asyncio.wait_for(asyncio.gather(task_a, task_a2), timeout=2.0)
        assert completed.count(IDENT_A) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_p1_timezone_passthrough_in_lifecycle_and_feedback(tmp_path: Path):
    ny_tz = ZoneInfo("America/New_York")
    # 2026-11-21 20:00 EST == 2026-11-22 09:00 Asia/Shanghai (different local days!)
    ts_evening_ny = datetime(2026, 11, 21, 20, 0, 0, tzinfo=ny_tz).timestamp()
    ts_morning_ny = datetime(2026, 11, 21, 9, 0, 0, tzinfo=ny_tz).timestamp()

    chat_owner = SimpleNamespace(
        get_series_shared_config=lambda: {"enabled": True, "rhythm_timezone": "America/New_York"}
    )
    ctx = FakeContext(
        stars=[{"name": "astrbot_plugin_chat_dynamics", "activated": True, "star": chat_owner}]
    )
    store = HorizonsStore(tmp_path / "tz.sqlite3")
    try:
        eng = HorizonsEngine(
            store,
            {"enabled": True, "daily_model_call_quota": 1, "daily_digest_quota": 1},
            lambda *a, **kw: {},
            context=ctx,
            clock=lambda: ts_evening_ny,
        )
        assert str(eng.timezone) == "America/New_York"

        # A call made at 09:00 NY is on the SAME NY day as 20:00 NY, even though Shanghai crossed midnight!
        store.reserve_call(IDENT_A, "analysis", 1, ts_morning_ny, tz=ny_tz)
        with pytest.raises(ValueError, match="daily_model_call_quota_reached"):
            store.reserve_call(IDENT_A, "analysis", 1, ts_evening_ny, tz=ny_tz)

        # diagnostics counts the morning NY call as today when tz=ny_tz
        diag_ny = store.diagnostics(IDENT_A, ts_evening_ny, tz=ny_tz)
        assert diag_ny["model_calls_today"] == 1
        diag_sh = store.diagnostics(IDENT_A, ts_evening_ny)
        assert diag_sh["model_calls_today"] == 0

        # claim_reading respects tz=ny_tz
        ev = {"content_hash": "h1"}
        tid1 = store.claim_reading(IDENT_A, {"candidate_id": "c1"}, ev, 1, ts_morning_ny, tz=ny_tz)
        assert tid1
        tid2 = store.claim_reading(
            IDENT_A, {"candidate_id": "c2"}, {"content_hash": "h2"}, 1, ts_evening_ny, tz=ny_tz
        )
        assert tid2 is None

        # feedback._retry_job schedules next_at using NY next day boundary, not Shanghai!
        with store.lock, store.db:
            store.db.execute(
                "INSERT INTO feedback_jobs(event_id,identity,umo,member_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                ("ev_tz", IDENT_A, UMO, "u1", json.dumps({}), ts_evening_ny),
            )
        row = dict(store.db.execute("SELECT * FROM feedback_jobs WHERE event_id='ev_tz'").fetchone())
        feedback_mod._retry_job(eng, row, ValueError("daily_model_call_quota_reached"), "model")
        updated = store.db.execute("SELECT next_at FROM feedback_jobs WHERE event_id='ev_tz'").fetchone()
        _, ny_next_day = shanghai_day_bounds(ts_evening_ny, tz=ny_tz)
        _, sh_next_day = shanghai_day_bounds(ts_evening_ny)
        assert ny_next_day != sh_next_day
        assert updated["next_at"] == ny_next_day + 60.0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_p1_sync_store_calls_offloaded_to_run_io(tmp_path: Path, monkeypatch):
    now_ts = _ts(14, 0)
    store = HorizonsStore(tmp_path / "runio.sqlite3")
    io_funcs: list[str] = []
    real_run_io = engine_mod.run_io

    async def spy_run_io(fn, *args, **kwargs):
        io_funcs.append(getattr(fn, "__name__", str(fn)))
        return await real_run_io(fn, *args, **kwargs)

    monkeypatch.setattr(engine_mod, "run_io", spy_run_io)
    monkeypatch.setattr(runtime_mod, "run_io", spy_run_io)
    try:
        world = FakeWorldPlugin()
        ctx = FakeContext(
            stars=[{"name": "astrbot_plugin_dynamics_world", "activated": True, "star": world}]
        )
        eng = HorizonsEngine(
            store,
            {
                "enabled": True,
                "sessions": [UMO],
                "proactive_enabled": True,
                "analysis_provider": "a",
                "review_provider": "r",
            },
            lambda *a, **kw: {},
            context=ctx,
            clock=lambda: now_ts,
        )
        store.bind_scope(UMO, IDENT_A, True, now_ts)
        store.create_book_with_chapters(
            identity=IDENT_A,
            title="测试书",
            creator="作者",
            chapters=[{"label": "第一章", "text": "正文"}],
            now=now_ts,
        )
        # 1) health_async offloads store.diagnostics
        io_funcs.clear()
        h = await eng.health_async(IDENT_A)
        assert "diagnostics" in h
        assert "diagnostics" in io_funcs

        # 2) effective_proactive_sessions_async offloads store.known_managed_scopes
        io_funcs.clear()
        sess = await eng.effective_proactive_sessions_async(IDENT_A)
        assert sess == [UMO]
        assert "known_managed_scopes" in io_funcs

        # 3) _digest_for_identity offloads store.chapter_available
        io_funcs.clear()
        monkeypatch.setattr(eng, "_stage2_deep_read_unit_with_review", lambda **kw: asyncio.sleep(0, result=(None, None)))
        await eng._digest_for_identity(IDENT_A)
        assert "chapter_available" in io_funcs

        # 4) run_digest_cycle error path offloads store.fail_running_tasks
        io_funcs.clear()

        async def boom(_ident: str):
            raise RuntimeError("boom")

        monkeypatch.setattr(eng, "_digest_for_identity", boom)
        res = await eng.run_digest_cycle(identity=IDENT_A)
        assert res["status"] == "error"
        assert "fail_running_tasks" in io_funcs
    finally:
        store.close()


# ------------------------------------------------------------------
# P2 Regressions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_native_stt_uses_series_llm_call_and_redacts_error_logs(
    tmp_path: Path, monkeypatch
):
    budget_calls: list[tuple[str, str, str]] = []

    class FakeBudget:
        supports_series_telemetry = True

        async def run(self, provider_id, purpose, invoke, *, plugin_id="", acquire_timeout=15.0):
            budget_calls.append((provider_id, purpose, plugin_id))
            return await invoke()

    class NativeSTTProvider:
        def __init__(self, fail: bool = False):
            self.fail = fail

        async def get_text(self, audio_path: str) -> str:
            if self.fail:
                raise RuntimeError(
                    "upstream failed Cookie: SESSDATA=secret_cookie_999 token=sk-secretapikey123456 https://leak.example.com/a"
                )
            return "  转写成功文本  "

    logged_warnings: list[str] = []

    def fake_warning(msg, *args):
        logged_warnings.append(msg % args if args else str(msg))

    monkeypatch.setattr(engine_mod.logger, "warning", fake_warning)
    store = HorizonsStore(tmp_path / "stt.sqlite3")
    try:
        stt_ok = NativeSTTProvider(fail=False)
        ctx = FakeContext(providers={"stt_native": stt_ok})
        ctx._chat_dynamics_provider_budget = FakeBudget()

        eng = HorizonsEngine(
            store,
            {"enabled": True, "video_audio_stt_provider": "stt_native"},
            lambda *a, **kw: {},
            context=ctx,
        )
        transcriber = eng._build_audio_transcriber()
        assert transcriber is not None
        out = await transcriber("/tmp/audio.m4a", 45)
        assert out == "转写成功文本"
        assert budget_calls == [("stt_native", "horizons_audio_stt", PLUGIN_ID)]

        # Error case: logs redacted warning without secrets/URLs
        stt_ok.fail = True
        out_err = await transcriber("/tmp/audio.m4a", 45)
        assert out_err == ""
        assert len(logged_warnings) == 1
        warn_text = logged_warnings[0]
        assert "secret_cookie_999" not in warn_text
        assert "sk-secretapikey123456" not in warn_text
        assert "https://leak.example.com" not in warn_text
    finally:
        store.close()


@pytest.mark.asyncio
async def test_p2_openai_inline_video_uses_to_thread_and_enforces_tighter_cap(monkeypatch):
    to_thread_calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *args, **kwargs):
        to_thread_calls.append(getattr(fn, "__name__", str(fn)))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(main_mod.asyncio, "to_thread", spy_to_thread)
    provider = FakeOpenAIGeminiProxyProvider(
        query_handler=lambda payloads: {"ok": True}
    )

    # Small video succeeds via asyncio.to_thread base64 encoding
    res = await _invoke_openai_gemini_native_video(
        provider,
        instruction="sys",
        payload="{}",
        video_bytes=MP4_FIXTURE,
        mime_type="video/mp4",
    )
    assert json.loads(res) == {"ok": True}
    assert "_encode_base64_ascii" in to_thread_calls

    # Oversized video (> _OPENAI_INLINE_VIDEO_MAX_BYTES) rejected before encoding
    oversized = b"\x00" * (_OPENAI_INLINE_VIDEO_MAX_BYTES + 1)
    with pytest.raises(ValueError, match="openai_inline_video_exceeds_limit"):
        await _invoke_openai_gemini_native_video(
            provider,
            instruction="sys",
            payload="{}",
            video_bytes=oversized,
            mime_type="video/mp4",
        )


def test_p2_get_series_health_contract_silent_lookup_and_readonly(tmp_path: Path):
    p_analysis = FakeProvider(lambda *_: {})
    p_review = FakeProvider(lambda *_: {})

    class SilentOnlyContext:
        def __init__(self, inst_map: dict):
            self.provider_manager = SimpleNamespace(inst_map=inst_map)

        def get_all_stars(self):
            return {}

        def get_provider_by_id(self, _pid: str):
            raise AssertionError("get_series_health must not call get_provider_by_id (cold-start WARN)")

    ctx = SilentOnlyContext({"prov_a": p_analysis, "prov_r": p_review})
    cfg = {
        "enabled": True,
        "analysis_provider": "prov_a",
        "review_provider": "prov_r",
        "review_mode": "llm",
    }
    plugin = Main(ctx, cfg)
    store = HorizonsStore(tmp_path / "health.sqlite3")
    now_ts = _ts(14, 0)
    try:
        plugin.engine = HorizonsEngine(store, cfg, lambda *a, **kw: {}, context=ctx, clock=lambda: now_ts)
        plugin.ready = True
        plugin.closed = False

        # Healthy case verified through series_panels.read_plugin_health contract
        h_ok = read_plugin_health(PLUGIN_ID, plugin)
        assert h_ok is not None
        assert h_ok["plugin_id"] == PLUGIN_ID
        assert h_ok["ready"] is True
        assert h_ok["degraded"] is False
        assert h_ok["issues"] == []
        roles = {p["role"]: p for p in h_ok["providers"]}
        assert roles["analysis"]["status"] == "ok"
        assert roles["review"]["status"] == "ok"
        assert roles["video"]["status"] == "ok"
        assert roles["stt"]["status"] == "unconfigured"
        assert isinstance(h_ok["metrics"], dict)

        # Degraded case: missing provider + active domain cooldown
        cfg["video_audio_stt_provider"] = "missing_stt"
        plugin.engine._domain_cooldowns["bilibili"] = now_ts + 900.0
        h_deg = read_plugin_health(PLUGIN_ID, plugin)
        assert h_deg is not None
        assert h_deg["ready"] is True
        assert h_deg["degraded"] is True
        assert any("stt_provider_not_found:missing_stt" in i for i in h_deg["issues"])
        assert any("domain_cooldown:bilibili" in i for i in h_deg["issues"])
        assert h_deg["metrics"]["active_domain_cooldowns"] == 1

        # Closed / not-ready case
        plugin.closed = True
        h_closed = read_plugin_health(PLUGIN_ID, plugin)
        assert h_closed is not None
        assert h_closed["ready"] is False
        assert h_closed["degraded"] is True
        assert "plugin_closed" in h_closed["issues"]
    finally:
        store.close()
