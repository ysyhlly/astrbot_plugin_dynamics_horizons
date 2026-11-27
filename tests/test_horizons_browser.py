from __future__ import annotations

import base64
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
PAGE_DIR = ROOT / "pages" / "horizons"


def _patch_local_chromium_if_needed() -> None:
    """Use verified host ms-playwright Chromium shell when Playwright wheel revision differs."""
    local_app = os.environ.get("LOCALAPPDATA")
    if not local_app:
        return
    ms_pw = Path(local_app) / "ms-playwright"
    if not ms_pw.is_dir():
        return
    candidates = sorted(
        ms_pw.glob(
            "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe"
        ),
        reverse=True,
    ) + sorted(
        ms_pw.glob("chromium-*/chrome-win64/chrome.exe"),
        reverse=True,
    )
    existing = next((p for p in candidates if p.is_file()), None)
    if existing is None:
        return
    browser_type_cls = playwright.BrowserType
    if getattr(browser_type_cls, "_horizons_patched", False):
        return
    orig_launch = browser_type_cls.launch

    def _launch(self, *args, **kwargs):
        if self.name == "chromium" and "executable_path" not in kwargs:
            try:
                return orig_launch(self, *args, **kwargs)
            except Exception:
                kwargs["executable_path"] = str(existing)
        return orig_launch(self, *args, **kwargs)

    browser_type_cls.launch = _launch
    browser_type_cls._horizons_patched = True


_patch_local_chromium_if_needed()


@pytest.fixture(scope="module")
def server():
    class QuietHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/plugin/page/bridge-sdk.js":
                # The browser fixture already installs the SDK double at document start.
                self.send_response(204)
                self.end_headers()
                return
            super().do_GET()

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(PAGE_DIR), **kwargs)

        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/index.html"
    httpd.shutdown()
    thread.join(2)


