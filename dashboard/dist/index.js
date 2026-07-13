/**
 * Hermes Factory dashboard plugin.
 *
 * Plain IIFE like the Kanban dashboard bundle: the host provides React,
 * design-system primitives, authenticated fetchJSON, and the plugin registry.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !SDK.React) return;

  var React = SDK.React;
  var h = React.createElement;
  var hooks = SDK.hooks || {};
  var useState = hooks.useState || React.useState;
  var useEffect = hooks.useEffect || React.useEffect;
  var Button = (SDK.components && SDK.components.Button) || function (props) {
    return h("button", props, props.children);
  };
  var API = "/api/plugins/factory";

  function request(path, options) {
    return SDK.fetchJSON(API + path, options);
  }

  function errorText(error) {
    var raw = error && error.message ? String(error.message) : String(error || "Request failed");
    try {
      var json = JSON.parse(raw.replace(/^\d{3}:\s*/, ""));
      return json.detail || raw;
    } catch (_ignore) { return raw; }
  }

  function tokenText(tokens) {
    if (!tokens) return "—";
    return String(tokens.charged || 0) + " / " + (tokens.budget == null ? "unbounded" : String(tokens.budget));
  }

  function Pill(props) {
    return h("span", { className: "factory-pill factory-pill--" + String(props.value || "unknown") }, props.value || "unknown");
  }

  function Empty(props) {
    return h("div", { className: "factory-empty" }, props.children);
  }

  function WaitingView() {
    var _a = useState([]), gates = _a[0], setGates = _a[1];
    var _b = useState(true), loading = _b[0], setLoading = _b[1];
    var _c = useState(""), message = _c[0], setMessage = _c[1];
    var _d = useState(""), busy = _d[0], setBusy = _d[1];
    function load() {
      setLoading(true);
      request("/waiting").then(function (items) { setGates(items); }).catch(function (err) {
        setMessage(errorText(err));
      }).then(function () { setLoading(false); });
    }
    useEffect(load, []);
    function decide(gate, action) {
      var reason = "";
      if (action === "reject") {
        reason = window.prompt("Reason for rejecting this gate:", "") || "";
        if (!reason.trim()) return;
      }
      var key = gate.instance_id + ":" + gate.step_id;
      setBusy(key + ":" + action);
      setMessage("");
      request("/" + action, {
        method: "POST",
        body: JSON.stringify({ instance: gate.instance_id, step: gate.step_id, reason: reason }),
      }).then(function () {
        setMessage(action === "approve" ? "Approval queued for the advancer." : "Rejection queued for the advancer.");
        load();
      }).catch(function (err) { setMessage(errorText(err)); }).then(function () { setBusy(""); });
    }
    if (loading) return h(Empty, null, "Loading gates…");
    return h("div", { className: "factory-stack" },
      h("div", { className: "factory-view-intro" },
        h("div", null, h("h2", null, "Waiting gates"), h("p", null, "Human decisions waiting across every Factory instance.")),
        h("a", { href: "/kanban", className: "factory-kanban-link" }, "Open Kanban →")
      ),
      message ? h("div", { className: "factory-message" }, message) : null,
      gates.length === 0 ? h(Empty, null, "no gates waiting") : gates.map(function (gate) {
        var key = gate.instance_id + ":" + gate.step_id;
        return h("article", { className: "factory-gate", key: key },
          h("div", { className: "factory-gate-main" },
            h("div", { className: "factory-eyebrow" }, gate.board + " · " + gate.recipe_id + "@" + gate.recipe_version),
            h("h3", null, gate.step_id),
            h("p", null, gate.blocked_reason || "Approval required")
          ),
          h("div", { className: "factory-actions" },
            h(Button, { size: "sm", disabled: busy === key + ":approve", onClick: function () { decide(gate, "approve"); } }, busy === key + ":approve" ? "Queuing…" : "Approve"),
            h(Button, { size: "sm", variant: "destructive", disabled: busy === key + ":reject", onClick: function () { decide(gate, "reject"); } }, busy === key + ":reject" ? "Queuing…" : "Reject")
          )
        );
      })
    );
  }

  function DetailDrawer(props) {
    var instance = props.instance;
    if (!instance) return null;
    return h("aside", { className: "factory-drawer" },
      h("div", { className: "factory-drawer-head" },
        h("div", null, h("div", { className: "factory-eyebrow" }, instance.board), h("h2", null, instance.recipe)),
        h("button", { className: "factory-close", onClick: props.onClose, "aria-label": "Close instance detail" }, "×")
      ),
      h("div", { className: "factory-detail-grid" },
        h("span", null, "Status"), h(Pill, { value: instance.status }),
        h("span", null, "Tokens"), h("strong", null, tokenText(instance.tokens)),
        h("span", null, "Blocked"), h("strong", null, instance.blocked_reason || "—")
      ),
      h("h3", null, "Steps & activations"),
      h("div", { className: "factory-step-list" }, (instance.steps || []).map(function (step) {
        return h("div", { className: "factory-step", key: step.step_id + ":" + step.activation },
          h("div", null, h("strong", null, step.step_id), h("span", null, " activation " + step.activation + " · " + step.primitive)),
          h("div", null, h(Pill, { value: step.state }), step.blocked_reason ? h("p", null, step.blocked_reason) : null)
        );
      })),
      h("h3", null, "Decisions"),
      (instance.decisions || []).length ? h("div", { className: "factory-decision-list" }, instance.decisions.map(function (item) {
        return h("div", { key: item.id }, item.stage_type + " · " + item.outcome);
      })) : h("p", { className: "factory-muted" }, "No Factory decisions recorded.")
    );
  }

  function InstancesView() {
    var _a = useState([]), instances = _a[0], setInstances = _a[1];
    var _b = useState(null), detail = _b[0], setDetail = _b[1];
    var _c = useState(""), error = _c[0], setError = _c[1];
    useEffect(function () { request("/instances").then(setInstances).catch(function (err) { setError(errorText(err)); }); }, []);
    function open(id) { request("/instances/" + encodeURIComponent(id)).then(setDetail).catch(function (err) { setError(errorText(err)); }); }
    return h("div", { className: "factory-stack" },
      h("div", { className: "factory-view-intro" }, h("div", null, h("h2", null, "Instances"), h("p", null, "Recipe progress, activations, and budget use."))),
      error ? h("div", { className: "factory-message" }, error) : null,
      instances.length ? h("div", { className: "factory-table-wrap" }, h("table", { className: "factory-table" },
        h("thead", null, h("tr", null, h("th", null, "Recipe"), h("th", null, "Board"), h("th", null, "State"), h("th", null, "Steps"), h("th", null, "Tokens"))),
        h("tbody", null, instances.map(function (item) { return h("tr", { key: item.id, onClick: function () { open(item.id); } },
          h("td", null, h("strong", null, item.recipe), h("span", null, item.id)), h("td", null, item.board), h("td", null, h(Pill, { value: item.status })),
          h("td", null, Object.keys(item.step_states || {}).map(function (state) { return state + " " + item.step_states[state]; }).join(" · ")),
          h("td", null, tokenText(item.tokens))
        ); }))
      )) : h(Empty, null, "No recipe instances yet."),
      h(DetailDrawer, { instance: detail, onClose: function () { setDetail(null); } })
    );
  }

  function DataView(props) {
    var _a = useState(null), rows = _a[0], setRows = _a[1];
    var _b = useState(""), error = _b[0], setError = _b[1];
    useEffect(function () { request(props.path).then(setRows).catch(function (err) { setError(errorText(err)); }); }, [props.path]);
    return h("div", { className: "factory-stack" },
      h("div", { className: "factory-view-intro" }, h("div", null, h("h2", null, props.title), h("p", null, props.description))),
      error ? h("div", { className: "factory-message" }, error) : null,
      rows === null ? h(Empty, null, "Loading…") : rows.length === 0 ? h(Empty, null, props.empty) : h("div", { className: "factory-data-list" }, rows.map(function (row, index) {
        return h("article", { className: "factory-data-card", key: (row.name || row.seat || row.executor || row.task || index) },
          Object.keys(row).map(function (key) { return h("div", { key: key }, h("span", null, key.replace(/_/g, " ")), h("strong", null, String(row[key] == null ? "—" : row[key]))); })
        );
      }))
    );
  }

  function FactoryPage() {
    var _a = useState("waiting"), tab = _a[0], setTab = _a[1];
    var tabs = [["waiting", "Waiting gates"], ["instances", "Instances"], ["seats", "Seats"], ["costs", "Costs"]];
    var body = tab === "waiting" ? h(WaitingView) : tab === "instances" ? h(InstancesView) : tab === "seats"
      ? h(DataView, { path: "/seats", title: "Seats", description: "Configured Factory operators and their current pause state.", empty: "No seats configured." })
      : h(DataView, { path: "/costs", title: "Costs", description: "Completed-run cost rollup for the last day.", empty: "No costs recorded today." });
    return h("main", { className: "hermes-factory" },
      h("nav", { className: "factory-tabs", "aria-label": "Factory views" }, tabs.map(function (item) {
        return h("button", { key: item[0], className: tab === item[0] ? "is-active" : "", onClick: function () { setTab(item[0]); } }, item[1]);
      })), body
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("factory", FactoryPage);
  }
})();
