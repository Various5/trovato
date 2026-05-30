"""Hardware detection + performance auto-tuning.

The same build has to run well on a 2-core / 4 GB laptop and a 16-core / 64 GB
workstation. Historically every resource knob (worker count, embedding batch
size, OCR render DPI, per-image OCR pixel cap, vision concurrency) was a magic
number tuned for one mid-range machine. This module detects the host and
derives those knobs so the app scales itself to the box it runs on.

Everything is best-effort and never raises: if ``psutil`` is missing or a probe
fails we fall back to conservative defaults that match the previous hardcoded
"balanced" values, so behaviour on a typical machine is unchanged.

Resolution order for the *effective* tuning:

1. ``settings.performance_profile`` — ``auto`` (default), ``low``,
   ``balanced`` or ``high``. ``auto`` classifies the detected hardware into a
   tier; the explicit names pin a tier regardless of hardware.
2. ``settings.parallel_workers`` — a manual override. ``0`` (the new default)
   means "let the profile decide"; any positive value overrides just the heavy
   worker count, with the I/O-bound quick-phase concurrency derived from it.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import asdict, dataclass
from functools import lru_cache

#: Profiles a user may pick in Settings. ``auto`` resolves to a tier below.
PROFILES: tuple[str, ...] = ("auto", "low", "balanced", "high")
#: Concrete hardware tiers ``auto`` resolves into.
TIERS: tuple[str, ...] = ("low", "balanced", "high")


@dataclass(frozen=True)
class HardwareInfo:
    """A snapshot of the host's relevant capabilities."""

    logical_cores: int
    physical_cores: int
    total_ram_gb: float
    available_ram_gb: float
    gpu: str  # human-readable GPU hint, "" when none detected
    has_gpu: bool
    machine: str  # platform.machine() e.g. "AMD64", "arm64"
    system: str  # "Windows" | "Darwin" | "Linux"
    psutil_available: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Tuning:
    """Resolved resource knobs for the current machine + profile."""

    profile: str  # the requested profile ("auto" | tier)
    tier: str  # the concrete tier in use ("low" | "balanced" | "high")
    workers: int  # concurrent documents during heavy phases (OCR/embed/vision)
    quick_workers: int  # concurrency for the I/O-bound quick phase
    embed_batch: int  # texts per embedding request
    page_dpi: int  # DPI for rendering pages to PNG before OCR
    image_ocr_max_pixels: int  # skip per-image OCR above this area
    vision_concurrency: int  # concurrent vision-model calls
    http_timeout: float  # LM Studio HTTP timeout (seconds)
    auto_resolved: bool  # True when the tier came from auto-detection
    worker_override: bool  # True when parallel_workers forced the worker count

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _detect_gpu() -> tuple[bool, str]:
    """Best-effort, non-blocking GPU hint.

    We deliberately avoid spawning ``nvidia-smi`` (slow, and the LLM runs in a
    separate process anyway). GPU presence is informational and only nudges
    vision concurrency, so a cheap heuristic is enough:

    * an ``nvidia-smi`` / ``rocminfo`` binary on PATH ⇒ a discrete GPU,
    * Apple Silicon ⇒ a capable integrated GPU.
    """
    try:
        if shutil.which("nvidia-smi"):
            return True, "NVIDIA (nvidia-smi on PATH)"
        if shutil.which("rocminfo"):
            return True, "AMD ROCm (rocminfo on PATH)"
        if platform.system() == "Darwin" and platform.machine().lower() in ("arm64", "aarch64"):
            return True, "Apple Silicon (integrated)"
    except Exception:
        pass
    return False, ""