def make_mock_state() -> dict:
    id_a = '["rin","rin-main"]'
    id_b = '["alice","alice-fantasy"]'
    return {
        "presence": {
            "schema_version": 1,
            "current": "astrbot_plugin_dynamics_horizons",
            "legacy_conflict": False,
            "plugins": [
                {
                    "id": "astrbot_plugin_dynamics_horizons",
                    "label": "群间见闻",
                    "state": "ready",
                    "page": "horizons",
                    "role": "信息源与兴趣见闻",
                    "current": True,
                    "can_open": True,
                },
                {
                    "id": "astrbot_plugin_dynamics_world",
                    "label": "群间世界",
                    "state": "ready",
                    "page": "world",
                    "role": "角色与生活状态",
                    "current": False,
                    "can_open": True,
                },
                {
                    "id": "astrbot_plugin_chat_dynamics",
                    "label": "Chat Dynamics",
                    "state": "enabled",
                    "page": "app",
                    "role": "对话调度与回复",
                    "current": False,
                    "can_open": True,
                },
                {
                    "id": "astrbot_plugin_dynamics_learning",
                    "label": "群间学习",
                    "state": "ready",
                    "page": "review",
                    "role": "学习与审查",
                    "current": False,
                    "can_open": True,
                },
                {
                    "id": "astrbot_plugin_dynamics_games",
                    "label": "群间游戏",
                    "state": "ready",
                    "page": "games",
                    "role": "共读、约局、接龙、挑战与共创",
                    "current": False,
                    "can_open": True,
                },
                {
                    "id": "astrbot_plugin_dynamics_memory",
                    "label": "群间记忆",
                    "state": "missing",
                    "page": "dashboard",
                    "role": "记忆与证据",
                    "current": False,
                    "can_open": False,
                },
            ],
        },
        "overviews": {
            id_a: {
                "enabled": True,
                "ready": True,
                "personas": [
                    {
                        "identity": id_a,
                        "persona_id": "rin",
                        "label": "凛 · 现代日常实例",
                    },
                    {
                        "identity": id_b,
                        "persona_id": "alice",
                        "label": "爱丽丝 · 异世界书斋",
                    },
                ],
                "active_identity": id_a,
                "stats": {
                    "sources_total": 3,
                    "sources_enabled": 2,
                    "candidates_unread": 9,
                    "logs_total": 25,
                    "active_interests": 2,
                    "pending_questions": 1,
                    "pending_reviews": 1,
                    "daily_quota": 5,
                    "daily_used": 2,
                    "last_poll_at": 1763030000,
                    "last_digest_at": 1763031800,
                    "analysis_status": "就绪 (420ms)",
                    "review_status": "就绪 (310ms)",
                },
                "staged_interests": [
                    {
                        "interest_id": "int-1",
                        "topic": "古建筑木构榫卯",
                        "summary": "最近连看了两期斗拱拆解视频，对不用一颗铁钉的受力结构很着迷。",
                        "heat_score": 0.82,
                        "expires_at": 1763550000,
                        "status": "active",
                    }
                ],
                "discussion_questions": [
                    {
                        "question_id": "q-1",
                        "topic": "古建筑木构榫卯",
                        "question": "你们去老街古建的时候，会抬头看屋檐底下的斗拱吗？",
                        "created_at": 1763031800,
                        "expires_at": 1763291000,
                        "umo_states": [
                            {
                                "umo": "napcat:GroupMessage:10001",
                                "status": "discussed",
                                "discussed_at": 1763035000,
                            },
                            {
                                "umo": "napcat:GroupMessage:20002",
                                "status": "pending",
                                "discussed_at": None,
                            },
                        ],
                    }
                ],
                "books": [
                    {
                        "book_id": "book-1",
                        "title": "银河铁道之夜",
                        "creator": "宫泽贤治",
                        "source_kind": "ebook",
                        "chapter_cursor": 3,
                        "total_chapters": 9,
                        "status": "reading",
                    }
                ],
                "world_link": {
                    "connected": True,
                    "asleep": False,
                    "paused": False,
                    "summary": "书桌台灯亮着 · 刚读完第 3 章",
                },
            },
            id_b: {
                "enabled": True,
                "ready": True,
                "personas": [
                    {
                        "identity": id_a,
                        "persona_id": "rin",
                        "label": "凛 · 现代日常实例",
                    },
                    {
                        "identity": id_b,
                        "persona_id": "alice",
                        "label": "爱丽丝 · 异世界书斋",
                    },
                ],
                "active_identity": id_b,
                "stats": {
                    "sources_total": 1,
                    "sources_enabled": 1,
                    "candidates_unread": 2,
                    "logs_total": 4,
                    "active_interests": 0,
                    "pending_questions": 0,
                    "pending_reviews": 0,
                    "daily_quota": 4,
                    "daily_used": 0,
                    "last_poll_at": 1763029000,
                    "last_digest_at": 0,
                    "analysis_status": "就绪",
                    "review_status": "就绪",
                },
                "staged_interests": [],
                "discussion_questions": [],
                "books": [],
                "world_link": {
                    "connected": True,
                    "asleep": True,
                    "paused": False,
                    "summary": "角色正在熟睡，本轮自动阅读已静默跳过",
                },
            },
        },
        "sources": [
            {
                "source_id": "src-bili-1",
                "source_kind": "bilibili",
                "identity": id_a,
                "name": "营造学社科普关注",
                "enabled": True,
                "status": "ok",
                "last_poll_at": 1763030000,
                "cooldown_until": 0,
                "config": {"mode": "up", "uid": "946974", "cookie": "SESS***MASKED"},
            },
            {
                "source_id": "src-feed-1",
                "source_kind": "feed",
                "identity": id_a,
                "name": "独立文化周刊 RSS",
                "enabled": False,
                "status": "idle",
                "last_poll_at": 1763000000,
                "cooldown_until": 0,
                "config": {"url": "https://example.org/rss.xml"},
            },
        ],
        "logs": [
            {
                "log_id": "log-1",
                "source_kind": "bilibili",
                "citation": {
                    "title": "十分钟看懂佛光寺东大殿斗拱",
                    "creator": "古建观察员",
                    "bvid": "BV1xx411c7mD",
                    "chapter_label": "",
                },
                "raw_url": "https://www.bilibili.com/video/BV1xx411c7mD",
                "digest": "视频拆解了唐代殿堂型木构的铺作层与昂的杠杆传力关系。",
                "reflection": "原来屋檐挑出那么深不是为了好看，而是靠一层层木头互相咬住把重量传下来，挺妙的。",
                "created_at": 1763031800,
                "discussion_reflections": [
                    {
                        "reflection_id": "note-1",
                        "umo": "qq:GroupMessage:1001",
                        "member_id": "member-7",
                        "topic": "木构受力",
                        "member_quote": '"?><img src=x onerror="window.__pwned=true">我还发现，榫卯的朝向也会影响受力。',
                        "reflection": "听她这么一说，我想再看看斗拱和榫卯怎么一起分担重量。",
                        "created_at": 1763032000,
                    }
                ],
            },
            {
                "log_id": "log-2",
                "source_kind": "ebook",
                "citation": {
                    "title": "银河铁道之夜",
                    "creator": "宫泽贤治",
                    "bvid": "",
                    "chapter_label": "第 3 章 · 午后授课",
                },
                "raw_url": "javascript:alert('xss')",
                "digest": '老师在黑板上画出银河剖面图。<img src=x onerror="window.__pwned=true">',
                "reflection": "把星群比作发光的砂砾，读着让人安静下来。",
                "created_at": 1763028000,
            },
        ],
        "reviews": [
            {
                "proposal_id": "prop-1",
                "state": "pending_review",
                "source_kind": "bilibili",
                "title": "古建筑彩画矿物颜料考",
                "digest": "介绍青金石与石绿在古建彩画中的耐久性差异。",
                "reflection": "没想到几百年前的蓝色颜料要从那么远的地方运过来。",
                "staged_interest": {
                    "topic": "矿物颜料与古建彩画",
                    "summary": "好奇天然矿石磨粉调胶的工艺",
                },
                "questions": ["你们在博物馆见过还没褪色的石青石绿彩画吗？"],
                "review_verdict": "approve",
                "review_reason": "符合固定人格语气，保持外部媒介边界，无注入与外链风险。",
                "created_at": 1763032000,
            },
            {
                "proposal_id": "prop-2",
                "state": "rejected",
                "source_kind": "feed",
                "title": "疑似软文推广帖",
                "digest": "夹带营销报名引导。",
                "reflection": "看着不太对劲。",
                "staged_interest": None,
                "questions": [],
                "review_verdict": "reject",
                "review_reason": "检测到引流推广特征，独立审查模型自动拦截。",
                "created_at": 1763020000,
            },
        ],
        "blocked_topics": ["饭圈互撕", "剧透"],
    }


