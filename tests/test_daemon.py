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


def test_tick_runs_selector_after_advancer_stages(monkeypatch):
    calls = []
    recipes_cfg = {
        "enabled": True,
        "dispatcher_max_in_progress": 4,
        "execution_profiles": {"standard": {}},
        "selector": {"enabled": True},
    }
    cfg = types.SimpleNamespace(company="test", recipes=recipes_cfg)
    config = types.ModuleType("factory.config")
    config.FactoryConfigError = ValueError
    config.load_seats = lambda: cfg
    config.selector_config = lambda recipes: {"enabled": True}
    advancer = types.ModuleType("factory.recipes.advancer")
    advancer.startup_guard = lambda config: calls.append("guard")
    advancer.apply_events = lambda conn, profiles, board=None: calls.append("events") or 1
    advancer.deliver_outbox = lambda: calls.append("outbox") or 2
    advancer.reconcile_root_collectors = lambda conn: calls.append("roots") or 3
    selector = types.ModuleType("factory.recipes.selector_stage")
    selector.run_stage = lambda conn, board: calls.append("selector") or {
        "leased": 1, "instantiated": 1, "parked": 0, "skipped": 0,
    }
    kanban = types.ModuleType("hermes_cli.kanban_db")
    kanban.dispatch_once = lambda conn, **kwargs: calls.append("dispatch") or "dispatched"
    hermes = types.ModuleType("hermes_cli")
    hermes.kanban_db = kanban
    spawn = types.ModuleType("factory.spawn")
    spawn.factory_spawn = object()
    spawn.reap_finished = lambda: []
    watchdog = types.ModuleType("factory.watchdog")
    watchdog.tick = lambda conn, board=None: None
    for name, module in (
        ("factory.config", config), ("factory.recipes.advancer", advancer),
        ("factory.recipes.selector_stage", selector), ("hermes_cli", hermes),
        ("hermes_cli.kanban_db", kanban), ("factory.spawn", spawn),
        ("factory.watchdog", watchdog),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    import factory as factory_package
    monkeypatch.setattr(factory_package, "spawn", spawn, raising=False)
    monkeypatch.setattr(factory_package, "watchdog", watchdog, raising=False)

    result = daemon.tick(object(), board="test")

    assert result["selector"] == {
        "leased": 1, "instantiated": 1, "parked": 0, "skipped": 0,
    }
    assert calls.index("events") < calls.index("outbox") < calls.index("roots")
    assert calls.index("roots") < calls.index("selector") < calls.index("dispatch")