@lru_cache(maxsize=1)
def detect_hardware() -> HardwareInfo:
    """Detect host capabilities once and cache the result.

    Static for the lifetime of the process; call ``detect_hardware.cache_clear()``
    in tests to re-probe.
    """
    logical = os.cpu_count() or 1
    physical = logical
    total_gb = 0.0
    avail_gb = 0.0
    have_psutil = False
    try:
        import psutil  # type: ignore

        have_psutil = True
        physical = psutil.cpu_count(logical=False) or logical
        vm = psutil.virtual_memory()
        total_gb = round(vm.total / (1024**3), 2)
        avail_gb = round(vm.available / (1024**3), 2)
    except Exception:
        # psutil missing or probe failed — keep conservative fallbacks.
        physical = max(1, logical // 2) if logical > 1 else 1

    has_gpu, gpu = _detect_gpu()
    return HardwareInfo(
        logical_cores=logical,
        physical_cores=max(1, physical),
        total_ram_gb=total_gb,
        available_ram_gb=avail_gb,
        gpu=gpu,
        has_gpu=has_gpu,
        machine=platform.machine(),
        system=platform.system(),
        psutil_available=have_psutil,
    )


# ---------------------------------------------------------------------------
# Tier classification + recommendation
# ---------------------------------------------------------------------------


def classify_tier(hw: HardwareInfo) -> str:
    """Bucket a machine into low / balanced / high.

    RAM is only used to *downgrade* — when it's unknown (psutil absent) we
    classify on cores alone so a beefy box without psutil isn't crippled.
    """
    cores = hw.physical_cores
    ram = hw.total_ram_gb  # 0.0 == unknown

    if cores <= 2 or (ram and ram < 6):
        return "low"
    if cores >= 8 and (not ram or ram >= 16):
        return "high"
    return "balanced"


def _recommend(tier: str, hw: HardwareInfo) -> Tuning:
    """Map a tier (+ raw core count) to concrete knobs.

    The ``balanced`` numbers intentionally equal the previous hardcoded
    defaults (workers≈2, embed_batch=128, dpi=220, 4 MP image cap) so existing
    mid-range machines see identical behaviour.
    """
    cores = hw.physical_cores

    if tier == "low":
        return Tuning(
            profile=tier,
            tier=tier,
            workers=1,
            quick_workers=4,
            embed_batch=32,
            page_dpi=150,
            image_ocr_max_pixels=2_000_000,
            vision_concurrency=1,
            http_timeout=120.0,
            auto_resolved=False,
            worker_override=False,
        )
    if tier == "high":
        workers = min(8, max(4, cores - 1))
        return Tuning(
            profile=tier,
            tier=tier,
            workers=workers,
            quick_workers=min(24, workers * 4),
            embed_batch=256,
            page_dpi=300,
            image_ocr_max_pixels=6_000_000,
            vision_concurrency=2 if hw.has_gpu else 1,
            http_timeout=180.0,
            auto_resolved=False,
            worker_override=False,
        )
    # balanced (default)
    workers = min(4, max(2, cores // 2))
    return Tuning(
        profile=tier,
        tier=tier,
        workers=workers,
        quick_workers=min(16, workers * 4),
        embed_batch=128,
        page_dpi=220,
        image_ocr_max_pixels=4_000_000,
        vision_concurrency=1,
        http_timeout=150.0,
        auto_resolved=False,
        worker_override=False,
    )


def resolve_tuning(
    profile: str,
    hw: HardwareInfo,
    *,
    worker_override: int = 0,
) -> Tuning:
    """Resolve the effective :class:`Tuning` for a profile + machine.

    ``profile`` is one of :data:`PROFILES`. ``worker_override`` > 0 pins the
    heavy worker count (mapping the legacy ``parallel_workers`` setting), while
    every other knob still follows the resolved tier.
    """
    requested = (profile or "auto").strip().lower()
    if requested not in PROFILES:
        requested = "auto"

    auto = requested == "auto"
    tier = classify_tier(hw) if auto else requested
    base = _recommend(tier, hw)

    workers = base.workers
    quick = base.quick_workers
    overridden = False
    if worker_override and worker_override > 0:
        workers = max(1, int(worker_override))
        quick = min(24, max(4, workers * 4))
        overridden = True

    from dataclasses import replace

    return replace(
        base,
        profile=requested,
        tier=tier,
        workers=workers,
        quick_workers=quick,
        auto_resolved=auto,
        worker_override=overridden,
    )


def active_tuning() -> Tuning:
    """The effective tuning derived from the live settings + detected hardware.

    Reads ``performance_profile`` and ``parallel_workers`` from settings. Cheap
    enough to call per scan job; detection itself is cached.
    """
    from app.config import get_settings

    s = get_settings()
    profile = getattr(s, "performance_profile", "auto")
    override = getattr(s, "parallel_workers", 0) or 0
    return resolve_tuning(profile, detect_hardware(), worker_override=override)


def tuning_summary() -> dict:
    """Combined hardware + active-tuning payload for the API / Diagnostics UI."""
    hw = detect_hardware()
    tuning = active_tuning()
    return {
        "hardware": hw.as_dict(),
        "tuning": tuning.as_dict(),
        "profiles": list(PROFILES),
    }