BRIDGE_INIT_SCRIPT = """
window.__calls = [];
window.__nav = [];
window.AstrBotPluginPage = {
  ready: async () => {},
  navigateToPlugin: async (...args) => { window.__nav.push(args); },
  apiGet: async (route, params = {}) => {
    window.__calls.push({ method: 'GET', route, params });
    const st = window.__mock;
    if (route === 'series_presence') return { status: 'ok', data: st.presence };
    if (route === 'overview') {
      const key = params.identity && st.overviews[params.identity] ? params.identity : Object.keys(st.overviews)[0];
      return { status: 'ok', data: st.overviews[key] };
    }
    if (route === 'sources') {
      const ov = st.overviews[params.identity] || Object.values(st.overviews)[0];
      return {
        status: 'ok',
        data: {
          sources: st.sources,
          books: ov.books,
          managed_umos: st.managed_umos || ['napcat:GroupMessage:10001'],
        },
      };
    }
    if (route === 'logs') {
      let filtered = params.source_kind
        ? st.logs.filter(x => x.source_kind === params.source_kind)
        : st.logs;
      if (params.query) {
        const q = String(params.query).toLowerCase();
        filtered = filtered.filter(x => JSON.stringify(x).toLowerCase().includes(q));
      }
      const page = Number(params.page) || 1;
      return { status: 'ok', data: { items: filtered, page, total: filtered.length, page_size: 1 } };
    }
    if (route === 'reviews') {
      let filtered = params.state
        ? (
            params.state === 'pending_review' || params.state === 'pending'
              ? st.reviews.filter(x => x.state === 'pending_review' || x.state === 'held')
              : st.reviews.filter(x => x.state === params.state)
          )
        : st.reviews;
      if (params.source_kind) {
        filtered = filtered.filter(x => x.source_kind === params.source_kind);
      }
      if (params.query) {
        const q = String(params.query).toLowerCase();
        filtered = filtered.filter(x => JSON.stringify(x).toLowerCase().includes(q));
      }
      return {
        status: 'ok',
        data: {
          items: filtered,
          blocked_topics: st.blocked_topics,
          corrections: st.corrections || [],
          corrections_count: (st.corrections || []).length,
          page: Number(params.page) || 1,
          total: filtered.length,
        },
      };
    }
    return { status: 'ok', data: {} };
  },
  apiPost: async (route, body = {}) => {
    window.__calls.push({ method: 'POST', route, body });
    const st = window.__mock;
    const ov = st.overviews[body.identity] || Object.values(st.overviews)[0];
    if (route === 'sources') {
      if (body.op === 'create') {
        st.sources.push({
          source_id: 'src-new-' + (st.sources.length + 1),
          source_kind: body.source_kind,
          identity: body.identity,
          name: body.name,
          enabled: body.enabled !== false,
          status: 'ok',
          last_poll_at: 0,
          cooldown_until: 0,
          config: body.config || {},
        });
        if (body.source_kind === 'ebook') {
          ov.books.push({
            book_id: 'book-new-' + (ov.books.length + 1),
            title: body.name,
            creator: (body.config && body.config.creator) || '公版作者',
            source_kind: 'ebook',
            chapter_cursor: 0,
            total_chapters: 6,
            status: 'reading',
          });
        }
        return { status: 'ok', data: { created: true } };
      }
      if (body.op === 'search_public_books') {
        return {
          status: 'ok',
          data: {
            books: [
              {
                id: 'ws-101',
                title: '朝花夕拾',
                creator: '鲁迅',
                language: body.language || 'zh',
                page: '朝花夕拾',
                summary: '鲁迅散文集公版收录版本。',
              }
            ],
          },
        };
      }
      if (body.op === 'upload_ebook') {
        ov.books.push({
          book_id: 'book-upload-1',
          title: body.filename.replace(/\\.(epub|txt)$/i, ''),
          creator: '本地上传',
          source_kind: 'ebook',
          chapter_cursor: 1,
          total_chapters: 4,
          status: 'reading',
        });
        return { status: 'ok', data: { book_id: 'book-upload-1' } };
      }
      if (body.op === 'update_book_status') {
        const bk = ov.books.find(b => b.book_id === body.book_id);
        if (bk) {
          if (body.status) bk.status = body.status;
          if (typeof body.chapter_cursor === 'number') bk.chapter_cursor = body.chapter_cursor;
        }
        return { status: 'ok', data: { updated: true } };
      }
      if (body.op === 'delete_book') {
        ov.books = ov.books.filter(b => b.book_id !== body.book_id);
        return { status: 'ok', data: { deleted: true } };
      }
      if (body.op === 'start_co_reading') {
        const bk = ov.books.find(b => b.book_id === body.book_id);
        if (bk) {
          bk.group_progress = [{ umo: body.umo, group_cursor: 0, co_read_completed: false }];
        }
        return { status: 'ok', data: { ok: true, activity_id: 'act-read-1', message: '已发起群共读活动！' } };
      }
      if (body.op === 'test_source') {
        if (st.test_source_response) return { status: 'ok', data: st.test_source_response };
        return {
          status: 'ok',
          data: {
            ok: true,
            count: 1,
            preview_items: [{ title: '试抓样本条目', creator: '测试作者' }],
          },
        };
      }
      if (body.op === 'update') {
        const s = st.sources.find(x => x.source_id === body.source_id);
        if (s) {
          s.name = body.name || s.name;
          s.enabled = body.enabled !== false;
          s.config = body.config || s.config;
        }
        return { status: 'ok', data: { updated: true } };
      }
      if (body.op === 'toggle') {
        const s = st.sources.find(x => x.source_id === body.source_id);
        if (s) s.enabled = !s.enabled;
        return { status: 'ok', data: { toggled: true } };
      }
      if (body.op === 'delete') {
        st.sources = st.sources.filter(x => x.source_id !== body.source_id);
        return { status: 'ok', data: { deleted: true } };
      }
      return { status: 'ok', data: { ok: true } };
    }
    if (route === 'reviews') {
      if (body.op === 'approve' || body.op === 'reject') {
        const p = st.reviews.find(x => x.proposal_id === body.proposal_id);
        if (p) p.state = body.op === 'approve' ? 'approved' : 'rejected';
      } else if (body.op === 'edit_proposal') {
        const p = st.reviews.find(x => x.proposal_id === body.proposal_id);
        if (p) {
          if (body.digest) p.digest = body.digest;
          if (body.reflection) p.reflection = body.reflection;
          if (body.staged_interest) p.staged_interest = body.staged_interest;
          if (body.questions) p.questions = body.questions;
        }
      } else if (body.op === 'batch_approve' || body.op === 'batch_reject') {
        const targetState = body.op === 'batch_approve' ? 'approved' : 'rejected';
        const ids = (body.items || []).map(i => i.proposal_id);
        for (const pid of ids) {
          const p = st.reviews.find(x => x.proposal_id === pid);
          if (p) p.state = targetState;
        }
        return { status: 'ok', data: { processed: ids.length, succeeded: ids.length, failed: 0 } };
      } else if (body.op === 'resolve_correction') {
        st.corrections = (st.corrections || []).filter(c => c.correction_id !== body.correction_id);
      } else if (body.op === 'apply_preset') {
        ov.runtime_config = Object.assign(ov.runtime_config || {}, { daily_digest_quota: 2, daily_model_call_quota: 20, review_mode: 'llm_then_human', interest_half_life_days: 8 });
      } else if (body.op === 'cancel_intent') {
        const it = (ov.outreach_diagnostics?.horizon_intents || []).find(x => x.intent_id === body.intent_id);
        if (it) it.status = 'cancelled';
      } else if (body.op === 'restore_member_outreach') {
        if (ov.outreach_diagnostics) {
          ov.outreach_diagnostics.opted_out_members = (ov.outreach_diagnostics.opted_out_members || []).filter(m => m.member_id !== body.member_id);
        }
      } else if (body.op === 'block_topic') {
        if (body.topic && !st.blocked_topics.includes(body.topic)) st.blocked_topics.push(body.topic);
      } else if (body.op === 'unblock_topic') {
        st.blocked_topics = st.blocked_topics.filter(t => t !== body.topic);
      } else if (body.op === 'extend_interest') {
        const it = ov.staged_interests.find(i => i.interest_id === body.interest_id);
        if (it) it.heat_score = Math.min(2.0, Number(it.heat_score || 0.85) + 0.53);
      } else if (body.op === 'revoke_interest') {
        ov.staged_interests = ov.staged_interests.filter(i => i.interest_id !== body.interest_id);
      } else if (body.op === 'archive_question') {
        ov.discussion_questions = ov.discussion_questions.filter(q => q.question_id !== body.question_id);
      }
      return { status: 'ok', data: { ok: true } };
    }
    return { status: 'ok', data: {} };
  },
};
"""


