"""Quality variants and the adaptive quality controller."""

from ashytv.services.quality import (
    LONG_STALL,
    STABLE_TO_STEP_UP,
    UPGRADE_BACKOFF,
    QualityController,
    base_key,
    split_quality,
)


def test_split_quality():
    assert split_quality("NEWS 24 FHD") == ("NEWS 24", 3)
    assert split_quality("News 24 HD [ALT]") == ("News 24", 2)
    assert split_quality("Sports 2 SD") == ("Sports 2", 1)
    assert split_quality("Nature 4K") == ("Nature", 4)
    assert split_quality("Kids H265 HD") == ("Kids", 2)
    assert split_quality("STATION -  CITY FHD") == ("STATION - CITY", 3)
    assert split_quality("Music 24H") == ("Music 24H", 0)  # 24H is not a quality
    assert split_quality("HD") == ("HD", 2)  # never returns an empty name
    assert base_key("Estação  Ação") == base_key("ESTACAO ACAO")


def test_steps_down_after_repeated_stalls():
    q = QualityController(levels=3)
    q.start(0)
    assert q.stall_started(10) is None
    q.stall_ended(11)
    assert q.stall_started(30) == 1  # second stall within the window
    assert q.index == 1


def test_long_stall_steps_down():
    q = QualityController(levels=2)
    q.start(0)
    q.stall_started(5)
    assert q.tick(5 + LONG_STALL - 1, buffer_full=False) is None
    assert q.tick(5 + LONG_STALL, buffer_full=False) == 1


def test_steps_up_after_stable_period_and_backs_off_on_failure():
    q = QualityController(levels=3, index=2)
    q.start(0)
    assert q.tick(STABLE_TO_STEP_UP - 1, buffer_full=True) is None
    assert q.tick(STABLE_TO_STEP_UP, buffer_full=True) == 1
    # Stalls right after stepping up: go back down and wait before retrying.
    t = STABLE_TO_STEP_UP + 10
    assert q.stall_started(t) == 2
    q.stall_ended(t + 1)
    assert q.tick(t + STABLE_TO_STEP_UP + 1, buffer_full=True) is None
    assert q.tick(t + UPGRADE_BACKOFF + 1, buffer_full=True) == 1


def test_no_step_up_without_full_buffer():
    q = QualityController(levels=2, index=1)
    q.start(0)
    assert q.tick(STABLE_TO_STEP_UP * 2, buffer_full=False) is None


def test_offline_variant_falls_back_even_in_manual_mode():
    q = QualityController(levels=3)
    q.start(0)
    q.select(1, 1)
    assert q.auto is False
    assert q.stall_started(2) is None and q.stall_started(3) is None  # manual: no adapting
    assert q.failed(4) == 2
    assert q.failed(5) is None  # nothing lower left
