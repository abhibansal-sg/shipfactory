import sys
import types

from shipfactory import daemon


def test_tick_dispatches_reaps_and_optionally_ticks(monkeypatch):
    calls = []
    kanban = types.ModuleType("hermes_cli.kanban_db")
    kanban.dispatch_once = lambda conn, **kw: calls.append(("dispatch", kw)) or "dispatched"
    hermes = types.ModuleType("hermes_cli")
    hermes.kanban_db = kanban
    spawn = types.ModuleType("shipfactory.spawn")
    spawn.shipfactory_spawn = object()
    # Finding #23: tick reaps BEFORE dispatch (finalize exited harnesses so the
    # claim watchdog can't fuse them) and once after. First reap returns the
    # finished worker; second returns nothing.
    _reaps = iter([[{"task_id": "x"}], []])
    spawn.reap_finished = lambda: calls.append(("reap",)) or next(_reaps, [])
    watchdog = types.ModuleType("factory.watchdog")
    watchdog.tick = lambda conn, board=None: calls.append(("watchdog", board)) or "watched"
    sync = types.ModuleType("factory.github_sync")
    sync.tick = lambda board=None: calls.append(("sync", board)) or "synced"
    for name, module in (("hermes_cli", hermes), ("hermes_cli.kanban_db", kanban),
                         ("shipfactory.spawn", spawn), ("factory.watchdog", watchdog),
                         ("factory.github_sync", sync)):
        monkeypatch.setitem(sys.modules, name, module)
    # `from shipfactory import X` resolves the attribute on the factory PACKAGE,
    # not sys.modules, once the real submodule has been imported by another
    # test — patch both so this test is order-independent (integration fix
    # 07-12: full-suite run imports real github_sync/watchdog/spawn first).
    import shipfactory as _factory_pkg
    monkeypatch.setattr(_factory_pkg, "spawn", spawn, raising=False)
    monkeypatch.setattr(_factory_pkg, "watchdog", watchdog, raising=False)
    monkeypatch.setattr(_factory_pkg, "github_sync", sync, raising=False)
    result = daemon.tick(object(), board="board", sync=True)
    assert result == {"dispatch": "dispatched", "reaped": [{"task_id": "x"}], "watchdog": "watched", "sync": "synced"}
    assert calls[0][0] == "reap" and calls[1][0] == "dispatch"


def test_run_once_returns_tick(monkeypatch):
    monkeypatch.setattr(daemon, "tick", lambda *args, **kwargs: {"ok": True})
    assert daemon.run(object(), once=True) == {"ok": True}