def attach_error_guards(page) -> tuple[list[str], list[str]]:
    page_errors: list[str] = []
    console_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))
    page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )
    return page_errors, console_errors


@pytest.mark.parametrize("width", [1280, 390])
def test_series_presence_and_safe_navigation(server, width):
    mock = make_mock_state()
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 1000})
        page_errors, console_errors = attach_error_guards(page)
        page.add_init_script(
            f"window.__mock = {json.dumps(mock, ensure_ascii=False)};\n"
            + BRIDGE_INIT_SCRIPT
        )
        page.goto(server)

        panel = page.locator("#dynamics-series-panel")
        panel.get_by_role("button").filter(has_text="返回群间小屋").wait_for()
        assert panel.get_by_role("button").filter(has_text="返回群间小屋").is_enabled()

        def ensure_matrix_open():
            trigger = panel.locator(".sp-matrix-trigger")
            if trigger.count() > 0:
                popover = page.locator("#sp-matrix-popover")
                if popover.count() == 0 or not popover.is_visible():
                    trigger.first.click()
                    popover.wait_for(state="visible")
                return popover
            return panel

        target_box = ensure_matrix_open()
        target_box.get_by_role("button").filter(has_text="对话主控").wait_for()
        assert target_box.get_by_role("button").filter(has_text="群间见闻").is_disabled()
        assert target_box.get_by_role("button").filter(has_text="群间记忆").is_disabled()
        assert target_box.get_by_role("button").filter(has_text="群间游戏").is_enabled()
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )

        page.evaluate("window.ChatDynamicsBeforeNavigate = async () => false")
        ensure_matrix_open().get_by_role("button").filter(has_text="对话主控").click()
        assert page.evaluate("window.__nav.length") == 0

        page.evaluate("window.ChatDynamicsBeforeNavigate = async () => true")
        ensure_matrix_open().get_by_role("button").filter(has_text="对话主控").click()
        assert page.evaluate("window.__nav[0].slice(0, 2)") == [
            "astrbot_plugin_chat_dynamics",
            "app",
        ]

        page.evaluate("delete window.AstrBotPluginPage.navigateToPlugin")
        ensure_matrix_open().get_by_role("button").filter(has_text="对话主控").click()
        assert "请更新入口和目标群间插件后重试" in panel.inner_text()
        page.evaluate("window.DynamicsWorkspace={open:async(id,view)=>window.__nav.push([id,view])}")
        ensure_matrix_open().get_by_role("button").filter(has_text="对话主控").click()
        assert page.evaluate("window.__nav[1]") == ["astrbot_plugin_chat_dynamics", "app"]
        assert not page_errors and not console_errors
        browser.close()


@pytest.mark.parametrize(
    "width,theme",
    [
        (1280, "light"),
        (1280, "dark"),
        (768, "light"),
        (390, "light"),
        (390, "dark"),
        (320, "light"),
    ],
)
def test_theme_language_and_zero_overflow_all_tabs(server, width, theme):
    mock = make_mock_state()
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": width, "height": 1000}, color_scheme=theme
        )
        context.add_init_script(
            f"window.__mock = {json.dumps(mock, ensure_ascii=False)};\n"
            + BRIDGE_INIT_SCRIPT
        )
        page = context.new_page()
        page_errors, console_errors = attach_error_guards(page)

        page.goto(server + ("?ui=night" if theme == "dark" else "?ui=day"))
        page.get_by_text("古建筑木构榫卯").first.wait_for()

        expected_bgs = (
            {"rgb(11, 20, 25)", "rgb(12, 20, 18)"}
            if theme == "dark"
            else {"rgb(245, 246, 244)", "rgb(243, 244, 239)"}
        )
        assert (
            page.evaluate("getComputedStyle(document.body).backgroundColor")
            in expected_bgs
        )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )

        # Toggle theme and verify persistence
        page.locator("#theme-toggle").click()
        toggled_theme = "day" if theme == "dark" else "night"
        assert (
            page.evaluate("localStorage.getItem('chat_dynamics_ui')") == toggled_theme
        )
        assert page.locator("html").get_attribute("data-ui-theme") == toggled_theme

        # Switch every tab and check zero horizontal overflow at this viewport
        for tab_key in ("sources", "logs", "reviews", "overview"):
            page.locator(f'[data-tab="{tab_key}"]').click()
            page.locator(f"#panel-{tab_key}").wait_for(state="visible")
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            ), f"Overflow detected on tab {tab_key} at width={width}"

        # Switch UI language via series companion bar without altering record body text
        page.locator("select[data-series-language]").select_option("en")
        playwright.expect(page.locator("#tab-overview")).to_contain_text(
            "Overview & Interests"
        )
        assert "古建筑木构榫卯" in page.locator("#interests-list").inner_text()

        assert not page_errors and not console_errors
        browser.close()


