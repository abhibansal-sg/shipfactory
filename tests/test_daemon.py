import sys
import types

from factory import daemon


def test_tick_dispatches_reaps_and_optionally_ticks(monkeypatch):
    calls = []
    kanban = types.ModuleType("hermes_cli.kanban_db")
    kanban.dispatch_once = lambda conn, **kw: calls.append(("dispatch", kw)) or "dispatched"
    hermes = types.ModuleType("hermes_cli")
    hermes.kanban_db = kanban
    spawn = types.ModuleType("factory.spawn")
    spawn.factory_spawn = object()
    spawn.reap_finished = lambda: [{"task_id": "x"}]
    watchdog = types.ModuleType("factory.watchdog")
    watchdog.tick = lambda conn, board=None: calls.append(("watchdog", board)) or "watched"
    sync = types.ModuleType("factory.github_sync")
    sync.tick = lambda board=None: calls.append(("sync", board)) or "synced"
    for name, module in (("hermes_cli", hermes), ("hermes_cli.kanban_db", kanban),
                         ("factory.spawn", spawn), ("factory.watchdog", watchdog),
                         ("factory.github_sync", sync)):
        monkeypatch.setitem(sys.modules, name, module)
    # `from factory import X` resolves the attribute on the factory PACKAGE,
    # not sys.modules, once the real submodule has been imported by another
    # test — patch both so this test is order-independent (integration fix
    # 07-12: full-suite run imports real github_sync/watchdog/spawn first).
    import factory as _factory_pkg
    monkeypatch.setattr(_factory_pkg, "spawn", spawn, raising=False)
    monkeypatch.setattr(_factory_pkg, "watchdog", watchdog, raising=False)
    monkeypatch.setattr(_factory_pkg, "github_sync", sync, raising=False)
    result = daemon.tick(object(), board="board", sync=True)
    assert result == {"dispatch": "dispatched", "reaped": [{"task_id": "x"}], "watchdog": "watched", "sync": "synced"}
    assert calls[0][0] == "dispatch"


def test_run_once_returns_tick(monkeypatch):
    monkeypatch.setattr(daemon, "tick", lambda *args, **kwargs: {"ok": True})
    assert daemon.run(object(), once=True) == {"ok": True}
