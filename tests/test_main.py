import pytest


def test_fail_fast_exits_nonzero(monkeypatch):
    pytest.importorskip("mautrix")
    import src.main as main

    exits = []
    monkeypatch.setattr(main.os, "_exit", lambda code: exits.append(code))

    main._fail_fast("boom")

    assert exits == [1]
