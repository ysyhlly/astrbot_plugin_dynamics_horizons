"""Batch 4 regression tests for astrbot_plugin_dynamics_horizons.

Covers:
- Hot-reload explicitly reloads `series_panels` in Main.initialize() before
  registering panels (aligned with sibling repos & reload_bootstrap).
- Packaging script (`tools/package_release.py`) produces a single top-level
  plugin directory ZIP, excludes dev files/dirs (`.github/`, `tests/`, `tools/`,
  `package-lock.json`, `build_release.py`, `.gitignore`), and enforces
  `check_python` rejecting standard `logging` imports in runtime modules.
"""

from __future__ import annotations

import pathlib
import types
import zipfile
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_dynamics_horizons import main as horizons_main
from astrbot_plugin_dynamics_horizons.tools import package_release

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_batch4_horizons_initialize_reloads_series_panels(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    reloaded: list[str] = []
    registered: list[tuple[object, str]] = []

    fake_sp = types.ModuleType("astrbot_plugin_dynamics_horizons.series_panels")
    fake_sp.__file__ = str(PLUGIN_ROOT / "series_panels.py")
    fake_sp._horizons_loaded_source_hash = "stale-hash-before-hot-reload"
    fake_sp.register_series_panels = lambda owner, pid: registered.append((owner, pid))

    monkeypatch.setattr(horizons_main, "import_module", lambda name: fake_sp)
    monkeypatch.setattr(
        horizons_main, "_series_panels_mod", horizons_main._series_panels_mod
    )

    def _fake_reload(mod):
        reloaded.append(mod.__name__)
        return mod

    monkeypatch.setattr(horizons_main, "reload", _fake_reload)
    monkeypatch.setattr(horizons_main, "cleanup_stale_temp_dirs", lambda: None)

    ctx = types.SimpleNamespace(get_config=lambda: {})
    plugin = horizons_main.Main(ctx, {})
    plugin._data_dir_override = tmp_path
    plugin.web_api.register = lambda: None
    plugin.web_api.unregister = lambda: None
    plugin.web_api.drain = AsyncMock()

    # Replace background loops so initialize() terminates cleanly without real polling
    async def _noop_loop(self):
        return None

    monkeypatch.setattr(horizons_main.HorizonsEngine, "poll_loop", _noop_loop)
    monkeypatch.setattr(horizons_main.HorizonsEngine, "digest_loop", _noop_loop)
    monkeypatch.setattr(horizons_main.HorizonsEngine, "maintenance_loop", _noop_loop)

    await plugin.initialize()
    try:
        assert reloaded == ["astrbot_plugin_dynamics_horizons.series_panels"]
        assert len(registered) == 1
        assert registered[0][0] is plugin
        assert registered[0][1] == horizons_main.PLUGIN_ID
    finally:
        await plugin.terminate()


def test_batch4_horizons_package_release_single_root_blacklist_and_python_check(
    tmp_path: pathlib.Path,
):
    # 1) check_python rejects standard logging in runtime modules, allows in non-runtime
    with pytest.raises(ValueError, match="astrbot.api.logger"):
        package_release.check_python(b"import logging\n", "core/bad.py", runtime=True)
    package_release.check_python(b"import logging\n", "tests/ok.py", runtime=False)

    # 2) Build install/source archives and verify install ZIP hygiene
    records = package_release.build(PLUGIN_ROOT, tmp_path / "dist")
    install_rec = next(r for r in records if r["kind"] == "install")
    zip_path = tmp_path / "dist" / install_rec["file"]
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert names
    top_dirs = {n.split("/", 1)[0] for n in names}
    assert top_dirs == {"astrbot_plugin_dynamics_horizons"}
    forbidden_dirs = {".github", "tests", "tools", "docs", "scripts", "frontend"}
    forbidden_files = {
        "package.json",
        "package-lock.json",
        "build_release.py",
        ".gitignore",
        ".gitattributes",
        "pyproject.toml",
        "pytest.ini",
    }
    for name in names:
        rel_parts = name.split("/")[1:]
        assert not (set(rel_parts) & forbidden_dirs), f"Dev dir leaked: {name}"
        assert rel_parts[-1] not in forbidden_files, f"Dev file leaked: {name}"
