"""Tests for multimodal video pipeline, dual-channel invocation, review alignment, shared cookies, and degradation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_dynamics_horizons.core.contract import (
    make_identity,
    sanitize_readonly_bilibili_cookie,
)
from astrbot_plugin_dynamics_horizons.core.engine import (
    STAGE3_REVIEW_PROMPT,
    VIDEO_STAGE2_VISUAL_ADDENDUM,
)
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.main import Main, is_native_video_provider
from astrbot_plugin_dynamics_horizons.tests.test_core_engine import (
    FakeContext,
    FakeProvider,
    FakeWorldPlugin,
    _ts,
)

IDENT = make_identity("alice", "alice")
MP4_FIXTURE = b"\x00\x00\x00\x18ftypisom" + b"video-bytes-fixture"


class FakeGenAIProvider:
    """Mimics AstrBot ProviderGoogleGenAI (self.client is genai.Client.aio)."""

    provider_config = {"type": "googlegenai_chat_completion"}

    def __init__(self, *, generate_handler, text_chat_handler=None, model="gemini-3.8-flash-high"):
        self.model_name = model
        self._generate_handler = generate_handler
        self._text_chat_handler = text_chat_handler
        self.native_calls: list[dict] = []
        self.text_chat_calls: list[dict] = []

        async def _generate_content(*, model, contents, config=None):
            self.native_calls.append(
                {"model": model, "contents": contents, "config": config}
            )
            return self._generate_handler(model, contents, config)

        self.client = SimpleNamespace(
            models=SimpleNamespace(generate_content=_generate_content)
        )

    def get_model(self) -> str:
        return self.model_name

    async def text_chat(
        self,
        *,
        prompt: str,
        system_prompt: str = "",
        image_urls=None,
        contexts=None,
    ):
        self.text_chat_calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "image_urls": list(image_urls or []),
            }
        )
        if self._text_chat_handler is None:
            raise AssertionError("unexpected text_chat call")
        reply = self._text_chat_handler(prompt, system_prompt, list(image_urls or []))
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return SimpleNamespace(completion_text=text)


class FakeOpenAIGeminiProxyProvider:
    """Mimics ProviderOpenAIOfficial pointed at a Gemini model."""

    provider_config = {"type": "openai_chat_completion"}

    def __init__(self, *, query_handler, text_chat_handler=None, model="gemini-3.8-flash-high"):
        self.model_name = model
        self._query_handler = query_handler
        self._text_chat_handler = text_chat_handler
        self.query_calls: list[dict] = []
        self.text_chat_calls: list[dict] = []

    def get_model(self) -> str:
        return self.model_name

    async def _query(self, payloads, tools=None):
        self.query_calls.append(payloads)
        reply = self._query_handler(payloads)
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return SimpleNamespace(completion_text=text)

    async def text_chat(
        self,
        *,
        prompt: str,
        system_prompt: str = "",
        image_urls=None,
        contexts=None,
    ):
        self.text_chat_calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "image_urls": list(image_urls or []),
            }
        )
        reply = self._text_chat_handler(prompt, system_prompt, list(image_urls or []))
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return SimpleNamespace(completion_text=text)


class FakeVLMProvider:
    """Mimics MiniMax or generic OpenAI-compatible VLM (not native video)."""

    provider_config = {"type": "openai_chat_completion"}

    def __init__(self, handler, *, model="MiniMax-VL-01"):
        self.model_name = model
        self._handler = handler
        self.calls: list[dict] = []

    def get_model(self) -> str:
        return self.model_name

    async def text_chat(
        self,
        *,
        prompt: str,
        system_prompt: str = "",
        image_urls=None,
        contexts=None,
    ):
        self.calls.append(
            {
                "prompt": json.loads(prompt),
                "system_prompt": system_prompt,
                "image_urls": list(image_urls or []),
            }
        )
        reply = self._handler(system_prompt, json.loads(prompt), list(image_urls or []))
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return SimpleNamespace(completion_text=text)


class FakeSTTProvider:
    def __init__(self, transcript: str):
        self.transcript = transcript
        self.calls: list[str] = []

    async def get_text(self, audio_url: str) -> str:
        self.calls.append(audio_url)
        return self.transcript


def _genai_response(payload_dict: dict, *, thought_text: str = "internal reasoning"):
    parts = []
    if thought_text:
        parts.append(SimpleNamespace(text=thought_text, thought=True))
    parts.append(
        SimpleNamespace(
            text=json.dumps(payload_dict, ensure_ascii=False), thought=False
        )
    )
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))]
    )


async def _make_plugin(
    tmp_path: Path,
    *,
    config: dict | None = None,
    providers: dict | None = None,
) -> Main:
    world = FakeWorldPlugin()
    ctx = FakeContext(
        providers=providers or {},
        stars=[{"name": "astrbot_plugin_dynamics_world", "activated": True, "star": world}],
    )
    cfg = {
        "enabled": True,
        "sessions": ["qq:GroupMessage:1001"],
        "analysis_provider": "prov_analysis",
        "review_provider": "prov_review",
        "review_mode": "llm",
        "daily_digest_quota": 5,
        "daily_model_call_quota": 50,
        "model_timeout": 30,
        **(config or {}),
    }
    plugin = Main(ctx, cfg)
    plugin._data_dir_override = tmp_path / "horizons_data"
    await plugin.initialize()
    plugin.engine.clock = lambda: _ts(15, 0)
    return plugin


def _seed_bili_video_candidate(
    store: HorizonsStore,
    *,
    bvid: str = "BV1gcs469Ecu",
    title: str = "GTA6神犬开游艇",
    config: dict | None = None,
    name: str = "B站热门",
) -> tuple[dict, dict]:
    src = store.create_source(
        identity=IDENT,
        source_kind="bilibili",
        name=name,
        config=config or {"mode": "popular", "kind": "hot"},
        now=_ts(14, 0),
    )
    inserted = store.insert_candidates(
        [
            {
                "external_id": bvid,
                "title": title,
                "creator": "测试UP",
                "bvid": bvid,
                "summary": "简短标题简介",
                "full_text": "",
                "content_ready": False,
                "raw_url": f"https://www.bilibili.com/video/{bvid}",
            }
        ],
        source_id=src["source_id"],
        source_kind="bilibili",
        identity=IDENT,
        now=_ts(14, 0),
    )
    return src, inserted[0]


def test_conf_schema_has_video_fields_with_backward_compatible_defaults():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["video_provider"]["default"] == ""
    assert schema["video_provider"]["_special"] == "select_provider"
    assert schema["video_audio_stt_provider"]["default"] == ""
    assert schema["bilibili_shared_cookie"]["default"] == ""
    assert schema["video_native_max_duration_sec"]["default"] == 480
    assert schema["video_native_max_mb"]["default"] == 15
    assert schema["video_max_frames"]["default"] == 12
    assert schema["video_danmaku_enabled"]["default"] is False
    assert schema["video_danmaku_max_items"]["default"] == 15


def test_sanitize_readonly_bilibili_cookie_strips_bili_jct():
    raw = "SESSDATA=abc123; bili_jct=secret_csrf_999; buvid3=xyz789; CSRF_TOKEN=nope"
    cleaned = sanitize_readonly_bilibili_cookie(raw)
    assert "SESSDATA=abc123" in cleaned
    assert "buvid3=xyz789" in cleaned
    assert "bili_jct" not in cleaned
    assert "secret_csrf_999" not in cleaned
    assert "CSRF_TOKEN" not in cleaned
    assert sanitize_readonly_bilibili_cookie("******") == ""
    assert sanitize_readonly_bilibili_cookie("raw_sess_only") == "SESSDATA=raw_sess_only"


def test_is_native_video_provider_detection():
    p_genai = FakeGenAIProvider(generate_handler=lambda *_: None)
    p_oai_gem = FakeOpenAIGeminiProxyProvider(query_handler=lambda *_: None)
    p_vlm = FakeVLMProvider(lambda *_: {}, model="MiniMax-VL-01")
    p_plain = FakeProvider(lambda *_: {})

    assert is_native_video_provider(p_genai, "gemini/gemini-3.8-flash-high") is True
    assert is_native_video_provider(p_oai_gem, "cpa_chat/gemini-3.8-flash-high") is True
    assert is_native_video_provider(p_vlm, "minimax/MiniMax-VL-01") is False
    assert is_native_video_provider(p_plain, "deepseek/deepseek-v4.1-flash") is False


@pytest.mark.asyncio
async def test_channel_a_gemini_native_video_and_review_alignment_and_cleanup(
    tmp_path: Path, monkeypatch
):
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(MP4_FIXTURE)
    cleaned_up = []

    p_analysis = FakeProvider(lambda *_: {"verdict": "approve"})
    p_video = FakeGenAIProvider(
        generate_handler=lambda model, contents, config: _genai_response(
            {
                "worth_recording": True,
                "drop_book": False,
                "digest": "短片展示了金毛犬坐在游艇驾驶位转向的趣味画面，并配有顶部花字调侃。",
                "reflection": "居然真是一只金毛稳稳坐在驾驶台前，画面挺讨喜的。",
                "visual_observations": [
                    "[00:05] 一只金毛犬坐在白色游艇驾驶位，前爪搭在方向盘上",
                    "[00:12] 画面顶部花字写着「老船长上线」 https://spam.example.com/drop",
                ],
                "staged_interest": {"topic": "趣味萌宠短片", "summary": "对反差感强的宠物短片感兴趣"},
                "discussion_questions": [
                    {"topic": "趣味萌宠短片", "question": "大家见过哪些特别像模像样的宠物名场面？"}
                ],
            }
        )
    )
    p_review = FakeProvider(
        lambda sys_p, data: {
            "verdict": "approve",
            "reason": "结合 visual_observations 核验通过，无越界",
            "risks": [],
        }
    )

    plugin = await _make_plugin(
        tmp_path,
        config={
            "analysis_provider": "prov_analysis",
            "video_provider": "prov_gemini",
            "review_provider": "prov_review",
            "allow_same_provider_for_review": True,
            "merge_same_provider_review": True,
        },
        providers={
            "prov_analysis": p_analysis,
            "prov_gemini": p_video,
            "prov_review": p_review,
        },
    )
    try:
        _seed_bili_video_candidate(plugin.engine.store)

        async def fake_fetch(
            source,
            candidate,
            *,
            timeout=25.0,
            fallback_cookies=None,
            media_options=None,
            audio_transcriber=None,
        ):
            assert media_options["visual_enabled"] is True
            assert media_options["prefer_native_video"] is True
            return {
                "full_text": "",
                "content_origin": "bilibili_native_video",
                "chapter_label": "",
                "total_chapters": None,
                "media_bundle": {
                    "watch_mode": "native_video",
                    "content_origin": "bilibili_native_video",
                    "has_visual": True,
                    "has_subtitle": False,
                    "has_audio_transcript": False,
                    "has_danmaku": False,
                    "duration_sec": 32,
                    "native_video_path": str(video_file),
                    "native_video_size_bytes": len(MP4_FIXTURE),
                    "frames": [],
                    "frame_paths": [],
                    "videoshot_paths": [],
                    "frame_count": 0,
                    "temp_dir": str(tmp_path),
                    "cleanup": lambda: cleaned_up.append(True),
                    "ensure_fallback_frames": None,
                    "skip_reason": "",
                    "is_transient_error": False,
                    "is_permanent_unavailable": False,
                    "error_detail": "",
                },
            }

        monkeypatch.setattr(
            "astrbot_plugin_dynamics_horizons.core.engine.fetch_item_content",
            fake_fetch,
        )

        res = await plugin.engine.run_digest_cycle(identity=IDENT)
        assert res["status"] == "ok"
        assert res["proposals_created"] == 1
        assert res["approved"] == 1
        # Native video channel used on video_provider; cheap analysis_provider NOT called
        assert len(p_video.native_calls) == 1
        assert len(p_video.text_chat_calls) == 0
        assert len(p_analysis.calls) == 0
        # Independent review_provider WAS called with visual_observations in source_evidence
        assert len(p_review.calls) == 1
        rev_sys, rev_payload = p_review.calls[0]
        assert rev_sys.endswith(STAGE3_REVIEW_PROMPT)
        assert "core_profile" not in rev_payload["character"]
        assert "visual_observations" in rev_sys
        se = rev_payload["source_evidence"]
        assert se["watch_mode"] == "native_video"
        assert len(se["visual_observations"]) == 2
        assert "https://" not in se["visual_observations"][1]
        assert len(se.get("text", "")) <= 1600
        assert "raw_url" not in se
        # Reading log persists watch_mode, visual_observations, media_meta
        logs = plugin.engine.store.list_logs(identity=IDENT)["items"]
        assert len(logs) == 1
        assert logs[0]["watch_mode"] == "native_video"
        assert logs[0]["content_origin"] == "bilibili_native_video"
        assert len(logs[0]["visual_observations"]) == 2
        assert logs[0]["media_meta"]["has_visual"] is True
        assert logs[0]["media_meta"]["provider_id"] == "prov_gemini"
        # Temp media cleanup ran
        assert cleaned_up == [True]
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_channel_a_failure_falls_back_to_channel_b_keyframes(
    tmp_path: Path, monkeypatch
):
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(MP4_FIXTURE)
    frame_1 = tmp_path / "f1.jpg"
    frame_2 = tmp_path / "f2.jpg"
    frame_1.write_bytes(b"\xff\xd8\xff\xe0jpeg1")
    frame_2.write_bytes(b"\xff\xd8\xff\xe0jpeg2")
    fallback_extracted = []

    def fail_native(*_args):
        raise RuntimeError("HTTP 413 proxy rejected video/mp4")

    p_video = FakeGenAIProvider(
        generate_handler=fail_native,
        text_chat_handler=lambda prompt, sys_p, imgs: {
            "worth_recording": True,
            "digest": "从关键帧和字幕看出UP主展示了朋友家整洁的双屏桌面。",
            "reflection": "走线收得真干净，看着很舒服。",
            "visual_observations": ["[00:10] 双屏电脑桌面配机械键盘"],
            "staged_interest": {"topic": "桌搭布置", "summary": "关注简洁桌面走线"},
            "discussion_questions": [],
        },
    )
    p_review = FakeProvider(
        lambda *_: {"verdict": "approve", "reason": "关键帧与字幕依据充分", "risks": []}
    )

    plugin = await _make_plugin(
        tmp_path,
        config={"video_provider": "prov_gemini"},
        providers={"prov_analysis": p_review, "prov_gemini": p_video, "prov_review": p_review},
    )
    try:
        _seed_bili_video_candidate(plugin.engine.store, bvid="BV1gQsc69E6x", title="去朋友家玩电脑")

        async def ensure_fallback():
            fallback_extracted.append(True)
            return [
                {"path": str(frame_1), "timestamp_sec": 5.0, "label": "00:05", "source": "ffmpeg"},
                {"path": str(frame_2), "timestamp_sec": 15.0, "label": "00:15", "source": "ffmpeg"},
            ]

        async def fake_fetch(source, candidate, **kwargs):
            return {
                "full_text": "[00:05] 今天来朋友家看看他的新电脑",
                "content_origin": "bilibili_native_video",
                "chapter_label": "",
                "total_chapters": None,
                "media_bundle": {
                    "watch_mode": "native_video",
                    "content_origin": "bilibili_native_video",
                    "has_visual": True,
                    "has_subtitle": True,
                    "has_audio_transcript": False,
                    "has_danmaku": False,
                    "duration_sec": 95,
                    "native_video_path": str(video_file),
                    "frames": [],
                    "frame_paths": [],
                    "videoshot_paths": [],
                    "frame_count": 0,
                    "cleanup": lambda: None,
                    "ensure_fallback_frames": ensure_fallback,
                    "skip_reason": "",
                    "is_transient_error": False,
                    "is_permanent_unavailable": False,
                },
            }

        monkeypatch.setattr(
            "astrbot_plugin_dynamics_horizons.core.engine.fetch_item_content",
            fake_fetch,
        )

        res = await plugin.engine.run_digest_cycle(identity=IDENT)
        assert res["approved"] == 1
        assert len(p_video.native_calls) == 1
        assert fallback_extracted == [True]
        assert len(p_video.text_chat_calls) == 1
        assert p_video.text_chat_calls[0]["image_urls"] == [str(frame_1), str(frame_2)]
        assert VIDEO_STAGE2_VISUAL_ADDENDUM in p_video.text_chat_calls[0]["system_prompt"]

        logs = plugin.engine.store.list_logs(identity=IDENT)["items"]
        assert logs[0]["watch_mode"] == "keyframes_subtitle"
        assert logs[0]["content_origin"] == "bilibili_vision_subtitle"
        assert logs[0]["media_meta"]["frame_count"] == 2
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_channel_b_minimax_vlm_uses_image_urls_and_shared_cookies(
    tmp_path: Path, monkeypatch
):
    frame_1 = tmp_path / "k1.jpg"
    frame_1.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    captured_fetch_kwargs = {}

    p_vlm = FakeVLMProvider(
        lambda sys_p, data, imgs: {
            "worth_recording": True,
            "digest": "视频结合字幕与抽帧演示了咖啡手冲水温控制。",
            "reflection": "水温对风味影响讲得很直观。",
            "visual_observations": ["[01:20] 温度计显示 91 度热水匀速注入滤杯"],
            "staged_interest": {"topic": "手冲咖啡", "summary": "关注水温与萃取曲线"},
            "discussion_questions": [],
        },
        model="MiniMax-VL-01",
    )
    p_review = FakeProvider(
        lambda *_: {"verdict": "approve", "reason": "ok", "risks": []}
    )

    plugin = await _make_plugin(
        tmp_path,
        config={
            "analysis_provider": "prov_analysis",
            "video_provider": "prov_minimax",
            "review_provider": "prov_review",
            "bilibili_shared_cookie": "SESSDATA=shared_sess; bili_jct=MUST_DROP; buvid3=b3",
            "video_danmaku_enabled": True,
            "video_danmaku_max_items": 10,
            "video_audio_stt_provider": "prov_stt",
        },
        providers={
            "prov_analysis": p_review,
            "prov_minimax": p_vlm,
            "prov_review": p_review,
            "prov_stt": FakeSTTProvider("语音转写内容"),
        },
    )
    try:
        # Another enabled bilibili source for the same identity holds a read-only cookie
        plugin.engine.store.create_source(
            identity=IDENT,
            source_kind="bilibili",
            name="推荐流(带Cookie)",
            config={
                "mode": "recommend",
                "cookie": "SESSDATA=rcmd_sess; bili_jct=DROP_THIS_TOO",
            },
            now=_ts(13, 0),
        )
        # Current source (popular) has NO cookie of its own
        _seed_bili_video_candidate(plugin.engine.store, bvid="BV158sm6sEwd", title="手冲咖啡水温")

        async def fake_fetch(
            source,
            candidate,
            *,
            timeout=25.0,
            fallback_cookies=None,
            media_options=None,
            audio_transcriber=None,
        ):
            captured_fetch_kwargs["fallback_cookies"] = list(fallback_cookies or [])
            captured_fetch_kwargs["media_options"] = dict(media_options or {})
            captured_fetch_kwargs["stt_result"] = (
                await audio_transcriber("/tmp/a.m4a", 30)
                if callable(audio_transcriber)
                else ""
            )
            return {
                "full_text": "[01:20] 保持91度匀速注水",
                "content_origin": "bilibili_vision_subtitle",
                "chapter_label": "",
                "total_chapters": None,
                "media_bundle": {
                    "watch_mode": "keyframes_subtitle",
                    "content_origin": "bilibili_vision_subtitle",
                    "has_visual": True,
                    "has_subtitle": True,
                    "has_audio_transcript": False,
                    "has_danmaku": True,
                    "duration_sec": 620,
                    "native_video_path": "",
                    "frames": [{"path": str(frame_1), "timestamp_sec": 80.0, "label": "01:20", "source": "ffmpeg"}],
                    "frame_paths": [str(frame_1)],
                    "videoshot_paths": [],
                    "frame_count": 1,
                    "cleanup": lambda: None,
                    "ensure_fallback_frames": None,
                    "skip_reason": "",
                    "is_transient_error": False,
                    "is_permanent_unavailable": False,
                },
            }

        monkeypatch.setattr(
            "astrbot_plugin_dynamics_horizons.core.engine.fetch_item_content",
            fake_fetch,
        )

        res = await plugin.engine.run_digest_cycle(identity=IDENT)
        assert res["approved"] == 1
        assert captured_fetch_kwargs["media_options"]["prefer_native_video"] is False
        assert captured_fetch_kwargs["media_options"]["danmaku_enabled"] is True
        assert captured_fetch_kwargs["media_options"]["danmaku_max_items"] == 10
        assert captured_fetch_kwargs["stt_result"] == "语音转写内容"
        cookies = captured_fetch_kwargs["fallback_cookies"]
        assert len(cookies) == 2
        assert "SESSDATA=shared_sess" in cookies[0]
        assert "SESSDATA=rcmd_sess" in cookies[1]
        assert all("bili_jct" not in c for c in cookies)

        assert len(p_vlm.calls) == 1
        assert p_vlm.calls[0]["image_urls"] == [str(frame_1)]
        assert p_vlm.calls[0]["prompt"]["metadata"]["frame_timestamps"] == ["01:20"]

        health = plugin.engine.health(IDENT)
        assert health["video_provider"] == "prov_minimax"
        assert health["video_provider_fallback"] is False
        assert health["video_audio_stt_provider"] == "prov_stt"
        assert "shared_sess" not in json.dumps(health)
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_no_fake_reading_log_on_transient_defer_and_permanent_skip(
    tmp_path: Path, monkeypatch
):
    p_video = FakeVLMProvider(lambda *_: {"worth_recording": True, "digest": "x"})
    p_review = FakeProvider(lambda *_: {"verdict": "approve"})
    plugin = await _make_plugin(
        tmp_path,
        config={"video_provider": "prov_vlm"},
        providers={"prov_analysis": p_review, "prov_vlm": p_video, "prov_review": p_review},
    )
    try:
        # 1. Transient error -> defer (leave unread), NO LLM call, cleanup runs
        _src1, cand1 = _seed_bili_video_candidate(
            plugin.engine.store, bvid="BV1trans00001", title="临时风控视频"
        )
        cleaned_1 = []

        async def fetch_transient(source, candidate, **kwargs):
            return {
                "full_text": "仅简介",
                "content_origin": "bilibili_metadata_only",
                "media_bundle": {
                    "watch_mode": "unavailable",
                    "content_origin": "bilibili_metadata_only",
                    "has_visual": False,
                    "has_subtitle": False,
                    "has_audio_transcript": False,
                    "has_danmaku": False,
                    "is_transient_error": True,
                    "is_permanent_unavailable": False,
                    "skip_reason": "timeout",
                    "cleanup": lambda: cleaned_1.append(True),
                },
            }

        monkeypatch.setattr(
            "astrbot_plugin_dynamics_horizons.core.engine.fetch_item_content",
            fetch_transient,
        )
        res1 = await plugin.engine.run_digest_cycle(identity=IDENT)
        assert res1["proposals_created"] == 0
        assert len(p_video.calls) == 0
        assert cleaned_1 == [True]
        assert plugin.engine.store.get_candidate(cand1["candidate_id"])["status"] == "unread"

        # 2. Permanent unavailable (VIP-only, no subtitle/visual) -> skipped with skip_reason
        plugin.engine.store.set_candidate_status(cand1["candidate_id"], "skipped")
        _src2, cand2 = _seed_bili_video_candidate(
            plugin.engine.store, bvid="BV1vip0000002", title="大会员专享视频"
        )
        cleaned_2 = []

        async def fetch_permanent(source, candidate, **kwargs):
            return {
                "full_text": "",
                "content_origin": "bilibili_unavailable_permanent",
                "media_bundle": {
                    "watch_mode": "unavailable",
                    "content_origin": "bilibili_unavailable_permanent",
                    "has_visual": False,
                    "has_subtitle": False,
                    "has_audio_transcript": False,
                    "has_danmaku": False,
                    "is_transient_error": False,
                    "is_permanent_unavailable": True,
                    "skip_reason": "vip_only_no_subtitle",
                    "cleanup": lambda: cleaned_2.append(True),
                },
            }

        monkeypatch.setattr(
            "astrbot_plugin_dynamics_horizons.core.engine.fetch_item_content",
            fetch_permanent,
        )
        res2 = await plugin.engine.run_digest_cycle(identity=IDENT)
        assert res2["proposals_created"] == 0
        assert len(p_video.calls) == 0
        assert cleaned_2 == [True]
        c2 = plugin.engine.store.get_candidate(cand2["candidate_id"])
        assert c2["status"] == "skipped"
        assert c2["meta"]["skip_reason"] == "vip_only_no_subtitle"
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_openai_compatible_gemini_proxy_sends_video_data_uri(tmp_path: Path):
    video_file = tmp_path / "proxy.mp4"
    video_file.write_bytes(MP4_FIXTURE)
    p_proxy = FakeOpenAIGeminiProxyProvider(
        query_handler=lambda payloads: {"ok": True, "seen": "video"}
    )
    plugin = await _make_plugin(
        tmp_path,
        config={"video_provider": "cpa_gemini"},
        providers={"prov_analysis": p_proxy, "cpa_gemini": p_proxy, "prov_review": p_proxy},
    )
    try:
        out = await plugin._model(
            "video_provider",
            "sys",
            {"hello": "world"},
            media_payload={"native_video_path": str(video_file)},
        )
        assert out == {"ok": True, "seen": "video"}
        assert len(p_proxy.query_calls) == 1
        user_content = p_proxy.query_calls[0]["messages"][1]["content"]
        assert any(
            part.get("type") == "image_url"
            and part["image_url"]["url"].startswith("data:video/mp4;base64,")
            for part in user_content
        )
    finally:
        await plugin.terminate()


def test_legacy_sqlite_migration_adds_media_meta_json_safely(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE reading_logs (
            log_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL DEFAULT '',
            identity TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            citation_json TEXT NOT NULL,
            raw_url TEXT NOT NULL DEFAULT '',
            digest TEXT NOT NULL,
            reflection TEXT NOT NULL,
            fact_kind TEXT NOT NULL DEFAULT 'external_media',
            created_at REAL NOT NULL
        );
        INSERT INTO reading_logs VALUES (
            'log_old', 'prop_old', '["alice","alice"]', 'manual',
            '{"title":"旧笔记"}', '', '旧摘要', '旧随感', 'external_media', 1700000000.0
        );
        """
    )
    conn.commit()
    conn.close()

    store = HorizonsStore(db_path)
    try:
        logs = store.list_logs(identity=IDENT)["items"]
        assert len(logs) == 1
        assert logs[0]["log_id"] == "log_old"
        assert logs[0]["watch_mode"] == ""
        assert logs[0]["content_origin"] == ""
        assert logs[0]["visual_observations"] == []
        assert logs[0]["media_meta"] == {}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_video_provider_does_not_merge_self_review_when_different_from_review_provider(
    tmp_path: Path, monkeypatch
):
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    p_shared = FakeProvider(
        lambda sys_p, data: {
            "verdict": "approve",
            "reason": "独立审查通过",
            "risks": [],
        }
    )
    p_video = FakeVLMProvider(
        lambda sys_p, data, imgs: {
            "worth_recording": True,
            "digest": "短片展示了古典园林雨后飞檐细节。",
            "reflection": "青瓦白墙配细雨很有层次。",
            "visual_observations": ["[00:08] 雨滴顺着青瓦飞檐滑落"],
            "staged_interest": {"topic": "江南园林", "summary": "留意屋檐与雨景构图"},
            "discussion_questions": [],
            # Even if video model attempts to emit inline review, engine must ignore it
            # because video_provider != review_provider!
            "review": {"verdict": "approve", "reason": "不应被采纳", "risks": []},
        }
    )
    plugin = await _make_plugin(
        tmp_path,
        config={
            "analysis_provider": "prov_shared",
            "review_provider": "prov_shared",
            "video_provider": "prov_video",
            "allow_same_provider_for_review": True,
            "merge_same_provider_review": True,
        },
        providers={"prov_shared": p_shared, "prov_video": p_video},
    )
    try:
        _seed_bili_video_candidate(plugin.engine.store, bvid="BV1merge00001", title="雨后园林")

        async def fake_fetch(source, candidate, **kwargs):
            return {
                "full_text": "[00:08] 雨后园林的飞檐",
                "content_origin": "bilibili_vision_subtitle",
                "media_bundle": {
                    "watch_mode": "keyframes_subtitle",
                    "content_origin": "bilibili_vision_subtitle",
                    "has_visual": True,
                    "has_subtitle": True,
                    "has_audio_transcript": False,
                    "has_danmaku": False,
                    "duration_sec": 60,
                    "native_video_path": "",
                    "frames": [{"path": str(frame), "label": "00:08"}],
                    "frame_paths": [str(frame)],
                    "frame_count": 1,
                    "cleanup": lambda: None,
                    "is_transient_error": False,
                    "is_permanent_unavailable": False,
                },
            }

        monkeypatch.setattr(
            "astrbot_plugin_dynamics_horizons.core.engine.fetch_item_content",
            fake_fetch,
        )
        res = await plugin.engine.run_digest_cycle(identity=IDENT)
        assert res["approved"] == 1
        assert len(p_video.calls) == 1
        # Independent review call on prov_shared MUST have executed!
        assert len(p_shared.calls) == 1
        assert p_shared.calls[0][0].endswith(STAGE3_REVIEW_PROMPT)
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_cleanup_runs_on_model_exception_and_binding_revision_tracks_video_provider(
    tmp_path: Path, monkeypatch
):
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    cleaned = []

    def boom(*_args):
        raise RuntimeError("upstream_vlm_500")

    p_video = FakeVLMProvider(boom)
    p_review = FakeProvider(lambda *_: {"verdict": "approve"})
    plugin = await _make_plugin(
        tmp_path,
        config={"video_provider": "prov_v1"},
        providers={"prov_analysis": p_review, "prov_v1": p_video, "prov_review": p_review},
    )
    try:
        rev1 = plugin.engine.binding_revision()
        plugin.config["video_provider"] = "prov_v2"
        rev2 = plugin.engine.binding_revision()
        assert rev1 != rev2
        plugin.config["video_provider"] = "prov_v1"

        _seed_bili_video_candidate(plugin.engine.store, bvid="BV1err0000001", title="失败清理测试")

        async def fake_fetch(source, candidate, **kwargs):
            return {
                "full_text": "[00:02] 正常字幕文本",
                "content_origin": "bilibili_vision_subtitle",
                "media_bundle": {
                    "watch_mode": "keyframes_subtitle",
                    "content_origin": "bilibili_vision_subtitle",
                    "has_visual": True,
                    "has_subtitle": True,
                    "has_audio_transcript": False,
                    "has_danmaku": False,
                    "duration_sec": 30,
                    "native_video_path": "",
                    "frames": [{"path": str(frame), "label": "00:02"}],
                    "frame_paths": [str(frame)],
                    "frame_count": 1,
                    "cleanup": lambda: cleaned.append(True),
                    "is_transient_error": False,
                    "is_permanent_unavailable": False,
                },
            }

        monkeypatch.setattr(
            "astrbot_plugin_dynamics_horizons.core.engine.fetch_item_content",
            fake_fetch,
        )
        res = await plugin.engine.run_digest_cycle(identity=IDENT)
        assert res["status"] == "error"
        assert cleaned == [True]
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_web_api_exposes_video_fields_on_logs_reviews_and_overview(
    tmp_path: Path, monkeypatch
):
    plugin = await _make_plugin(
        tmp_path,
        config={"analysis_provider": "prov_a", "review_provider": "prov_r", "video_provider": ""},
        providers={
            "prov_a": FakeProvider(lambda *_: {}),
            "prov_r": FakeProvider(lambda *_: {}),
        },
    )
    try:
        prop = plugin.engine.store.create_proposal(
            identity=IDENT,
            source_kind="bilibili",
            title="视频测试",
            bvid="BV1web0000001",
            digest="核对过的视频摘要",
            reflection="角色随感",
            evidence={
                "watch_mode": "keyframes_subtitle",
                "content_origin": "bilibili_vision_subtitle",
                "visual_observations": ["[00:10] 画面出现猫咪"],
                "media_meta": {
                    "watch_mode": "keyframes_subtitle",
                    "content_origin": "bilibili_vision_subtitle",
                    "has_visual": True,
                    "has_subtitle": True,
                    "has_audio_transcript": False,
                    "has_danmaku": True,
                    "frame_count": 8,
                    "duration_sec": 120,
                    "provider_id": "prov_a",
                },
            },
            now=_ts(15, 0),
        )
        assert prop["watch_mode"] == "keyframes_subtitle"
        assert prop["visual_observations"] == ["[00:10] 画面出现猫咪"]
        assert prop["media_meta"]["has_danmaku"] is True

        health = plugin.engine.health(IDENT)
        assert health["video_provider"] == "prov_a"
        assert health["video_provider_explicit"] == ""
        assert health["video_provider_fallback"] is True

        committed = await plugin.engine.approve_proposal(prop["proposal_id"], reviewed_by="llm")
        log_item = committed["log"]
        assert log_item["watch_mode"] == "keyframes_subtitle"
        assert log_item["content_origin"] == "bilibili_vision_subtitle"
        assert log_item["visual_observations"] == ["[00:10] 画面出现猫咪"]
        assert log_item["media_meta"]["frame_count"] == 8
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_real_bilibili_dynamic_candidate_routes_to_analysis_provider_with_dynamic_watch_mode(
    tmp_path: Path, monkeypatch
):
    p_analysis = FakeProvider(
        lambda sys_p, data: {
            "worth_recording": True,
            "digest": "UP主在动态里预告了下周会更新茶具测评长图文。",
            "reflection": "提前记一下，等下周看看实测细节。",
            "staged_interest": {"topic": "茶具测评", "summary": "关注下周茶具实测动态"},
            "discussion_questions": [],
        }
    )
    p_video = FakeVLMProvider(
        lambda *_: (_ for _ in ()).throw(AssertionError("video_provider must not be called for dynamics"))
    )
    p_review = FakeProvider(
        lambda *_: {"verdict": "approve", "reason": "动态内容通过", "risks": []}
    )
    fetch_called = []

    async def should_not_fetch(*args, **kwargs):
        fetch_called.append(True)
        return {}

    monkeypatch.setattr(
        "astrbot_plugin_dynamics_horizons.core.engine.fetch_item_content",
        should_not_fetch,
    )

    plugin = await _make_plugin(
        tmp_path,
        config={
            "analysis_provider": "prov_analysis",
            "video_provider": "prov_video",
            "review_provider": "prov_review",
        },
        providers={
            "prov_analysis": p_analysis,
            "prov_video": p_video,
            "prov_review": p_review,
        },
    )
    try:
        src = plugin.engine.store.create_source(
            identity=IDENT,
            source_kind="bilibili",
            name="茶文化UP",
            config={"mode": "up", "uid": "12345"},
            now=_ts(14, 0),
        )
        plugin.engine.store.insert_candidates(
            [
                {
                    "external_id": "bili:dyn:9876543210",
                    "title": "下周更新预告",
                    "creator": "茶文化UP",
                    "bvid": "",
                    "summary": "下周更新茶具测评长图文",
                    "full_text": "下周会更新一期盖碗和紫砂壶对比测评长图文，欢迎留言想看的款式。",
                    "content_ready": True,
                    "meta": {"kind": "dynamic", "dynamic_id": "9876543210"},
                }
            ],
            source_id=src["source_id"],
            source_kind="bilibili",
            identity=IDENT,
            now=_ts(14, 0),
        )
        res = await plugin.engine.run_digest_cycle(identity=IDENT)
        assert res["approved"] == 1
        assert len(p_analysis.calls) == 1
        assert len(p_video.calls) == 0
        assert fetch_called == []
        logs = plugin.engine.store.list_logs(identity=IDENT)["items"]
        assert logs[0]["watch_mode"] == "dynamic"
        assert logs[0]["content_origin"] == "bilibili_dynamic"
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_ephemeral_visual_video_refetches_media_bundle_after_transient_llm_failure(
    tmp_path: Path, monkeypatch
):
    fetch_count = 0
    attempt = 0

    def vlm_handler(sys_p, data, imgs):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise RuntimeError("transient_vlm_503")
        assert len(imgs) == 1
        return {
            "worth_recording": True,
            "digest": "重试后成功重新抽帧并看懂画面里的木雕细节。",
            "reflection": "第二次终于看到了画面细节，刀工确实精细。",
            "visual_observations": ["[00:15] 匠人手持刻刀雕刻木质茶则"],
            "staged_interest": {"topic": "木雕手作", "summary": "对传统木雕工艺感兴趣"},
            "discussion_questions": [],
        }

    p_vlm = FakeVLMProvider(vlm_handler)
    p_review = FakeProvider(
        lambda *_: {"verdict": "approve", "reason": "通过", "risks": []}
    )
    plugin = await _make_plugin(
        tmp_path,
        config={"video_provider": "prov_vlm"},
        providers={"prov_analysis": p_review, "prov_vlm": p_vlm, "prov_review": p_review},
    )
    try:
        _seed_bili_video_candidate(plugin.engine.store, bvid="BV1retry00001", title="木雕手作")

        async def fake_fetch(source, candidate, **kwargs):
            nonlocal fetch_count
            fetch_count += 1
            frame_path = tmp_path / f"frame_attempt_{fetch_count}.jpg"
            frame_path.write_bytes(b"\xff\xd8\xff\xe0jpeg")
            return {
                "full_text": "【视频简介】木雕手作记录",
                "content_origin": "bilibili_vision_frames",
                "media_bundle": {
                    "watch_mode": "keyframes_only",
                    "content_origin": "bilibili_vision_frames",
                    "has_visual": True,
                    "has_subtitle": False,
                    "has_audio_transcript": False,
                    "has_danmaku": False,
                    "duration_sec": 45,
                    "native_video_path": "",
                    "frames": [{"path": str(frame_path), "label": "00:15"}],
                    "frame_paths": [str(frame_path)],
                    "frame_count": 1,
                    "cleanup": lambda p=frame_path: p.unlink(missing_ok=True),
                    "is_transient_error": False,
                    "is_permanent_unavailable": False,
                },
            }

        monkeypatch.setattr(
            "astrbot_plugin_dynamics_horizons.core.engine.fetch_item_content",
            fake_fetch,
        )

        # Attempt 1: fetch succeeds and persists content_ready=1 + content_origin='bilibili_vision_frames',
        # then VLM fails transiently and cleanup deletes frame_attempt_1.jpg
        res1 = await plugin.engine.run_digest_cycle(identity=IDENT)
        assert res1["status"] == "error"
        assert fetch_count == 1
        assert not (tmp_path / "frame_attempt_1.jpg").exists()

        # Attempt 2: MUST re-fetch fresh ephemeral visual media instead of skipping fetch!
        res2 = await plugin.engine.run_digest_cycle(identity=IDENT)
        assert res2["approved"] == 1
        assert fetch_count == 2
        assert len(p_vlm.calls) == 2
        assert p_vlm.calls[1]["image_urls"] == [str(tmp_path / "frame_attempt_2.jpg")]
        assert not (tmp_path / "frame_attempt_2.jpg").exists()
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_non_native_provider_with_native_video_path_enforces_degradation_guard(
    tmp_path: Path,
):
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(MP4_FIXTURE)
    p_vlm = FakeVLMProvider(
        lambda sys_p, data, imgs: {"ok": True, "imgs": len(imgs)},
        model="MiniMax-VL-01",
    )
    plugin = await _make_plugin(
        tmp_path,
        config={"video_provider": "prov_vlm"},
        providers={"prov_analysis": p_vlm, "prov_vlm": p_vlm, "prov_review": p_vlm},
    )
    try:
        async def empty_fallback():
            return []

        # Case A: no fallback frames AND no subtitle/audio -> MUST raise (no title/desc fabrication)
        media_no_sub = {
            "native_video_path": str(video_file),
            "image_urls": [],
            "ensure_fallback_frames": empty_fallback,
            "has_subtitle": False,
            "has_audio_transcript": False,
        }
        with pytest.raises(ValueError, match="native_video_unsupported_and_no_fallback_frames"):
            await plugin._model(
                "video_provider", "sys", {"k": "v"}, media_payload=media_no_sub
            )
        assert len(p_vlm.calls) == 0

        # Case B: no fallback frames BUT has_subtitle=True -> marks visual_dropped and uses subtitle text
        media_with_sub = {
            "native_video_path": str(video_file),
            "image_urls": [],
            "ensure_fallback_frames": empty_fallback,
            "has_subtitle": True,
            "has_audio_transcript": False,
        }
        out = await plugin._model(
            "video_provider", "sys", {"k": "v"}, media_payload=media_with_sub
        )
        assert out == {"ok": True, "imgs": 0}
        assert media_with_sub["fallback_used"] is True
        assert media_with_sub["visual_dropped"] is True
        assert media_with_sub["effective_frame_count"] == 0
        assert len(p_vlm.calls) == 1
    finally:
        await plugin.terminate()