def test_overview_persona_switch_interests_and_per_umo_questions(server):
    mock = make_mock_state()
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page_errors, console_errors = attach_error_guards(page)
        page.add_init_script(
            f"window.__mock = {json.dumps(mock, ensure_ascii=False)};\n"
            + BRIDGE_INIT_SCRIPT
        )
        page.goto(server)

        page.get_by_text("古建筑木构榫卯").first.wait_for()
        assert "清醒可读" in page.locator("#world-link-badge").inner_text()
        assert "82%" in page.locator("#interests-list").inner_text()

        # Verify per-UMO independent consumption pills
        q_text = page.locator("#questions-list").inner_text()
        assert "群聊 · 10001 · 已聊核销" in q_text
        assert "群聊 · 20002 · 待聊" in q_text

        # Trigger manual digest, extend interest, revoke interest, archive question
        page.locator("#btn-trigger-digest").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'reviews' && c.body?.op === 'trigger_digest')"
        )

        page.locator("#interests-list").get_by_role("button", name="延期").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'reviews' && c.body?.op === 'extend_interest')"
        )
        page.locator("#interests-list").get_by_text("100%").wait_for()
        assert "100%" in page.locator("#interests-list").inner_text()

        page.locator("#interests-list").get_by_role("button", name="撤回").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'reviews' && c.body?.op === 'revoke_interest')"
        )
        assert "暂无活跃暂存兴趣" in page.locator("#interests-list").inner_text()

        page.locator("#questions-list").get_by_role("button", name="归档").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'reviews' && c.body?.op === 'archive_question')"
        )
        assert "没有待讨论问题" in page.locator("#questions-list").inner_text()

        # Switch character persona to asleep fantasy character
        page.locator("#persona-select").select_option('["alice","alice-fantasy"]')
        page.get_by_text("熟睡休息中").wait_for()
        assert "角色正在熟睡" in page.locator("#world-link-summary").inner_text()

        assert not page_errors and not console_errors
        browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_sources_creation_ebook_search_upload_and_bookshelf(server, width, tmp_path):
    mock = make_mock_state()
    sample_txt = tmp_path / "星野手记.txt"
    raw_content = (
        "第一章 夜班列车\n窗外掠过蓝白色的信号灯。\n\n第二章 河岸灯塔\n远处传来汽笛声。"
    )
    sample_txt.write_text(raw_content, encoding="utf-8", newline="\n")

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 1000})
        page_errors, console_errors = attach_error_guards(page)
        page.add_init_script(
            f"window.__mock = {json.dumps(mock, ensure_ascii=False)};\n"
            + BRIDGE_INIT_SCRIPT
        )
        page.goto(server)

        page.locator('[data-tab="sources"]').click()
        page.get_by_text("营造学社科普关注").wait_for()
        assert "已掩码保存" in page.locator("#sources-list").inner_text()
        assert "SESS***MASKED" not in page.locator("#sources-list").inner_text()

        # 1. Edit existing Bilibili UP source without re-entering masked cookie -> preserves '******'
        first_src = page.locator("#sources-list .item-card").first
        first_src.get_by_role("button", name="编辑").click()
        assert page.locator("#source-drawer").is_visible()
        page.locator("#src-name").fill("营造学社科普关注（更新）")
        page.locator("#btn-submit-source").click()
        page.locator("#sources-list").get_by_text("营造学社科普关注（更新）").wait_for()
        updated_call = page.evaluate(
            "window.__calls.find(c => c.route === 'sources' && c.body?.op === 'update')"
        )
        assert updated_call["body"]["config"] == {
            "mode": "up",
            "uid": "946974",
            "cookie": "******",
        }

        # 2. Create a new Bilibili popular source & an OPDS ebook source with allow_private_opds
        page.locator("#btn-open-source-drawer").click()
        assert page.locator("#source-drawer").is_visible()
        page.locator("#src-name").fill("每周必看精选")
        page.locator("#bili-mode").select_option("popular")
        page.locator("#bili-popular-kind").select_option("weekly")
        page.locator("#btn-submit-source").click()
        page.locator("#sources-list").get_by_text("每周必看精选").wait_for()
        created_call = page.evaluate(
            "window.__calls.find(c => c.route === 'sources' && c.body?.op === 'create' && c.body?.name === '每周必看精选')"
        )
        assert created_call["body"]["config"] == {
            "mode": "popular",
            "popular_kind": "weekly",
        }

        page.locator("#btn-open-source-drawer").click()
        page.locator('#source-kind-tabs [data-src-kind="ebook"]').click()
        page.locator("#src-name").fill("内网 Calibre 书斋")
        page.locator("#ebook-provider").select_option("opds")
        page.locator("#ebook-opds-url").fill("http://192.168.1.20:8083/opds")
        page.locator("#ebook-allow-private").check()
        page.locator("#btn-submit-source").click()
        page.locator("#sources-list").get_by_text("内网 Calibre 书斋").wait_for()
        opds_call = page.evaluate(
            "window.__calls.find(c => c.route === 'sources' && c.body?.op === 'create' && c.body?.name === '内网 Calibre 书斋')"
        )
        assert opds_call["body"]["config"] == {
            "provider": "opds",
            "url": "http://192.168.1.20:8083/opds",
            "allow_private_opds": True,
            "credential_origins": [],
        }

        # 3. Submit a manual clipping feed
        page.locator("#btn-open-manual-drawer").click()
        page.locator("#manual-title").fill("木构桥梁修缮札记")
        page.locator("#manual-creator").fill("地方志季刊")
        page.locator("#manual-text").fill("廊桥在梅雨季前需巡检梁底卯口。")
        page.locator("#btn-submit-source").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'sources' && c.body?.op === 'manual_feed')"
        )

        # 4. Search public ebooks & one-click add to bookshelf + upload local .txt ebook
        page.locator("#btn-open-book-modal").click()
        assert page.locator("#book-modal").is_visible()
        page.locator("#pub-book-query").fill("鲁迅")
        page.locator("#btn-search-books").click()
        page.locator("#pub-book-results").get_by_text("朝花夕拾").wait_for()
        page.locator("#pub-book-results").get_by_role(
            "button", name="加入角色书架"
        ).click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'sources' && c.body?.op === 'create' && c.body?.name === '朝花夕拾')"
        )

        page.locator("#ebook-file-input").set_input_files(str(sample_txt))
        page.locator("#btn-upload-ebook").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'sources' && c.body?.op === 'upload_ebook')"
        )
        upload_body = page.evaluate(
            "window.__calls.find(c => c.route === 'sources' && c.body?.op === 'upload_ebook').body"
        )
        assert upload_body["filename"] == "星野手记.txt"
        assert (
            base64.b64decode(upload_body["content_base64"]).decode("utf-8")
            == raw_content
        )
        page.locator("#btn-close-book-modal").click()

        # 5. Serial Bookshelf progress & status switching
        shelf = page.locator("#bookshelf-list")
        assert "第 3 / 9 章" in shelf.inner_text()
        assert "星野手记" in shelf.inner_text()
        first_book = shelf.locator(".book-card").first
        first_book.get_by_role("button", name="弃书").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'sources' && c.body?.op === 'update_book_status' && c.body?.status === 'dropped')"
        )
        assert (
            first_book.get_by_role("button", name="弃书").get_attribute("aria-pressed")
            == "true"
        )

        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        assert not page_errors and not console_errors
        browser.close()


