"""Tests for hardware detection + performance auto-tuning."""

from __future__ import annotations

import pytest

from app.config import get_settings, save_user_settings
from app.services.hardware import (
    PROFILES,
    TIERS,
    HardwareInfo,
    active_tuning,
    classify_tier,
    detect_hardware,
    resolve_tuning,
    tuning_summary,
)


def _hw(cores: int, ram_gb: float, *, gpu: bool = False, psutil: bool = True) -> HardwareInfo:
    return HardwareInfo(
        logical_cores=cores * 2,
        physical_cores=cores,
        total_ram_gb=ram_gb,
        available_ram_gb=ram_gb / 2,
        gpu="Test GPU" if gpu else "",
        has_gpu=gpu,
        machine="AMD64",
        system="Windows",
        psutil_available=psutil,
    )


@pytest.fixture(autouse=True)
def _restore_settings():
    """Each test mutates settings.json in the shared session data dir — reset."""
    yield
    save_user_settings({"performance_profile": "auto", "parallel_workers": 0})
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cores,ram,expected",
    [
        (1, 4, "low"),
        (2, 8, "low"),  # 2 cores -> low regardless of RAM
        (4, 4, "low"),  # <6 GB RAM -> low regardless of cores
        (4, 16, "balanced"),
        (6, 16, "balanced"),
        (8, 8, "balanced"),  # 8 cores but only 8 GB -> not high
        (8, 16, "high"),
        (16, 64, "high"),
    ],
)
def test_classify_tier(cores, ram, expected):
    assert classify_tier(_hw(cores, ram)) == expected


def test_classify_tier_unknown_ram_uses_cores():
    # psutil missing -> total_ram_gb == 0.0; must classify on cores alone and
    # not get stuck in "low" purely because RAM is unknown.
    assert classify_tier(_hw(8, 0.0, psutil=False)) == "high"
    assert classify_tier(_hw(4, 0.0, psutil=False)) == "balanced"
    assert classify_tier(_hw(2, 0.0, psutil=False)) == "low"


# ---------------------------------------------------------------------------
# Tuning resolution
# ---------------------------------------------------------------------------


def test_balanced_matches_legacy_defaults():
    # The balanced tier must equal the previous hardcoded values so existing
    # mid-range machines see no behaviour change.
    tn = resolve_tuning("balanced", _hw(4, 16))
    assert tn.embed_batch == 128
    assert tn.page_dpi == 220
    assert tn.image_ocr_max_pixels == 4_000_000
    assert 2 <= tn.workers <= 4


def test_low_profile_is_conservative():
    tn = resolve_tuning("low", _hw(8, 32))  # beefy box, but pinned low
    assert tn.tier == "low"
    assert tn.workers == 1
    assert tn.page_dpi == 150
    assert tn.embed_batch <= 64


def test_high_profile_scales_up():
    tn = resolve_tuning("high", _hw(16, 64, gpu=True))
    assert tn.tier == "high"
    assert tn.workers >= 4
    assert tn.page_dpi == 300
    assert tn.embed_batch == 256
    assert tn.vision_concurrency == 2  # GPU present


def test_high_profile_without_gpu_single_vision():
    tn = resolve_tuning("high", _hw(16, 64, gpu=False))
    assert tn.vision_concurrency == 1


def test_auto_resolves_and_flags():
    tn = resolve_tuning("auto", _hw(2, 4))
    assert tn.profile == "auto"
    assert tn.tier == "low"
    assert tn.auto_resolved is True


def test_explicit_profile_not_auto_resolved():
    tn = resolve_tuning("high", _hw(16, 64))
    assert tn.auto_resolved is False
    assert tn.tier == "high"


def test_unknown_profile_falls_back_to_auto():
    tn = resolve_tuning("nonsense", _hw(4, 16))
    assert tn.profile == "auto"
    assert tn.tier in TIERS


def test_worker_override_pins_workers():
    tn = resolve_tuning("low", _hw(8, 32), worker_override=6)
    assert tn.workers == 6
    assert tn.worker_override is True
    assert tn.quick_workers >= tn.workers
    # other knobs still follow the (low) tier
    assert tn.page_dpi == 150


def test_worker_override_zero_is_ignored():
    tn = resolve_tuning("balanced", _hw(4, 16), worker_override=0)
    assert tn.worker_override is False


def test_workers_are_monotonic_in_cores():
    prev = 0
    for cores in (1, 2, 4, 8, 16, 32):
        tn = resolve_tuning("auto", _hw(cores, 64))
        assert tn.workers >= prev
        prev = tn.workers


def test_quick_workers_never_below_workers():
    for tier in TIERS:
        for cores in (1, 2, 4, 8, 16):
            tn = resolve_tuning(tier, _hw(cores, 16))
            assert tn.quick_workers >= tn.workers


# ---------------------------------------------------------------------------
# Live wiring through settings
# ---------------------------------------------------------------------------


def test_active_tuning_reads_profile():
    save_user_settings({"performance_profile": "low"})
    get_settings.cache_clear()
    assert active_tuning().tier == "low"


def test_active_tuning_reads_worker_override():
    save_user_settings({"performance_profile": "auto", "parallel_workers": 7})
    get_settings.cache_clear()
    tn = active_tuning()
    assert tn.workers == 7
    assert tn.worker_override is True


def test_detect_hardware_is_sane():
    hw = detect_hardware()
    assert hw.logical_cores >= 1
    assert hw.physical_cores >= 1


def test_tuning_summary_shape():
    summ = tuning_summary()
    assert set(summ) == {"hardware", "tuning", "profiles"}
    assert summ["profiles"] == list(PROFILES)
    assert "workers" in summ["tuning"]
    assert "physical_cores" in summ["hardware"]


def test_settings_route_enum_validation_config():
    # The settings route constrains performance_profile to PROFILES.
    from app.api.routes.settings import _ALLOWED_KEYS, _ENUM_KEYS

    assert "performance_profile" in _ALLOWED_KEYS
    assert _ENUM_KEYS["performance_profile"] == set(PROFILES)
