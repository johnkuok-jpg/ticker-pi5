# MIT License — Copyright (c) 2026 John Kuok
"""YouTube mode: video cache eviction.

The cache lives on tmpfs (/tmp), which is RAM-backed and about 1 GB on the Pi.
The original retention rule kept "the 5 newest files" regardless of size, and
because this mode watches 60-minute compilations, five files at ~200 MB apiece
filled tmpfs to 100% -- which then broke unrelated things that write to /tmp
(apt failing with ENOSPC, a half-written pip install leaving a corrupt
`~t_dlp` dist-info in the venv).

These tests pin the replacement policy: evict by total bytes, newest-first
retention, never evict the file the caller is about to play, and never raise
out of the pruner regardless of what state the directory is in.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ticker.modes import youtube


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """Point the module's CACHE_DIR at a scratch directory."""
    target = tmp_path / "ticker-yt-cache"
    target.mkdir()
    monkeypatch.setattr(youtube, "CACHE_DIR", target)
    return target


def _write(cache_dir: Path, name: str, size: int, age_seconds: float = 0.0) -> Path:
    """Create a file of exactly `size` bytes, optionally backdated."""
    path = cache_dir / name
    path.write_bytes(b"\0" * size)
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


def _names(cache_dir: Path) -> set[str]:
    return {p.name for p in cache_dir.iterdir()}


def _total_bytes(cache_dir: Path) -> int:
    return sum(p.stat().st_size for p in cache_dir.iterdir() if p.is_file())


# --------------------------------------------------------------------------
# Budget enforcement
# --------------------------------------------------------------------------


def test_under_budget_deletes_nothing(cache_dir):
    _write(cache_dir, "a.mp4", 100)
    _write(cache_dir, "b.mp4", 100)

    freed = youtube._prune_cache(1000)

    assert freed == 0
    assert _names(cache_dir) == {"a.mp4", "b.mp4"}


def test_evicts_until_within_budget(cache_dir):
    # 4 x 100 bytes, distinct ages. Budget of 250 fits only the two newest.
    _write(cache_dir, "newest.mp4", 100, age_seconds=1)
    _write(cache_dir, "second.mp4", 100, age_seconds=2)
    _write(cache_dir, "third.mp4", 100, age_seconds=3)
    _write(cache_dir, "oldest.mp4", 100, age_seconds=4)

    freed = youtube._prune_cache(250)

    assert _names(cache_dir) == {"newest.mp4", "second.mp4"}
    assert freed == 200
    assert _total_bytes(cache_dir) <= 250


def test_retention_is_newest_first(cache_dir):
    """The survivor is the newest file, not whichever the filesystem lists first."""
    _write(cache_dir, "aaa_old.mp4", 100, age_seconds=500)
    _write(cache_dir, "zzz_new.mp4", 100, age_seconds=1)

    youtube._prune_cache(100)

    assert _names(cache_dir) == {"zzz_new.mp4"}


def test_zero_budget_clears_everything(cache_dir):
    _write(cache_dir, "a.mp4", 100)
    _write(cache_dir, "b.mp4", 100)

    freed = youtube._prune_cache(0)

    assert _names(cache_dir) == set()
    assert freed == 200


def test_single_file_larger_than_budget_is_evicted(cache_dir):
    """A file that alone busts the budget is not grandfathered in."""
    _write(cache_dir, "huge.mp4", 5000)

    youtube._prune_cache(1000)

    assert _names(cache_dir) == set()


# --------------------------------------------------------------------------
# The keep set
# --------------------------------------------------------------------------


def test_kept_file_survives_even_over_budget(cache_dir):
    """The file about to be played must never be evicted -- that would force
    an immediate re-download of the thing we just decided to use."""
    keeper = _write(cache_dir, "playing.mp4", 5000, age_seconds=999)

    youtube._prune_cache(100, keep={keeper})

    assert keeper.exists()


def test_kept_file_does_not_consume_budget(cache_dir):
    """A kept file is excluded from the running total, so it does not cause
    otherwise-fitting neighbours to be evicted."""
    keeper = _write(cache_dir, "playing.mp4", 10_000, age_seconds=999)
    _write(cache_dir, "recent.mp4", 100, age_seconds=1)

    youtube._prune_cache(200, keep={keeper})

    assert _names(cache_dir) == {"playing.mp4", "recent.mp4"}


def test_keep_set_of_none_is_treated_as_empty(cache_dir):
    _write(cache_dir, "a.mp4", 100)

    youtube._prune_cache(0, keep=None)

    assert _names(cache_dir) == set()


# --------------------------------------------------------------------------
# Robustness -- the pruner is called on the render/playback path and must not
# be able to take the mode down.
# --------------------------------------------------------------------------


def test_missing_cache_dir_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "CACHE_DIR", tmp_path / "does-not-exist")

    assert youtube._prune_cache(100) == 0


def test_subdirectories_are_ignored(cache_dir):
    """Only files are candidates; a stray directory must not be unlinked."""
    (cache_dir / "somedir").mkdir()
    _write(cache_dir, "a.mp4", 100)

    youtube._prune_cache(0)

    assert (cache_dir / "somedir").is_dir()
    assert not (cache_dir / "a.mp4").exists()


def test_partial_download_siblings_are_evictable(cache_dir):
    """yt-dlp leaves .part/.ytdl files behind on a failed retry; the pruner
    treats them as ordinary cache occupants rather than skipping them."""
    _write(cache_dir, "vid.mp4.part", 100, age_seconds=900)
    _write(cache_dir, "vid.ytdl", 100, age_seconds=900)
    _write(cache_dir, "fresh.mp4", 100, age_seconds=1)

    youtube._prune_cache(100)

    assert _names(cache_dir) == {"fresh.mp4"}


def test_unlink_failure_is_survived(cache_dir, monkeypatch):
    """If a delete fails the pruner keeps going and still reports honestly."""
    _write(cache_dir, "a.mp4", 100, age_seconds=10)
    _write(cache_dir, "b.mp4", 100, age_seconds=20)

    def boom(self, missing_ok=False):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "unlink", boom)

    # Must not propagate, and must not claim to have freed bytes it did not.
    assert youtube._prune_cache(0) == 0
    assert _total_bytes(cache_dir) == 200


def test_file_vanishing_mid_scan_does_not_raise(cache_dir, monkeypatch):
    """A concurrent renderer process can delete a file between glob and stat."""
    _write(cache_dir, "a.mp4", 100)
    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "a.mp4":
            raise OSError("vanished")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    assert youtube._prune_cache(0) == 0


# --------------------------------------------------------------------------
# Budget constants -- the whole point is that the cache cannot fill tmpfs.
# --------------------------------------------------------------------------


def test_budget_leaves_tmpfs_headroom():
    """tmpfs is ~1 GB. Budget plus reserve must stay clear of it, or we are
    back to the original bug with extra steps."""
    tmpfs_bytes = 1006 * 1024 * 1024
    assert youtube.CACHE_BUDGET_BYTES < tmpfs_bytes / 2


def test_reserve_is_smaller_than_budget():
    """Otherwise the pre-download budget floors at zero and every fetch wipes
    the entire cache, defeating the point of caching."""
    assert 0 < youtube.CACHE_RESERVE_BYTES < youtube.CACHE_BUDGET_BYTES
