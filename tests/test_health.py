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


# ── decrypt-aware health contract ──────────────────────────────────────────
def test_state_roundtrip_and_merge(tmp_path):
    path = tmp_path / "state.json"
    health.write_state(path, now=1000.0, last_message_at=999.0)
    health.write_state(path, now=1010.0, last_transcription_at=1005.0)

    state = health.read_state(path)
    assert state["last_message_at"] == 999.0
    assert state["last_transcription_at"] == 1005.0
    assert state["updated_at"] == 1010.0


def test_missing_and_corrupt_state_are_empty(tmp_path):
    assert health.read_state(tmp_path / "nope.json") == {}
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{not json")
    assert health.read_state(corrupt) == {}


def test_record_decrypt_failure_keeps_a_decaying_window(tmp_path):
    path = health.state_path(tmp_path)
    health.record_decrypt_failure(path, "!r", "$e1", now=1000.0, window_minutes=60)
    health.record_decrypt_failure(path, "!r", "$e2", now=5000.0, window_minutes=60)

    state = health.read_state(path)
    assert state["decrypt_failures"] == [5000.0]
    assert state["last_decrypt_failure_event"] == "$e2"
    assert state["last_decrypt_failure_room"] == "!r"


def test_evaluate_is_healthy_with_a_fresh_beat_and_no_state(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path))
    healthy, message = health.evaluate(tmp_path, max_age=120)
    assert healthy is True
    assert "healthy" in message


def test_evaluate_is_unhealthy_when_the_heartbeat_is_stale(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path), now=0.0)
    healthy, message = health.evaluate(tmp_path, max_age=120)
    assert healthy is False
    assert "old" in message


def test_evaluate_is_unhealthy_when_device_keys_are_stale(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path))
    health.write_state(health.state_path(tmp_path), device_keys_ok=False)

    healthy, message = health.evaluate(tmp_path, max_age=120)

    assert healthy is False
    assert "device keys" in message


def test_evaluate_is_unhealthy_when_undecryptable_traffic_is_unanswered(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path))
    path = health.state_path(tmp_path)
    health.record_decrypt_failure(path, "!room:example.org", "$enc1", now=1000.0)

    healthy, message = health.evaluate(tmp_path, max_age=120, now=1010.0)

    assert healthy is False
    assert "undecryptable" in message
    assert "!room:example.org" in message
    assert "$enc1" in message


def test_evaluate_is_healthy_again_once_a_transcription_succeeds(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path))
    path = health.state_path(tmp_path)
    health.record_decrypt_failure(path, "!room:example.org", "$enc1", now=1000.0)
    health.record_transcription(path, now=1020.0)

    healthy, message = health.evaluate(tmp_path, max_age=120, now=1030.0)

    assert healthy is True
    assert "transcription" in message


def test_evaluate_ignores_failures_outside_the_window(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path))
    path = health.state_path(tmp_path)
    health.record_decrypt_failure(path, "!room:example.org", "$enc1", now=1000.0)

    healthy, _message = health.evaluate(
        tmp_path, max_age=120, now=1000.0 + 3 * 3600, decrypt_window_minutes=60
    )

    assert healthy is True


def test_evaluate_honours_a_higher_threshold(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path))
    path = health.state_path(tmp_path)
    health.record_decrypt_failure(path, "!r", "$e1", now=1000.0)

    healthy, _ = health.evaluate(tmp_path, max_age=120, now=1001.0, decrypt_threshold=5)

    assert healthy is True


def test_evaluate_reports_no_transcription_while_traffic_continues(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path))
    path = health.state_path(tmp_path)
    health.write_state(
        path,
        now=1000.0,
        last_message_at=1000.0,
        last_transcription_at=1000.0 - 10 * 3600,
        decrypt_failures=[1000.0 - 5 * 3600],
    )

    healthy, message = health.evaluate(
        tmp_path, max_age=120, now=1000.0, decrypt_threshold=5, stale_transcribe_hours=6
    )

    assert healthy is False
    assert "no transcription" in message


def test_cli_reports_undecryptable_traffic(tmp_path, capsys):
    health.write_beat(health.heartbeat_path(tmp_path))
    health.record_decrypt_failure(
        health.state_path(tmp_path), "!room:example.org", "$enc1"
    )

    assert health.main(["--check", "--store", str(tmp_path)]) == 1
    assert "undecryptable" in capsys.readouterr().err


def test_cli_accepts_the_new_tuning_flags(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path))
    health.record_decrypt_failure(health.state_path(tmp_path), "!r", "$e1")

    assert (
        health.main(
            [
                "--check",
                "--store",
                str(tmp_path),
                "--decrypt-window-minutes",
                "30",
                "--decrypt-threshold",
                "2",
                "--stale-transcribe-hours",
                "6",
            ]
        )
        == 0
    )


def test_cli_rejects_invalid_threshold(tmp_path):
    health.write_beat(health.heartbeat_path(tmp_path))
    assert health.main(["--check", "--store", str(tmp_path), "--decrypt-threshold", "0"]) == 2