def test_logs_filtering_dual_panes_and_xss_isolation(server):
    mock = make_mock_state()
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page_errors, console_errors = attach_error_guards(page)
        page.add_init_script(
            f"window.__mock = {json.dumps(mock, ensure_ascii=False)};\n"
            + BRIDGE_INIT_SCRIPT
        )
        page.goto(server)

        page.locator('[data-tab="logs"]').click()
        page.get_by_text(
            "十分钟看懂佛光寺东大殿斗拱 · 古建观察员 · BV1xx411c7mD"
        ).wait_for()
        assert "内容核对摘要" in page.locator("#logs-list").inner_text()
        assert "角色随感" in page.locator("#logs-list").inner_text()
        assert "交流后记" in page.locator("#logs-list").inner_text()
        assert "会话：qq:GroupMessage:1001" in page.locator("#logs-list").inner_text()
        assert "成员：member-7" in page.locator("#logs-list").inner_text()
        assert "后续感想：听她这么一说" in page.locator("#logs-list").inner_text()

        # Verify XSS payload in log-2 rendered purely as literal text and javascript: URL blocked
        assert page.locator("#logs-list img").count() == 0
        assert not page.evaluate("Boolean(window.__pwned)")
        assert page.locator('#logs-list a[href^="javascript:"]').count() == 0
        assert (
            page.locator('#logs-list a[href^="https://www.bilibili.com/"]').count() == 1
        )

        # Filter by ebook source kind
        page.locator('#logs-filter-group [data-log-kind="ebook"]').click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'logs' && c.params?.source_kind === 'ebook')"
        )
        assert (
            "银河铁道之夜 · 宫泽贤治 · 第 3 章 · 午后授课"
            in page.locator("#logs-list").inner_text()
        )
        assert "BV1xx411c7mD" not in page.locator("#logs-list").inner_text()

        assert not page_errors and not console_errors
        browser.close()


def test_reviews_verdict_actions_and_topic_blocklist(server):
    mock = make_mock_state()
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page_errors, console_errors = attach_error_guards(page)
        page.add_init_script(
            f"window.__mock = {json.dumps(mock, ensure_ascii=False)};\n"
            + BRIDGE_INIT_SCRIPT
        )
        page.goto(server)

        page.locator('[data-tab="reviews"]').click()
        page.locator("#reviews-list").get_by_text("古建筑彩画矿物颜料考").wait_for()
        assert "独立审查模型结论" in page.locator("#reviews-list").inner_text()
        assert "符合固定人格语气" in page.locator("#reviews-list").inner_text()

        # Approve pending proposal
        page.locator("#reviews-list").get_by_role("button", name="批准").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'reviews' && c.body?.op === 'approve')"
        )

        # Switch filter to rejected and check rejected reason
        page.locator('#reviews-filter-group [data-review-state="rejected"]').click()
        page.locator("#reviews-list").get_by_text("疑似软文推广帖").wait_for()
        assert "检测到引流推广特征" in page.locator("#reviews-list").inner_text()

        # Add & remove blocked topic
        page.locator("#block-topic-input").fill("博彩广告")
        page.locator("#btn-add-block-topic").click()
        page.locator("#blocked-topics-list").get_by_text("博彩广告").wait_for()
        assert page.evaluate(
            "window.__calls.some(c => c.route === 'reviews' && c.body?.op === 'block_topic' && c.body?.topic === '博彩广告')"
        )

        chip = page.locator(".blocked-tag").filter(has_text="博彩广告")
        chip.get_by_role("button", name="移除").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'reviews' && c.body?.op === 'unblock_topic' && c.body?.topic === '博彩广告')"
        )

        assert not page_errors and not console_errors
        browser.close()


