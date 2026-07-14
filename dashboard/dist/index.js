/**
 * Hermes Factory dashboard plugin.
 *
 * Plain IIFE like the Kanban dashboard bundle. The host owns React, design
 * system primitives, authenticated fetchJSON, theme tokens, and routing.
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
  var useCallback = hooks.useCallback || React.useCallback;
  var useMemo = hooks.useMemo || React.useMemo;
  var components = SDK.components || {};
  var Button = components.Button || function (props) { return h("button", props, props.children); };
  var Badge = components.Badge || function (props) { return h("span", props, props.children); };
  var Card = components.Card || function (props) { return h("div", props, props.children); };
  var CardContent = components.CardContent || function (props) { return h("div", props, props.children); };
  var API = "/api/plugins/factory";
  var POLL_MS = 20000;

  function request(path, options) {
    return SDK.fetchJSON(API + path, options);
  }

  function errorText(error) {
    var raw = error && error.message ? String(error.message) : String(error || "Request failed");
    var body = raw.replace(/^\d{3}:\s*/, "");
    try {
      var json = JSON.parse(body);
      if (typeof json.detail === "string") return json.detail;
      if (json.detail && typeof json.detail.message === "string") return json.detail.message;
    } catch (_ignore) { /* Return the readable raw body. */ }
    return body || raw;
  }

  function formatNumber(value) {
    var number = Number(value || 0);
    try { return new Intl.NumberFormat().format(number); }
    catch (_ignore) { return String(number); }
  }

  function formatDuration(seconds) {
    var total = Number(seconds || 0);
    if (total < 60) return Math.round(total) + "s";
    if (total < 3600) return Math.round(total / 60) + "m";
    return (total / 3600).toFixed(1) + "h";
  }

  function fallbackTimeAgo(value) {
    if (!value) return "—";
    var raw = typeof value === "number" && value < 1000000000000 ? value * 1000 : value;
    var stamp = new Date(raw).getTime();
    if (!Number.isFinite(stamp)) return "—";
    var seconds = Math.max(0, Math.floor((Date.now() - stamp) / 1000));
    if (seconds < 10) return "just now";
    if (seconds < 60) return seconds + "s ago";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
    if (seconds < 2592000) return Math.floor(seconds / 86400) + "d ago";
    return Math.floor(seconds / 2592000) + "mo ago";
  }

  function timeAgo(value) {
    if (!value) return "—";
    if (SDK.utils && SDK.utils.timeAgo) {
      var normalized = value;
      if (typeof value === "string") {
        var parsed = new Date(value).getTime();
        if (Number.isFinite(parsed)) normalized = Math.floor(parsed / 1000);
      }
      return SDK.utils.timeAgo(normalized);
    }
    return fallbackTimeAgo(value);
  }

  function normalizedState(value) {
    return String(value || "unknown").toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  }

  function StatePill(props) {
    var value = props.value || "unknown";
    var state = normalizedState(value);
    var tones = {
      running: "success", ready: "success", active: "success", done: "success",
      completed: "success", approved: "success", approve: "success", delivered: "success",
      waiting: "warning", waiting_gate: "warning", pending: "warning", paused: "warning",
      blocked: "destructive", failed: "destructive", rejected: "destructive",
      reject: "destructive", cancelled: "destructive", cancelling: "destructive",
    };
    return h(Badge, {
      className: "factory-pill text-xs tabular-nums",
      tone: tones[state] || "secondary",
      title: props.title || String(value),
    }, props.children || String(value).replace(/_/g, " "));
  }

  function MonoChip(props) {
    return h("span", {
      className: "font-mono-ui max-w-full truncate text-xs text-text-secondary",
      title: props.title,
    }, props.children);
  }

  function Ago(props) {
    return h("time", {
      className: props.className || "whitespace-nowrap text-xs text-text-tertiary",
      dateTime: props.value || undefined,
      title: props.value || "",
    }, timeAgo(props.value));
  }

  function Spinner(props) {
    return h("span", { className: "factory-spinner inline-flex items-center gap-2", "aria-hidden": "true" },
      h("span", null),
      props.label ? h("span", { className: "whitespace-nowrap" }, props.label) : null
    );
  }

  function LoadingState(props) {
    return h(Card, { role: "status" },
      h(CardContent, { className: "flex flex-col gap-3 p-4 text-sm text-muted-foreground" },
        h(Spinner, { label: props.label || "Loading…" }),
        h("div", { className: "factory-skeleton-stack", "aria-hidden": "true" },
          h("span", null), h("span", null), h("span", null)
        )
      )
    );
  }

  function EmptyState(props) {
    return h(Card, { className: "factory-empty" },
      h(CardContent, { className: "flex flex-col items-center gap-2 py-12 text-center text-sm text-muted-foreground" },
        h("span", { className: "text-primary", "aria-hidden": "true" }, "◇"),
        h("strong", { className: "font-mondwest normal-case text-sm font-medium text-foreground" }, props.title),
        h("span", { className: "max-w-2xl text-xs text-text-tertiary" }, props.description)
      )
    );
  }

  function ErrorState(props) {
    return h("div", { className: "flex flex-col gap-3 border border-destructive/30 bg-destructive/10 p-4 text-sm sm:flex-row sm:items-center sm:justify-between", role: "alert" },
      h("div", null,
        h("strong", { className: "font-mondwest normal-case text-sm font-medium text-destructive" }, props.title || "Factory data could not be loaded"),
        h("p", { className: "mt-1 text-xs text-text-secondary" }, props.message)
      ),
      h(Button, { size: "sm", ghost: true, onClick: props.onRetry }, "Retry")
    );
  }

  function Toast(props) {
    if (!props.toast) return null;
    return h("div", {
      className: "factory-toast flex items-center gap-2 border p-3 text-sm " + (props.toast.ok ? "border-success/30 bg-success/10 text-success" : "border-destructive/30 bg-destructive/10 text-destructive"),
      role: props.toast.ok ? "status" : "alert",
    },
      h("span", { className: "font-bold" }, props.toast.ok ? "✓" : "!"),
      h("span", null, props.toast.text),
      h("button", { type: "button", className: "ml-auto cursor-pointer text-current", onClick: props.onClose, "aria-label": "Dismiss message" }, "×")
    );
  }

  function ViewHeading(props) {
    return h("div", { className: "flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between" },
      h("div", null,
        h("h2", { className: "font-mondwest text-display text-base tracking-wider text-foreground" }, props.title),
        h("p", { className: "mt-1 text-xs text-text-tertiary" }, props.description)
      ),
      props.action || null
    );
  }

  function SectionHeading(props) {
    return h("div", { className: "flex items-end justify-between gap-3" },
      h("div", null,
        h("h3", { className: "font-mondwest text-display text-base tracking-wider text-foreground" }, props.title),
        props.description ? h("p", { className: "mt-1 text-xs text-text-tertiary" }, props.description) : null
      ),
      props.meta ? h("span", { className: "font-mono-ui shrink-0 text-xs text-text-secondary" }, props.meta) : null
    );
  }

  function usePollingResource(path, refreshKey) {
    var _a = useState(null), data = _a[0], setData = _a[1];
    var _b = useState(true), loading = _b[0], setLoading = _b[1];
    var _c = useState(""), error = _c[0], setError = _c[1];
    var _d = useState(null), loadedAt = _d[0], setLoadedAt = _d[1];

    var load = useCallback(function (quiet) {
      if (!quiet && data === null) setLoading(true);
      return request(path).then(function (payload) {
        setData(payload);
        setError("");
        setLoadedAt(new Date().toISOString());
        return payload;
      }).catch(function (err) {
        setError(errorText(err));
        throw err;
      }).finally(function () { setLoading(false); });
    }, [path, data === null]);

    useEffect(function () {
      var active = true;
      load(false).catch(function () {});
      var timer = window.setInterval(function () {
        if (active && document.visibilityState !== "hidden") load(true).catch(function () {});
      }, POLL_MS);
      return function () { active = false; window.clearInterval(timer); };
    }, [path, refreshKey]);

    return { data: data, loading: loading, error: error, loadedAt: loadedAt, reload: function () { return load(false); }, setData: setData };
  }

  function useReportViewMeta(props, resource, boards) {
    useEffect(function () {
      if (!resource.loadedAt || !props.onMeta) return;
      var unique = Array.from(new Set((boards || []).filter(Boolean)));
      props.onMeta({
        loadedAt: resource.loadedAt,
        board: unique.length === 0 ? "All boards" : unique.length === 1 ? unique[0] : unique.length + " boards",
      });
    }, [resource.loadedAt, (boards || []).join("|")]);
  }

  function GateCard(props) {
    var gate = props.gate;
    var key = gate.instance_id + ":" + gate.step_id;
    var busy = props.busy.indexOf(key + ":") === 0;
    var position = gate.step_position && gate.step_total
      ? "Step " + gate.step_position + " of " + gate.step_total
      : "Approval step";
    return h(Card, { className: "factory-gate" },
      h(CardContent, { className: "flex flex-col gap-3 p-4" },
        h("div", { className: "flex items-center justify-between gap-3" },
          h(StatePill, { value: "waiting" }, "waiting for decision"),
          h(Ago, { value: gate.updated_at || gate.instance_updated_at })
        ),
        h("div", { className: "flex flex-col items-start gap-3 sm:flex-row sm:items-end sm:justify-between" },
          h("div", { className: "min-w-0" },
            h("div", { className: "font-mondwest text-display text-xs tracking-[0.12em] text-text-tertiary" },
              gate.board + " · " + gate.recipe_id + "@" + gate.recipe_version + " · " + position + " · activation " + gate.activation
            ),
            h("h3", { className: "mt-1 font-mondwest normal-case text-sm font-medium text-foreground" }, gate.step_id.replace(/[-_]/g, " ")),
            h("p", { className: "mt-1 text-xs leading-relaxed text-text-secondary" }, gate.blocked_reason || gate.instance_blocked_reason || "An operator decision is required before this recipe can advance."),
            h("div", { className: "mt-2 flex flex-wrap items-center gap-2" },
              h(MonoChip, null, gate.instance_id),
              h(MonoChip, null, gate.primitive)
            )
          ),
          h("div", { className: "flex shrink-0 gap-2" },
            h(Button, {
              size: "sm", ghost: true,
              disabled: busy,
              onClick: function () { props.onDecide(gate, "approve"); },
            }, props.busy === key + ":approve" ? h(Spinner, { label: "Approving" }) : "Approve"),
            h(Button, {
              size: "sm", ghost: true, destructive: true,
              disabled: busy,
              onClick: function () { props.onDecide(gate, "reject"); },
            }, props.busy === key + ":reject" ? h(Spinner, { label: "Rejecting" }) : "Reject")
          )
        )
      )
    );
  }

  function WaitingView(props) {
    var resource = usePollingResource("/waiting", props.refreshKey);
    var _a = useState(""), busy = _a[0], setBusy = _a[1];
    var _b = useState(null), toast = _b[0], setToast = _b[1];
    var gates = resource.data || [];
    useReportViewMeta(props, resource, gates.map(function (gate) { return gate.board; }));

    function decide(gate, action) {
      var reason = "";
      if (action === "reject") {
        reason = window.prompt("Reason for rejecting this gate:", "") || "";
        if (!reason.trim()) return;
      }
      var key = gate.instance_id + ":" + gate.step_id;
      var previous = gates.slice();
      setBusy(key + ":" + action);
      setToast(null);
      resource.setData(gates.filter(function (item) {
        return item.instance_id !== gate.instance_id || item.step_id !== gate.step_id;
      }));
      request("/" + action, {
        method: "POST",
        body: JSON.stringify({ instance: gate.instance_id, step: gate.step_id, reason: reason }),
      }).then(function () {
        setToast({ ok: true, text: action === "approve" ? "Approval queued for the advancer." : "Rejection queued for the advancer." });
        resource.reload().catch(function () {});
      }).catch(function (err) {
        resource.setData(previous);
        setToast({ ok: false, text: errorText(err) });
      }).finally(function () { setBusy(""); });
    }

    return h("section", { className: "factory-view flex min-w-0 flex-col gap-4" },
      h(ViewHeading, {
        title: "Waiting gates",
        description: "Human decisions waiting across every Factory recipe.",
        action: h("a", { href: "/kanban", className: "font-mondwest text-display text-xs tracking-[0.1em] text-text-secondary hover:text-midground" }, "Open Kanban →"),
      }),
      h(Toast, { toast: toast, onClose: function () { setToast(null); } }),
      resource.error && resource.data !== null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) : null,
      resource.loading && resource.data === null ? h(LoadingState, { label: "Loading waiting gates…" }) :
      resource.error && resource.data === null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) :
      gates.length === 0 ? h(EmptyState, {
        title: "No gates are waiting",
        description: "Approval gates appear here when a running recipe needs an operator decision.",
      }) : h("div", { className: "grid gap-3" }, gates.map(function (gate) {
        return h(GateCard, { key: gate.instance_id + ":" + gate.step_id, gate: gate, busy: busy, onDecide: decide });
      }))
    );
  }

  function BudgetProgress(props) {
    var tokens = props.tokens || {};
    var charged = Number(tokens.charged || 0);
    var budget = tokens.budget == null ? null : Number(tokens.budget);
    var percent = budget && budget > 0 ? Math.min(100, Math.round(charged / budget * 100)) : null;
    return h("div", { className: "factory-budget flex flex-col gap-2" },
      h("div", { className: "flex justify-between gap-3 font-mono-ui text-xs tabular-nums text-text-tertiary" },
        h("span", { className: "text-foreground" }, formatNumber(charged) + " tokens"),
        h("span", null, budget == null ? "unbounded" : percent + "% of " + formatNumber(budget))
      ),
      h("progress", {
        value: budget == null ? 0 : Math.min(charged, budget),
        max: budget == null ? 1 : Math.max(budget, 1),
        className: budget == null ? "is-unbounded" : "",
      }, percent == null ? "unbounded" : percent + "%")
    );
  }

  function StepStateSummary(props) {
    var states = props.states || {};
    var keys = Object.keys(states);
    if (keys.length === 0) return h("span", { className: "text-text-tertiary" }, "No steps");
    return h("div", { className: "flex flex-wrap items-center gap-2" }, keys.map(function (state) {
      return h(StatePill, { key: state, value: state }, states[state] + " " + state.replace(/_/g, " "));
    }));
  }

  function InstanceDrawer(props) {
    var detail = props.detail;
    useEffect(function () {
      if (!props.open) return undefined;
      function onKey(event) { if (event.key === "Escape") props.onClose(); }
      window.addEventListener("keydown", onKey);
      return function () { window.removeEventListener("keydown", onKey); };
    }, [props.open, props.onClose]);
    if (!props.open) return null;

    return h("div", { className: "factory-drawer-shade", onClick: props.onClose },
      h("aside", { className: "factory-drawer bg-card text-foreground", onClick: function (event) { event.stopPropagation(); }, "aria-label": "Instance detail" },
        h("div", { className: "factory-drawer-head flex items-center justify-between gap-4 border-b border-border" },
          h("div", null,
            h("span", { className: "font-mondwest text-display text-xs tracking-[0.12em] text-text-tertiary" }, detail ? detail.board : "Factory instance"),
            h("h2", { className: "mt-1 font-mono-ui text-sm font-medium" }, detail ? detail.recipe : "Loading instance…")
          ),
          h(Button, { type: "button", size: "xs", ghost: true, onClick: props.onClose, "aria-label": "Close instance detail" }, "×")
        ),
        props.loading ? h(LoadingState, { label: "Loading instance detail…" }) :
        props.error ? h(ErrorState, { message: props.error, onRetry: props.onRetry }) :
        detail ? h("div", { className: "factory-drawer-body" },
          detail.blocked_reason ? h("div", { className: "flex gap-2 border border-destructive/30 bg-destructive/10 p-3 text-sm" },
            h("strong", { className: "text-destructive" }, "Blocked"), h("span", null, detail.blocked_reason)
          ) : null,
          h("div", { className: "factory-meta-panel border border-border bg-card p-3 text-xs" },
            h("div", null, h("span", { className: "text-text-tertiary" }, "Status"), h(StatePill, { value: detail.status })),
            h("div", null, h("span", { className: "text-text-tertiary" }, "Instance"), h(MonoChip, null, detail.id)),
            h("div", null, h("span", { className: "text-text-tertiary" }, "Created"), h(Ago, { value: detail.created_at })),
            h("div", null, h("span", { className: "text-text-tertiary" }, "Updated"), h(Ago, { value: detail.updated_at }))
          ),
          h(BudgetProgress, { tokens: detail.tokens }),
          h("div", { className: "flex items-center justify-between border-l-2 border-primary bg-primary/10 p-3 text-sm" },
            h("span", { className: "text-text-secondary" }, "Activations"),
            h("strong", { className: "font-mono-ui" }, formatNumber(detail.activation_count) + " / " + (detail.budgets && detail.budgets.max_activations != null ? formatNumber(detail.budgets.max_activations) : "unbounded"))
          ),
          h("section", { className: "flex flex-col gap-2" },
            h(SectionHeading, { title: "Steps", description: "Every activation in recipe order.", meta: (detail.steps || []).length + " rows" }),
            (detail.steps || []).length === 0 ? h(EmptyState, { title: "No steps", description: "This instance has not created a step activation yet." }) :
            h("div", { className: "overflow-x-auto border border-border bg-card" },
              h("table", { className: "factory-step-table w-full font-mondwest normal-case text-sm" },
                h("thead", null, h("tr", { className: "border-b border-border text-xs text-muted-foreground" },
                  h("th", { className: "px-3 py-2 text-left font-medium" }, "Step"), h("th", { className: "px-3 py-2 text-left font-medium" }, "Primitive"), h("th", { className: "px-3 py-2 text-left font-medium" }, "Activation"), h("th", { className: "px-3 py-2 text-left font-medium" }, "State"), h("th", { className: "px-3 py-2 text-left font-medium" }, "Kanban task")
                )),
                h("tbody", null, detail.steps.map(function (step) {
                  return h("tr", { key: step.step_id + ":" + step.activation, className: "border-b border-border/50" },
                    h("td", { className: "px-3 py-2" },
                      h("strong", { className: "font-medium" }, step.step_id),
                      step.blocked_reason ? h("span", { className: "block max-w-sm text-xs leading-relaxed text-destructive" }, step.blocked_reason) : null
                    ),
                    h("td", { className: "px-3 py-2" }, h(MonoChip, null, step.primitive)),
                    h("td", { className: "px-3 py-2 font-mono-ui text-xs" }, "#" + step.activation),
                    h("td", { className: "px-3 py-2" }, h(StatePill, { value: step.state })),
                    h("td", { className: "px-3 py-2" }, step.kanban_task_id
                      ? h("a", { href: "/kanban?task=" + encodeURIComponent(step.kanban_task_id), className: "font-mono-ui text-xs text-primary hover:underline" }, step.kanban_task_id)
                      : h("span", { className: "text-text-tertiary" }, "—"))
                  );
                }))
              )
            )
          ),
          h("section", { className: "flex flex-col gap-2" },
            h(SectionHeading, { title: "Decisions", description: "Recorded policy and gate outcomes.", meta: (detail.decisions || []).length + " total" }),
            (detail.decisions || []).length === 0 ? h(EmptyState, { title: "No decisions recorded", description: "Review and approval outcomes will appear here." }) :
            h("div", { className: "grid gap-2" }, detail.decisions.map(function (decision) {
              return h("article", { key: decision.id, className: "border-l-2 border-primary/40 bg-card p-3" },
                h("div", { className: "flex flex-wrap items-center gap-2" },
                  h(StatePill, { value: decision.outcome }),
                  h("strong", { className: "font-mondwest normal-case text-sm font-medium" }, decision.stage_id),
                  h(MonoChip, null, decision.stage_type)
                ),
                h("div", { className: "mt-2 flex items-center justify-between gap-2 text-xs text-text-tertiary" },
                  h("span", null, decision.seat ? "@" + decision.seat : "unassigned"),
                  h(Ago, { value: decision.at })
                ),
                decision.body ? h("p", { className: "mt-2 text-xs leading-relaxed text-text-secondary" }, decision.body) : null
              );
            }))
          )
        ) : null
      )
    );
  }

  function InstancesView(props) {
    var resource = usePollingResource("/instances", props.refreshKey);
    var _a = useState(null), selectedId = _a[0], setSelectedId = _a[1];
    var _b = useState(null), detail = _b[0], setDetail = _b[1];
    var _c = useState(false), detailLoading = _c[0], setDetailLoading = _c[1];
    var _d = useState(""), detailError = _d[0], setDetailError = _d[1];
    var instances = resource.data || [];
    useReportViewMeta(props, resource, instances.map(function (item) { return item.board; }));

    function loadDetail(id) {
      setSelectedId(id);
      setDetailLoading(true);
      setDetailError("");
      return request("/instances/" + encodeURIComponent(id)).then(setDetail).catch(function (err) {
        setDetailError(errorText(err));
      }).finally(function () { setDetailLoading(false); });
    }

    function openFromKey(event, id) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); loadDetail(id); }
    }

    return h("section", { className: "factory-view flex min-w-0 flex-col gap-4" },
      h(ViewHeading, { title: "Instances", description: "Recipe progress, activations, and budget consumption." }),
      resource.error && resource.data !== null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) : null,
      resource.loading && resource.data === null ? h(LoadingState, { label: "Loading recipe instances…" }) :
      resource.error && resource.data === null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) :
      instances.length === 0 ? h(EmptyState, { title: "No recipe instances yet", description: "Instances appear after a task is matched to an active recipe." }) :
      h("div", { className: "overflow-x-auto border border-border bg-card" },
        h("table", { className: "factory-instance-table w-full font-mondwest normal-case text-sm" },
          h("thead", null, h("tr", { className: "border-b border-border text-xs text-muted-foreground" },
            h("th", { className: "px-3 py-2 text-left font-medium" }, "Recipe / instance"), h("th", { className: "px-3 py-2 text-left font-medium" }, "Board"), h("th", { className: "px-3 py-2 text-left font-medium" }, "State"), h("th", { className: "px-3 py-2 text-left font-medium" }, "Steps"),
            h("th", { className: "px-3 py-2 text-left font-medium" }, "Budget"), h("th", { className: "px-3 py-2 text-left font-medium" }, "Activations"), h("th", { className: "px-3 py-2 text-left font-medium" }, "Created"), h("th", { className: "px-3 py-2 text-left font-medium" }, "Updated")
          )),
          h("tbody", null, instances.map(function (item) {
            return h("tr", {
              key: item.id, tabIndex: 0, role: "button",
              className: "border-b border-border/50 transition-colors hover:bg-secondary/20",
              onClick: function () { loadDetail(item.id); },
              onKeyDown: function (event) { openFromKey(event, item.id); },
            },
              h("td", { className: "px-3 py-2" }, h("div", { className: "font-medium text-foreground" }, item.recipe), h("span", { className: "mt-1 block font-mono-ui text-xs text-text-tertiary" }, item.id)),
              h("td", { className: "px-3 py-2" }, item.board),
              h("td", { className: "px-3 py-2" }, h(StatePill, { value: item.status })),
              h("td", { className: "px-3 py-2" }, h(StepStateSummary, { states: item.step_states })),
              h("td", { className: "factory-budget-cell px-3 py-2" }, h(BudgetProgress, { tokens: item.tokens })),
              h("td", { className: "px-3 py-2 font-mono-ui text-xs" }, formatNumber(item.activation_count) + " / " + (item.budgets && item.budgets.max_activations != null ? formatNumber(item.budgets.max_activations) : "∞")),
              h("td", { className: "px-3 py-2" }, h(Ago, { value: item.created_at })),
              h("td", { className: "px-3 py-2" }, h(Ago, { value: item.updated_at }))
            );
          }))
        )
      ),
      h(InstanceDrawer, {
        open: !!selectedId, detail: detail, loading: detailLoading, error: detailError,
        onClose: function () { setSelectedId(null); setDetail(null); setDetailError(""); },
        onRetry: function () { return loadDetail(selectedId); },
      })
    );
  }

  function SeatCard(props) {
    var seat = props.seat;
    return h(Card, { className: "factory-seat-card" },
      h(CardContent, { className: "flex flex-col gap-3 p-4" },
        h("div", { className: "flex items-start justify-between gap-3" },
          h("div", null, h("span", { className: "font-mondwest text-display text-xs tracking-[0.12em] text-text-tertiary" }, seat.role || "operator"), h("h3", { className: "mt-1 font-mondwest normal-case text-sm font-medium text-foreground" }, seat.name)),
          h(StatePill, { value: seat.paused ? "paused" : "ready" }, seat.paused ? "paused" : "active")
        ),
        h("div", { className: "flex flex-wrap items-center gap-2" },
          h(MonoChip, { title: "Executor" }, seat.executor || "default executor"),
          h(MonoChip, { title: "Model" }, seat.model || "default model"),
          h(MonoChip, { title: "Reasoning" }, seat.reasoning || "default reasoning")
        ),
        h("dl", { className: "grid gap-2 text-xs" },
          h("div", { className: "flex items-baseline justify-between gap-3" }, h("dt", { className: "text-text-tertiary" }, "Profile"), h("dd", { className: "font-mono-ui text-right text-foreground" }, seat.profile || "—")),
          h("div", { className: "flex items-baseline justify-between gap-3" }, h("dt", { className: "text-text-tertiary" }, "Reports to"), h("dd", { className: "text-right text-foreground" }, seat.reports_to || "—")),
          h("div", { className: "flex items-baseline justify-between gap-3" }, h("dt", { className: "text-text-tertiary" }, "Concurrency"), h("dd", { className: "font-mono-ui text-right text-foreground" }, formatNumber(seat.max_concurrent)))
        )
      )
    );
  }

  function SeatsView(props) {
    var resource = usePollingResource("/seats", props.refreshKey);
    var seats = resource.data || [];
    useReportViewMeta(props, resource, []);
    return h("section", { className: "factory-view flex min-w-0 flex-col gap-4" },
      h(ViewHeading, { title: "Seats", description: "Configured Factory operators, execution profiles, and pause state." }),
      resource.error && resource.data !== null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) : null,
      resource.loading && resource.data === null ? h(LoadingState, { label: "Loading seats…" }) :
      resource.error && resource.data === null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) :
      seats.length === 0 ? h(EmptyState, { title: "No seats configured", description: "Add operators to the Factory seats configuration to populate this view." }) :
      h("div", { className: "factory-card-grid" }, seats.map(function (seat) { return h(SeatCard, { key: seat.name, seat: seat }); }))
    );
  }

  function CostCard(props) {
    var item = props.item;
    var identity = item.day || item.instance || "unknown";
    return h(Card, { className: "factory-cost-card" },
      h(CardContent, { className: "flex flex-col gap-3 p-4" },
        h("div", { className: "flex items-start justify-between gap-3" },
          h("div", null,
            h("span", { className: "font-mondwest text-display text-xs tracking-[0.12em] text-text-tertiary" }, props.kind),
            h("h3", { className: "mt-1 text-sm font-medium text-foreground " + (item.instance ? "font-mono-ui" : "font-mondwest normal-case") }, identity)
          ),
          h(StatePill, { value: Number(item.tokens_total || 0) > 0 ? "running" : "waiting" }, formatNumber(item.tokens_total) + " tokens")
        ),
        item.recipe ? h("p", { className: "text-xs text-text-secondary" }, item.board + " · " + item.recipe) : null,
        h("dl", { className: "grid gap-2 text-xs" },
          h("div", { className: "flex items-baseline justify-between gap-3" }, h("dt", { className: "text-text-tertiary" }, "Charges"), h("dd", { className: "font-mono-ui text-right text-foreground" }, formatNumber(item.charges))),
          h("div", { className: "flex items-baseline justify-between gap-3" }, h("dt", { className: "text-text-tertiary" }, "Tokens"), h("dd", { className: "font-mono-ui text-right text-foreground" }, formatNumber(item.tokens_total)))
        )
      )
    );
  }

  function CostsView(props) {
    var daily = usePollingResource("/costs?by=day&since_days=30", props.refreshKey);
    var perInstance = usePollingResource("/costs?by=instance&since_days=30", props.refreshKey);
    var loading = daily.data === null && daily.loading || perInstance.data === null && perInstance.loading;
    var fatalError = daily.data === null && daily.error || perInstance.data === null && perInstance.error;
    var loadedAt = daily.loadedAt && perInstance.loadedAt
      ? (daily.loadedAt > perInstance.loadedAt ? daily.loadedAt : perInstance.loadedAt)
      : daily.loadedAt || perInstance.loadedAt;
    useReportViewMeta(props, { loadedAt: loadedAt }, (perInstance.data || []).map(function (item) { return item.board; }));
    function retry() { daily.reload().catch(function () {}); perInstance.reload().catch(function () {}); }

    return h("section", { className: "factory-view flex min-w-0 flex-col gap-4" },
      h(ViewHeading, { title: "Costs", description: "Budget charges by UTC day and recipe instance over the last 30 days." }),
      !fatalError && (daily.error || perInstance.error) ? h(ErrorState, { message: daily.error || perInstance.error, onRetry: retry }) : null,
      loading ? h(LoadingState, { label: "Loading cost rollups…" }) :
      fatalError ? h(ErrorState, { message: fatalError, onRetry: retry }) :
      h("div", { className: "grid gap-6" },
        h("section", { className: "flex flex-col gap-3" },
          h(SectionHeading, { title: "Daily usage", description: "Token charges grouped by UTC day.", meta: (daily.data || []).length + " days" }),
          (daily.data || []).length === 0 ? h(EmptyState, { title: "No daily charges", description: "Factory has not admitted any token-budget charges in this period." }) :
          h("div", { className: "factory-card-grid" }, daily.data.map(function (item) { return h(CostCard, { key: item.day, item: item, kind: "UTC day" }); }))
        ),
        h("section", { className: "flex flex-col gap-3" },
          h(SectionHeading, { title: "Instance usage", description: "Token charges grouped by recipe instance.", meta: (perInstance.data || []).length + " instances" }),
          (perInstance.data || []).length === 0 ? h(EmptyState, { title: "No instance charges", description: "Per-instance usage appears when a recipe activation is admitted." }) :
          h("div", { className: "factory-card-grid" }, perInstance.data.map(function (item) { return h(CostCard, { key: item.instance, item: item, kind: "Instance" }); }))
        )
      )
    );
  }

  var VIEW_REGISTRY = [
    { id: "waiting", label: "Waiting gates", component: WaitingView },
    { id: "instances", label: "Instances", component: InstancesView },
    { id: "seats", label: "Seats", component: SeatsView },
    { id: "costs", label: "Costs", component: CostsView },
  ];

  function FactoryHeader(props) {
    return h("header", { className: "flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between" },
      h("div", { className: "min-w-0" },
        h("div", { className: "flex flex-wrap items-center gap-2" },
          h(StatePill, { value: "running" }, props.board || "All boards"),
          h("p", { className: "text-sm text-text-secondary" }, "Recipe operations, approvals, capacity, and spend.")
        ),
        h("div", { className: "mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-tertiary" },
          h("span", null, "Board scope: ", h("strong", { className: "font-medium text-foreground" }, props.board || "All boards")),
          h("span", null, "Last refreshed ", props.loadedAt ? h(Ago, { value: props.loadedAt }) : "—"),
          h("span", null, "Auto-refreshes every " + Math.round(POLL_MS / 1000) + "s")
        )
      ),
      h(Button, { size: "sm", ghost: true, className: "shrink-0 uppercase", onClick: props.onRefresh, title: "Reload the active Factory view" },
        h("span", { "aria-hidden": "true" }, "↻"), " Refresh"
      )
    );
  }

  function SegmentedNav(props) {
    return h("nav", {
      className: "factory-tabs inline-flex w-fit border border-midground/15 bg-background/30",
      "aria-label": "Factory views",
      role: "radiogroup",
    }, VIEW_REGISTRY.map(function (view) {
      var active = props.value === view.id;
      return h("button", {
        key: view.id,
        type: "button",
        role: "radio",
        "aria-checked": active,
        className: [
          "font-mondwest text-display tracking-[0.1em]",
          "transition-colors cursor-pointer whitespace-nowrap",
          "border-r border-midground/15 last:border-r-0",
          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground/30",
          "h-8 px-3 text-xs",
          active
            ? "is-active bg-midground text-background"
            : "text-text-secondary hover:bg-midground/10 hover:text-midground",
        ].join(" "),
        onClick: function () { props.onChange(view.id); },
      }, view.label);
    }));
  }

  function FactoryPage() {
    var _a = useState("waiting"), activeId = _a[0], setActiveId = _a[1];
    var _b = useState(0), refreshKey = _b[0], setRefreshKey = _b[1];
    var _c = useState({ board: "All boards", loadedAt: null }), meta = _c[0], setMeta = _c[1];
    var active = useMemo(function () {
      return VIEW_REGISTRY.find(function (view) { return view.id === activeId; }) || VIEW_REGISTRY[0];
    }, [activeId]);
    var ActiveView = active.component;
    var onMeta = useCallback(function (next) { setMeta(next); }, []);

    useEffect(function () {
      var timer = window.setInterval(function () {
        setMeta(function (current) { return { board: current.board, loadedAt: current.loadedAt }; });
      }, 30000);
      return function () { window.clearInterval(timer); };
    }, []);

    return h("main", { className: "hermes-factory flex flex-col gap-4" },
      h(FactoryHeader, {
        board: meta.board, loadedAt: meta.loadedAt,
        onRefresh: function () { setRefreshKey(function (value) { return value + 1; }); },
      }),
      h(SegmentedNav, {
        value: activeId,
        onChange: function (id) { setActiveId(id); setMeta({ board: "All boards", loadedAt: null }); },
      }),
      h(ActiveView, { refreshKey: refreshKey, onMeta: onMeta })
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("factory", FactoryPage);
  }
})();
