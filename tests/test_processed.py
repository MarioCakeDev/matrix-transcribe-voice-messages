import sqlite3

import pytest

from src.processed import ProcessedStore, database_path


def make_store(tmp_path, *, stale_seconds=900.0, clock=None):
    kwargs = {"stale_seconds": stale_seconds}
    if clock is not None:
        kwargs["clock"] = clock
    return ProcessedStore(database_path(tmp_path), **kwargs).open()


def test_first_claim_wins_and_second_is_skipped(tmp_path):
    store = make_store(tmp_path)
    try:
        assert store.claim("$e1", "!room:example.org") is True
        assert store.claim("$e1", "!room:example.org") is False
        assert store.seen("$e1")
    finally:
        store.close()


def test_completion_is_recorded(tmp_path):
    store = make_store(tmp_path)
    try:
        store.claim("$e1", "!room:example.org")
        store.complete("$e1")
        assert store.status_of("$e1") == "replied"
    finally:
        store.close()


def test_failed_event_is_not_retried(tmp_path):
    store = make_store(tmp_path)
    try:
        store.claim("$e1", "!room:example.org")
        store.complete("$e1", status="failed")
        assert store.claim("$e1", "!room:example.org") is False
        assert store.status_of("$e1") == "failed"
    finally:
        store.close()


def test_unfinished_claim_is_retried_after_the_stale_window(tmp_path):
    store = make_store(tmp_path, stale_seconds=60.0)
    try:
        store.claim("$e1", "!room:example.org", now=1000.0)
        # Still inside the window: a live / concurrent run owns it.
        assert store.claim("$e1", "!room:example.org", now=1030.0) is False
        # Past the window: the previous run died mid-transcription.
        assert store.claim("$e1", "!room:example.org", now=1100.0) is True
        assert store.status_of("$e1") == "processing"
    finally:
        store.close()


def test_empty_event_id_is_never_deduped(tmp_path):
    store = make_store(tmp_path)
    try:
        assert store.claim("", "!room:example.org") is True
        assert store.claim("", "!room:example.org") is True
    finally:
        store.close()


def test_state_survives_reopening(tmp_path):
    store = make_store(tmp_path)
    store.claim("$e1", "!room:example.org")
    store.complete("$e1")
    store.close()

    reopened = ProcessedStore(database_path(tmp_path)).open()
    try:
        assert reopened.seen("$e1")
        assert reopened.count() == 1
    finally:
        reopened.close()


def test_prune_drops_rows_older_than_the_age_limit(tmp_path):
    store = make_store(tmp_path)
    try:
        store.claim("$old", "!room:example.org", now=1000.0)
        store.complete("$old", now=1000.0)
        store.claim("$new", "!room:example.org", now=2_000_000.0)
        store.complete("$new", now=2_000_000.0)

        deleted = store.prune(max_age_days=1.0, max_rows=100, now=2_000_000.0)

        assert deleted == 1
        assert store.seen("$new")
        assert not store.seen("$old")
    finally:
        store.close()


def test_prune_caps_the_row_count_keeping_the_newest(tmp_path):
    store = make_store(tmp_path)
    try:
        for index in range(5):
            event = f"$e{index}"
            store.claim(event, "!room:example.org", now=1000.0 + index)
            store.complete(event, now=1000.0 + index)

        deleted = store.prune(max_age_days=1000.0, max_rows=2, now=2000.0)

        assert deleted == 3
        assert store.count() == 2
        assert store.seen("$e4") and store.seen("$e3")
    finally:
        store.close()


def test_prune_keeps_unfinished_rows(tmp_path):
    store = make_store(tmp_path, stale_seconds=10_000.0)
    try:
        store.claim("$pending", "!room:example.org", now=1000.0)
        store.prune(max_age_days=0.0, max_rows=10, now=10_000_000.0)
        assert store.seen("$pending")
    finally:
        store.close()


@pytest.mark.parametrize("kwargs", [{"max_age_days": -1}, {"max_rows": 0}])
def test_prune_rejects_nonsense(tmp_path, kwargs):
    store = make_store(tmp_path)
    try:
        with pytest.raises(ValueError):
            store.prune(**kwargs)
    finally:
        store.close()


def test_open_creates_parent_directory(tmp_path):
    store = ProcessedStore(tmp_path / "nested" / "processed.db").open()
    try:
        store.claim("$e1", "!room:example.org")
    finally:
        store.close()
    assert (tmp_path / "nested" / "processed.db").exists()


def test_uses_a_separate_database_from_the_crypto_store(tmp_path):
    assert database_path(tmp_path).name == "processed.db"
    assert database_path(tmp_path).name != "crypto.db"


def test_database_is_sqlite_and_readable(tmp_path):
    store = make_store(tmp_path)
    store.claim("$e1", "!room:example.org")
    store.close()
    rows = sqlite3.connect(str(database_path(tmp_path))).execute(
        "SELECT event_id, room_id, status FROM processed_event"
    ).fetchall()
    assert rows == [("$e1", "!room:example.org", "processing")]