def test_v2_health_disclosure_half_life_bars_journal_chips_and_cmdk_deeplink(server):
    mock = make_mock_state()
    id_a = '["rin","rin-main"]'
    mock["overviews"][id_a]["health"] = {
        "blockers": [],
        "last_error": "",
        "analysis_provider": "openai/gpt-4.1",
        "review_provider": "anthropic/claude-haiku",
        "review_context_policy": "distinct_providers",
        "model_call_quota": 50,
        "storage_warning_mb": 512,
        "proactive_scope": "all_managed_groups",
        "next_poll_at": 1763034000,
        "next_digest_at": 1763035000,
        "diagnostics": {
            "model_calls_today": 9,
            "archived_candidates": 4,
            "storage_bytes": 1677721,
            "review_jobs": [],
            "world_pending": [],
            "corrections": [],
        },
    }
    mock["logs"][0]["questions"] = [
        {
            "question_id": "q-1",
            "topic": "古建筑木构榫卯",
            "question": "你们去老街古建的时候，会抬头看屋檐底下的斗拱吗？",
        }
    ]
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page_errors, console_errors = attach_error_guards(page)
        page.add_init_script(
            f"window.__mock = {json.dumps(mock, ensure_ascii=False)};\n"
            + BRIDGE_INIT_SCRIPT
        )
        page.goto(server)

        page.get_by_text("古建筑木构榫卯").first.wait_for()

        # 1. Compact health summary chip & collapsible diagnostics disclosure
        health_det = page.locator("#health-panel details.health-disclosure")
        assert health_det.count() == 1
        assert health_det.get_attribute("open") is None
        assert "今日模型 9/50" in page.locator("#health-panel").inner_text()
        health_det.locator("summary").click()
        assert health_det.get_attribute("open") is not None
        assert "openai/gpt-4.1" in page.locator("#health-panel").inner_text()

        # 2. Visual half-life decay progress bar on active interest cards
        decay_bar = page.locator("#interests-list .decay-track[role='progressbar']").first
        assert decay_bar.get_attribute("aria-valuenow") == "82"
        assert page.locator("#interests-list .decay-fill.is-hot").count() == 1

        # 3. Compact (i) info popover opens and closes on Escape
        pop = page.locator("#panel-overview .info-popover").first
        pop.locator("summary").click()
        assert pop.get_attribute("open") is not None
        page.keyboard.press("Escape")
        assert pop.get_attribute("open") is None

        # 4. Ctrl+K same-plugin deep-link switches to Reading Journal & renders left-accent card + question chip
        page.evaluate(
            "window.dispatchEvent(new CustomEvent('dynamics-cmdk-deeplink', { detail: { deep_link: { tab: 'logs', log_id: 'log-1' } } }))"
        )
        page.locator("#panel-logs").wait_for(state="visible")
        page.locator("#logs-list .journal-card.kind-bilibili").first.wait_for()
        q_chip = page.locator("#logs-list .question-chip").first
        assert "待聊：" in q_chip.inner_text()
        assert "屋檐底下的斗拱" in q_chip.inner_text()

        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        assert not page_errors and not console_errors
        browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_wave1_held_filter_review_risks_and_series_scope_persona_sync(server, width):
    mock = make_mock_state()
    id_a = '["rin","rin-main"]'
    mock["overviews"][id_a]["stats"]["pending_reviews"] = 2
    mock["overviews"][id_a]["stats"]["held_reviews"] = 1
    mock["reviews"].append(
        {
            "proposal_id": "prop-held-1",
            "state": "held",
            "source_kind": "bilibili",
            "title": "需人工复核的视频见闻",
            "digest": "介绍某地冷门建筑修缮争议。",
            "reflection": "感觉说法还有些存疑，先记下来再看看。",
            "staged_interest": None,
            "questions": [],
            "review_verdict": "hold",
            "review_reason": "观点存在边界疑点，建议管理员复核。",
            "review_risks": [
                "raw_url_detected",
                "injection:zh_prompt_injection",
                "blocked_topic:剧透",
                "外部传闻未经核实",
            ],
            "created_at": 1763033000,
        }
    )

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 1000})
        page_errors, console_errors = attach_error_guards(page)
        page.add_init_script(
            f"window.__mock = {json.dumps(mock, ensure_ascii=False)};\n"
            + BRIDGE_INIT_SCRIPT
        )
        page.goto(server)

        page.get_by_text("古建筑木构榫卯").first.wait_for()
        # Badge includes held proposals
        assert page.locator("#badge-reviews").inner_text().strip() == "2"
        assert "含人工复核 1 项" in page.locator("#kpi-grid").inner_text()

        # Switch to reviews tab: default pending_review view shows BOTH pending_review and held
        page.locator('[data-tab="reviews"]').click()
        rev_list = page.locator("#reviews-list")
        rev_list.get_by_text("需人工复核的视频见闻").wait_for()
        assert "古建筑彩画矿物颜料考" in rev_list.inner_text()
        # Review risks rendered with Chinese labels & explanations
        assert "裸链接风险" in rev_list.inner_text()
        assert "中文越狱/改人设注入" in rev_list.inner_text()
        assert "屏蔽话题：剧透" in rev_list.inner_text()
        assert "外部传闻未经核实" in rev_list.inner_text()

        # Click dedicated held filter
        page.locator('#reviews-filter-group [data-review-state="held"]').click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'reviews' && c.params?.state === 'held')"
        )
        assert "需人工复核的视频见闻" in rev_list.inner_text()
        assert "古建筑彩画矿物颜料考" not in rev_list.inner_text()

        # Switch back to overview and verify dynamics-scopechange + dynamics-personachange sync
        page.locator('[data-tab="overview"]').click()
        page.evaluate(
            "window.dispatchEvent(new CustomEvent('dynamics-scopechange', { detail: { umo: 'napcat:GroupMessage:20002' } }))"
        )
        page.locator("#questions-list .pill.is-active-scope").wait_for()
        assert "20002" in page.locator("#questions-list .pill.is-active-scope").inner_text()

        page.evaluate(
            "window.dispatchEvent(new CustomEvent('dynamics-personachange', { detail: { persona_id: 'alice' } }))"
        )
        page.get_by_text("熟睡休息中").wait_for()
        assert page.locator("#persona-select").input_value() == '["alice","alice-fantasy"]'

        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        assert not page_errors and not console_errors
        browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_wave2_workbench_parity_presets_decay_batch_edit_and_diagnostics(server, width):
    mock = make_mock_state()
    id_a = '["rin","rin-main"]'
    mock["overviews"][id_a]["stats"]["pending_corrections"] = 1
    mock["overviews"][id_a]["runtime_config"] = {
        "daily_digest_quota": 5,
        "daily_model_call_quota": 50,
        "interest_half_life_days": 6,
        "review_mode": "llm",
    }
    mock["overviews"][id_a]["health"] = {
        "blockers": [],
        "last_error": "",
        "analysis_provider": "openai/gpt-4.1",
        "review_provider": "anthropic/claude-haiku",
        "review_context_policy": "distinct_providers",
        "model_call_quota": 50,
        "storage_warning_mb": 512,
        "proactive_enabled": True,
        "proactive_scope": "all_managed_chats",
        "diagnostics": {
            "model_calls_today": 7,
            "anli_today_total": 3,
            "cost_by_stage": [
                {"stage": "triage", "calls": 2, "prompt_tokens": 500, "completion_tokens": 120, "total_tokens": 620},
                {"stage": "video_deep_read", "calls": 2, "prompt_tokens": 1400, "completion_tokens": 450, "total_tokens": 1850},
            ],
            "video_channel_stats": {
                "total": 4,
                "native_video": 2,
                "keyframes": 1,
                "videoshot": 0,
                "subtitle_only": 1,
                "audio_transcript": 0,
                "degraded_count": 2,
                "native_hit_rate": 0.5,
                "fallback_rate": 0.5,
            },
            "archived_candidates": 2,
            "storage_bytes": 1048576,
            "review_jobs": [],
            "world_pending": [],
            "corrections": [{"correction_id": "corr-1"}],
        },
    }
    mock["overviews"][id_a]["outreach_diagnostics"] = {
        "horizon_intents": [
            {
                "intent_id": "intent-anli-1",
                "umo": "napcat:GroupMessage:10001",
                "recipient_user_id": "u100",
                "recipient_name": "小明",
                "topic": "古建筑木构榫卯",
                "horizon_kind": "anli_reflection_reply",
                "status": "pending",
                "reason": "小明之前安利了斗拱视频，最近刚看完可以自然聊一句观后感。",
                "expires_at": 1763291000,
            }
        ],
        "co_discussed_topics": [
            {
                "umo": "napcat:GroupMessage:10001",
                "member_id": "u100",
                "topic": "古建筑木构榫卯",
                "turn_count": 3,
                "last_occurred_at": 1763031800,
                "opted_out": False,
            }
        ],
        "opted_out_members": [
            {
                "kind": "all",
                "identity": id_a,
                "umo": "napcat:GroupMessage:10001",
                "member_id": "u200",
                "topic": "",
            }
        ],
    }
    mock["corrections"] = [
        {
            "correction_id": "corr-1",
            "identity": id_a,
            "umo": "napcat:GroupMessage:10001",
            "member_id": "u101",
            "log_id": "log-1",
            "log_title": "十分钟看懂佛光寺东大殿斗拱",
            "statement": "这个年份说法不对，应当是唐大中十一年。",
            "created_at": 1763032200,
        }
    ]
    mock["test_source_response"] = {
        "ok": False,
        "status": "risk_control_backoff",
        "error": "B站返回 -412",
        "advice": "命中 B 站 -412 风控：建议补充只读 Cookie。",
        "preview_items": [],
    }

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 1000})
        page_errors, console_errors = attach_error_guards(page)
        page.add_init_script(
            f"window.__mock = {json.dumps(mock, ensure_ascii=False)};\n"
            + BRIDGE_INIT_SCRIPT
        )
        page.goto(server)

        page.get_by_text("古建筑木构榫卯").first.wait_for()
        # Badge combines pending_reviews (1) + pending_corrections (1) = 2
        assert page.locator("#badge-reviews").inner_text().strip() == "2"

        # 1. Health panel shows stage-split token cost & video channel hit/fallback stats
        assert "原生直传命中率 50%" in page.locator("#health-panel").inner_text()
        assert "阶段2 视频多模态" in page.locator("#health-panel").inner_text()

        # 2. Presets & interactive decay curve preview
        assert "低耗省流" in page.locator("#presets-decay-panel").inner_text()
        assert page.locator("#presets-decay-panel svg.decay-curve-svg").count() == 1
        page.locator("#presets-decay-panel .preset-card").first.get_by_role("button").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'reviews' && c.body?.op === 'apply_preset' && c.body?.preset_id === 'conservative')"
        )

        # 3. Outreach diagnostics (intents + opted_out restore)
        diag_panel = page.locator("#outreach-diagnostics-panel")
        assert "安利回信" in diag_panel.inner_text()
        diag_panel.get_by_role("button", name="退订名单 (1)").click()
        diag_panel.get_by_role("button", name="恢复分享").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'reviews' && c.body?.op === 'restore_member_outreach')"
        )

        # 4. Sources tab: connection test (-412 advice) & bookshelf cursor adjust
        page.locator('[data-tab="sources"]').click()
        page.locator("#btn-open-source-drawer").click()
        page.locator("#bili-uid").fill("946974")
        page.locator("#btn-test-source").click()
        page.locator("#source-test-result").get_by_text("命中 B 站 -412 风控").wait_for()
        page.locator("#btn-close-source-drawer").click()

        first_book = page.locator("#bookshelf-list .book-card").first
        first_book.get_by_role("button", name="发起群共读").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'sources' && c.body?.op === 'start_co_reading')"
        )
        first_book.locator(".book-cursor-input").fill("5")
        first_book.get_by_role("button", name="调整进度").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'sources' && c.body?.op === 'update_book_status' && c.body?.chapter_cursor === 5)"
        )

        # 5. Logs tab: keyword search
        page.locator('[data-tab="logs"]').click()
        page.locator("#logs-search-input").fill("银河铁道")
        page.locator("#btn-logs-search").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'logs' && c.params?.query === '银河铁道')"
        )

        # 6. Reviews tab: inline proposal edit, batch approve & corrections queue
        page.locator('[data-tab="reviews"]').click()
        assert page.locator("#corrections-count-pill").inner_text().strip() == "1"
        assert "唐大中十一年" in page.locator("#corrections-list").inner_text()
        page.locator("#reviews-list").get_by_role("button", name="微调编辑").first.click()
        page.locator("#reviews-list .proposal-editor").first.get_by_role("button", name="保存微调").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'reviews' && c.body?.op === 'edit_proposal')"
        )
        page.locator("#reviews-select-all").check()
        page.locator("#btn-batch-approve").click()
        page.wait_for_function(
            "window.__calls.some(c => c.route === 'reviews' && c.body?.op === 'batch_approve')"
        )

        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        assert not page_errors and not console_errors
        browser.close()