def test_graph_runner_mode_skips_legacy_control_plane(monkeypatch):
    calls = []
    kanban = types.ModuleType("hermes_cli.kanban_db")

    def forbidden_legacy(*_args, **_kwargs):
        raise AssertionError("graph mode must not enter the legacy control plane")

    kanban.dispatch_once = forbidden_legacy
    hermes = types.ModuleType("hermes_cli")
    hermes.kanban_db = kanban
    spawn = types.ModuleType("shipfactory.spawn")
    spawn.shipfactory_spawn = object()
    spawn.reap_finished = lambda: calls.append("reap") or []
    config = types.ModuleType("shipfactory.config")
    config.recipe_runtime_config = lambda _recipes: {
        "max_workers": 2,
        "runner_mode": "graph",
    }
    environments = types.ModuleType("shipfactory.environments")
    environments.restore_materializations = forbidden_legacy
    environments.reap_materializations = forbidden_legacy
    environments.tick = forbidden_legacy
    advancer = types.ModuleType("shipfactory.recipes.advancer")
    advancer.apply_events = forbidden_legacy
    advancer.deliver_outbox = forbidden_legacy
    advancer.reconcile_root_collectors = forbidden_legacy
    selector = types.ModuleType("shipfactory.recipes.selector_stage")
    selector.run_stage = forbidden_legacy
    cfg = types.SimpleNamespace(company="test", recipes={"enabled": True})
    monkeypatch.setattr(daemon, "validate_recipe_mode", lambda **_kwargs: cfg)
    monkeypatch.setattr(
        daemon,
        "_tick_graph_v1",
        lambda **kwargs: calls.append(("graph", kwargs)) or {"advanced": 1},
    )
    watchdog = types.ModuleType("shipfactory.watchdog")
    watchdog.tick = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("graph mode must not tick the legacy watchdog")
    )
    sync = types.ModuleType("shipfactory.github_sync")
    sync.tick = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("graph mode must not run legacy sync")
    )
    for name, module in (
        ("hermes_cli", hermes),
        ("hermes_cli.kanban_db", kanban),
        ("shipfactory.spawn", spawn),
        ("shipfactory.config", config),
        ("shipfactory.environments", environments),
        ("shipfactory.recipes.advancer", advancer),
        ("shipfactory.recipes.selector_stage", selector),
        ("shipfactory.watchdog", watchdog),
        ("shipfactory.github_sync", sync),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    import shipfactory as factory_package
    monkeypatch.setattr(factory_package, "spawn", spawn, raising=False)
    monkeypatch.setattr(factory_package, "environments", environments, raising=False)
    monkeypatch.setattr(factory_package, "watchdog", watchdog, raising=False)
    monkeypatch.setattr(factory_package, "github_sync", sync, raising=False)

    result = daemon.tick(object(), board="graph-board", sync=True)

    assert result == {
        "dispatch": None,
        "reaped": [],
        "graph": {"advanced": 1},
        "watchdog": None,
    }
    assert calls == ["reap", ("graph", {"board": "graph-board", "max_workers": 2}), "reap"]


def test_tick_runs_selector_after_advancer_stages(monkeypatch):
    calls = []
    recipes_cfg = {
        "enabled": True,
        "dispatcher_max_in_progress": 4,
        "execution_profiles": {"standard": {}},
        "selector": {"enabled": True},
    }
    cfg = types.SimpleNamespace(company="test", recipes=recipes_cfg)
    config = types.ModuleType("shipfactory.config")
    config.FactoryConfigError = ValueError
    config.load_seats = lambda: cfg
    config.selector_config = lambda recipes: {"enabled": True}
    advancer = types.ModuleType("shipfactory.recipes.advancer")
    advancer.startup_guard = lambda config: calls.append("guard")
    advancer.apply_events = lambda conn, profiles, board=None: calls.append("events") or 1
    advancer.deliver_outbox = lambda conn, board=None: calls.append("outbox") or 2
    advancer.reconcile_root_collectors = lambda conn, board=None: calls.append("roots") or 3
    selector = types.ModuleType("shipfactory.recipes.selector_stage")
    selector.run_stage = lambda conn, board: calls.append("selector") or {
        "leased": 1, "instantiated": 1, "parked": 0, "skipped": 0,
    }
    kanban = types.ModuleType("hermes_cli.kanban_db")
    kanban.dispatch_once = lambda conn, **kwargs: calls.append("dispatch") or "dispatched"
    hermes = types.ModuleType("hermes_cli")
    hermes.kanban_db = kanban
    spawn = types.ModuleType("shipfactory.spawn")
    spawn.shipfactory_spawn = object()
    spawn.reap_finished = lambda: []
    watchdog = types.ModuleType("factory.watchdog")
    watchdog.tick = lambda conn, board=None: None
    for name, module in (
        ("shipfactory.config", config), ("shipfactory.recipes.advancer", advancer),
        ("shipfactory.recipes.selector_stage", selector), ("hermes_cli", hermes),
        ("hermes_cli.kanban_db", kanban), ("shipfactory.spawn", spawn),
        ("factory.watchdog", watchdog),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    import shipfactory as factory_package
    monkeypatch.setattr(factory_package, "spawn", spawn, raising=False)
    monkeypatch.setattr(factory_package, "watchdog", watchdog, raising=False)

    result = daemon.tick(object(), board="test")

    assert result["selector"] == {
        "leased": 1, "instantiated": 1, "parked": 0, "skipped": 0,
    }
    assert calls.index("events") < calls.index("outbox") < calls.index("roots")
    assert calls.index("roots") < calls.index("selector") < calls.index("dispatch")
