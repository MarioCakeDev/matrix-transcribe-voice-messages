from src import health


def test_write_then_read_roundtrip(tmp_path):
    path = tmp_path / "hb"
    health.write_beat(path, now=1000.0)
    assert health.read_beat(path) == 1000.0


def test_write_beat_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "hb"
    health.write_beat(path)
    assert path.exists()


def test_missing_heartbeat_is_unhealthy(tmp_path):
    healthy, message = health.check_heartbeat(tmp_path / "nope", 60, now=1000.0)
    assert healthy is False
    assert "no heartbeat" in message


def test_fresh_heartbeat_is_healthy(tmp_path):
    path = tmp_path / "hb"
    health.write_beat(path, now=1000.0)
    healthy, message = health.check_heartbeat(path, 120, now=1050.0)
    assert healthy is True
    assert "healthy" in message


def test_stale_heartbeat_is_unhealthy(tmp_path):
    path = tmp_path / "hb"
    health.write_beat(path, now=1000.0)
    healthy, message = health.check_heartbeat(path, 120, now=1200.0)
    assert healthy is False
    assert "old" in message


def test_corrupt_heartbeat_is_unhealthy(tmp_path):
    path = tmp_path / "hb"
    path.write_text("not-a-number")
    assert health.read_beat(path) is None
    healthy, _ = health.check_heartbeat(path, 60, now=1000.0)
    assert healthy is False


def test_write_beat_never_raises(tmp_path, monkeypatch):
    path = tmp_path / "hb"

    def boom(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(health.os, "replace", boom)
    health.write_beat(path)


def test_check_rejects_non_positive_max_age(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        health.check_heartbeat(tmp_path / "hb", 0)


def test_cli_healthy_exits_zero(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path))
    assert health.main(["--check", "--store", str(tmp_path), "--max-age", "120"]) == 0


def test_cli_stale_exits_one(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path), now=0.0)
    assert health.main(["--check", "--store", str(tmp_path), "--max-age", "1"]) == 1


def test_cli_missing_exits_one(tmp_path):
    assert health.main(["--check", "--store", str(tmp_path)]) == 1


def test_cli_invalid_max_age_exits_two(tmp_path):
    assert health.main(["--check", "--store", str(tmp_path), "--max-age", "abc"]) == 2


def test_cli_requires_check_flag(tmp_path):
    import pytest

    with pytest.raises(SystemExit) as excinfo:
        health.main(["--store", str(tmp_path)])
    assert excinfo.value.code == 2
