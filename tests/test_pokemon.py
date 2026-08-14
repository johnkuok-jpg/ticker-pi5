# MIT License — Copyright (c) 2026 John Kuok
"""Pokémon mode: sprite caching, round scheduling, and safe offline fallback."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from ticker import config as config_module
from ticker.canvas import Canvas
from ticker.modes import MODE_TYPES
from ticker.modes.pokemon import (
    _DISSOLVE_SECS,
    _FADE_OUT_SECS,
    _REVEAL_SECS,
    _SILHOUETTE_SECS,
    PokemonMode,
)
from ticker.modes.pokemon_names import GEN1_NAMES


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: state)
    return config_module.load_config()


def _fake_sprite() -> bytes:
    """A tiny valid 96×96 RGBA PNG with a diamond of visible pixels.

    Using an actual PNG rather than mocking the download makes the sprite
    trimming code path exercise its bbox and thumbnail branches too.
    """
    img = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    # Diamond of solid pixels in the middle so the bbox isn't the full 96×96.
    for x in range(30, 66):
        for y in range(30, 66):
            if abs(x - 48) + abs(y - 48) < 18:
                img.putpixel((x, y), (255, 200, 0, 255))
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_pokemon_mode_is_registered():
    assert "pokemon" in MODE_TYPES
    assert MODE_TYPES["pokemon"] is PokemonMode


def test_gen1_name_table_has_151_entries():
    assert len(GEN1_NAMES) == 151
    # A couple of specific entries so a copy-paste shift is caught.
    assert GEN1_NAMES[0] == "Bulbasaur"
    assert GEN1_NAMES[24] == "Pikachu"    # dex 25
    assert GEN1_NAMES[149] == "Mewtwo"    # dex 150
    assert GEN1_NAMES[150] == "Mew"       # dex 151


def test_load_sprite_downloads_and_caches(cfg, monkeypatch):
    """First read hits the network; second read is served from disk cache."""
    mode = PokemonMode(cfg)
    calls = {"count": 0}

    def fake_urlopen(request, timeout=None):
        calls["count"] += 1

        class _R:
            def read(self):
                return _fake_sprite()
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    sprite = mode._load_sprite(25)
    assert sprite is not None
    assert sprite.size == (32, 32)
    assert calls["count"] == 1
    # Cached on disk
    assert (cfg.state_dir / "pokemon" / "025.png").exists()
    # Second load: still one network call, satisfied from in-memory cache
    mode._loaded.clear()  # force re-read from disk
    sprite2 = mode._load_sprite(25)
    assert sprite2 is not None
    assert calls["count"] == 1


def test_load_sprite_returns_none_on_network_error(cfg, monkeypatch):
    """A failed fetch should propagate as None so the mode can skip the dex."""
    mode = PokemonMode(cfg)
    import urllib.error

    def boom(request, timeout=None):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert mode._load_sprite(1) is None
    # No file left behind
    assert not (cfg.state_dir / "pokemon" / "001.png").exists()


def test_render_uses_placeholder_when_pool_is_empty(cfg, monkeypatch):
    """If every sprite fetch fails, the mode shouldn't crash — it should paint
    a placeholder so the panel stays alive."""
    mode = PokemonMode(cfg)
    import urllib.error

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("nope"))
    )
    canvas = Canvas(cfg.width, cfg.height)
    # Should not raise, even after burning through the full pool.
    mode.render(canvas, tick=0)
    mode.render(canvas, tick=30)


def test_render_progresses_through_phases_without_error(cfg, tmp_path):
    """Seed one cached sprite, then step through a full round of ticks."""
    # Seed a plausible sprite so no network call is made.
    (cfg.state_dir / "pokemon").mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "pokemon" / "025.png").write_bytes(_fake_sprite())
    mode = PokemonMode(cfg)
    mode._pool = [25]  # force pikachu

    fps = cfg.fps
    # Sample across all four phases: silhouette, dissolve, reveal, fade-out.
    total_secs = _SILHOUETTE_SECS + _DISSOLVE_SECS + _REVEAL_SECS + _FADE_OUT_SECS
    for phase_frac in (0.1, 0.5, 0.7, 0.95):
        tick = int(total_secs * fps * phase_frac)
        canvas = Canvas(cfg.width, cfg.height)
        mode.render(canvas, tick)
        # Some pixel got drawn somewhere.
        assert canvas.image_buffer.getbbox() is not None


def test_pool_reshuffles_after_exhausting(cfg, monkeypatch):
    """When the pool is drained, the next pick reshuffles all 151 dex numbers."""
    (cfg.state_dir / "pokemon").mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "pokemon" / "025.png").write_bytes(_fake_sprite())
    mode = PokemonMode(cfg)
    # Set pool to a single dex we've cached
    mode._pool = [25]
    assert mode._next_dex() == 25
    assert mode._pool == []
    # Downloading anything else will fail; but 25 is on disk so the reshuffle
    # will eventually land on it.
    import urllib.error
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("nope"))
    )
    dex = mode._next_dex()
    assert dex == 25  # only cached sprite


def test_valid_modes_includes_pokemon():
    assert "pokemon" in config_module.VALID_MODES
