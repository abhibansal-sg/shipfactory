/**
 * ShipFactory dashboard plugin.
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
  var API = "/api/plugins/shipfactory";
  var POLL_MS = 20000;

  function request(path, options) {
    // POST bodies are JSON.stringify'd — without an explicit JSON
    // Content-Type FastAPI receives the raw string and Pydantic rejects it
    // ("Input should be a valid dictionary") — shakedown finding #16.
    if (options && options.body) {
      options.headers = Object.assign(
        { "Content-Type": "application/json" },
        options.headers || {}
      );
    }
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

  function newNonce() {
    // A fresh, single-use nonce per decision click (finding #81). The server
    // treats a replayed nonce for a different tuple as a conflict, so it must
    // never be derived from the gate.
    try {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID().replace(/-/g, "");
      }
    } catch (_ignore) { /* fall through to the timestamped fallback */ }
    return "n" + Date.now().toString(16) + Math.floor(Math.random() * 0xffffffff).toString(16);
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
      reject: "destructive", cancelled: "destructive", cancelling: "destructive", stopped: "destructive",
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
        h("strong", { className: "font-mondwest normal-case text-sm font-medium text-destructive" }, props.title || "ShipFactory data could not be loaded"),
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

  var FIELD_CLASS = "flex h-9 w-full border border-border bg-background/40 px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:border-foreground/25 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30";
  var TEXTAREA_CLASS = "flex min-h-[80px] w-full border border-border bg-background/40 px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:border-foreground/25 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30";

  function DialogFrame(props) {
    useEffect(function () {
      if (!props.open) return undefined;
      function onKey(event) { if (event.key === "Escape" && !props.busy) props.onClose(); }
      window.addEventListener("keydown", onKey);
      return function () { window.removeEventListener("keydown", onKey); };
    }, [props.open, props.busy, props.onClose]);
    if (!props.open) return null;
    return h("div", {
      className: "fixed inset-0 z-[200] flex items-center justify-center bg-background/85 p-4",
      role: "dialog", "aria-modal": "true", "aria-labelledby": props.labelledBy,
      onClick: function (event) { if (event.target === event.currentTarget && !props.busy) props.onClose(); },
    },
      h(Card, { className: "flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden border border-border bg-card shadow-2xl" },
        h("div", { className: "flex items-start justify-between gap-4 border-b border-border p-4" },
          h("div", { className: "min-w-0" },
            h("h2", { id: props.labelledBy, className: "font-mondwest text-display text-base tracking-wider text-foreground" }, props.title),
            props.description ? h("p", { className: "mt-1 text-xs leading-relaxed text-text-tertiary" }, props.description) : null
          ),
          h(Button, { type: "button", size: "xs", ghost: true, disabled: props.busy, onClick: props.onClose, "aria-label": "Close dialog" }, "×")
        ),
        h(CardContent, { className: "overflow-y-auto p-4" }, props.children)
      )
    );
  }

  function FieldLabel(props) {
    return h("label", { className: "grid gap-2 text-xs text-text-secondary", htmlFor: props.htmlFor },
      h("span", null, props.children, props.required ? h("span", { className: "ml-1 text-destructive", title: "Required" }, "*") : null),
      props.control
    );
  }

  function recipeKey(recipe) { return recipe ? recipe.id + "@" + recipe.version : ""; }

  function defaultParameters(recipe, existing) {
    var values = {};
    var source = existing || {};
    Object.keys((recipe && recipe.parameters) || {}).forEach(function (name) {
      var spec = recipe.parameters[name];
      if (Object.prototype.hasOwnProperty.call(source, name)) values[name] = source[name];
      else if (Object.prototype.hasOwnProperty.call(spec, "default")) values[name] = spec.default;
      else values[name] = spec.type === "boolean" ? false : "";
    });
    return values;
  }

  function ParameterFields(props) {
    var recipe = props.recipe;
    if (!recipe) return null;
    return h("div", { className: "grid gap-4 sm:grid-cols-2" }, Object.keys(recipe.parameters || {}).map(function (name) {
      var spec = recipe.parameters[name];
      var id = props.prefix + "-parameter-" + name;
      var value = props.values[name];
      var control;
      if (spec.type === "boolean") {
        control = h("label", { className: "flex h-9 items-center gap-2 border border-border bg-background/40 px-3 text-sm text-text-secondary" },
          h("input", { id: id, type: "checkbox", checked: !!value, onChange: function (event) { props.onChange(name, event.target.checked); } }),
          "Enabled"
        );
      } else if (spec.type === "enum") {
        control = h("select", { id: id, className: FIELD_CLASS, required: spec.required, value: value == null ? "" : value, onChange: function (event) { props.onChange(name, event.target.value === "" && !spec.required ? null : event.target.value); } },
          !spec.required ? h("option", { value: "" }, "Use no value") : null,
          (spec.values || []).map(function (option) { return h("option", { key: option, value: option }, option); })
        );
      } else {
        control = h("input", {
          id: id,
          className: FIELD_CLASS,
          type: spec.type === "integer" ? "number" : spec.type === "datetime" ? "datetime-local" : "text",
          required: spec.required,
          value: value == null ? "" : value,
          onChange: function (event) {
            var next = event.target.value;
            props.onChange(name, spec.type === "integer" && next !== "" ? Number(next) : next === "" && !spec.required ? null : next);
          },
        });
      }
      return h(FieldLabel, { key: name, htmlFor: id, required: spec.required, control: control }, name.replace(/_/g, " ") + " · " + spec.type);
    }));
  }

  function RunRecipeDialog(props) {
    var activeRecipes = (props.recipes || []).filter(function (recipe) { return recipe.status === "active"; });
    var _a = useState(""), selectedKey = _a[0], setSelectedKey = _a[1];
    var _b = useState({}), parameters = _b[0], setParameters = _b[1];
    var _c = useState([]), skips = _c[0], setSkips = _c[1];
    var _d = useState(props.board || "default"), board = _d[0], setBoard = _d[1];
    var _e = useState(""), error = _e[0], setError = _e[1];
    var _f = useState(false), busy = _f[0], setBusy = _f[1];
    var selected = activeRecipes.find(function (recipe) { return recipeKey(recipe) === selectedKey; });
    useEffect(function () {
      if (!props.open || activeRecipes.length === 0) return;
      var next = selected || activeRecipes[0];
      if (!selected) setSelectedKey(recipeKey(next));
      setBoard(props.board || "default");
    }, [props.open, activeRecipes.map(recipeKey).join("|"), props.board]);
    useEffect(function () {
      if (!selected) return;
      setParameters(defaultParameters(selected));
      setSkips([]);
      setError("");
    }, [selectedKey]);

    function submit(event) {
      event.preventDefault();
      if (!selected) { setError("No active recipe is available."); return; }
      setBusy(true); setError("");
      request("/instances", { method: "POST", body: JSON.stringify({
        recipe: selected.id, version: selected.version, board: board,
        parameters: parameters, skip_steps: skips,
      }) }).then(function (result) {
        props.onCreated(result);
      }).catch(function (err) { setError(errorText(err)); }).finally(function () { setBusy(false); });
    }

    return h(DialogFrame, { open: props.open, busy: busy, onClose: props.onClose, labelledBy: "run-recipe-title", title: "Run recipe", description: "Create a pinned Factory instance from the configured recipe library." },
      h("form", { className: "grid gap-5", onSubmit: submit },
        error ? h("div", { className: "border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive", role: "alert" }, error) : null,
        props.recipesError ? h("div", { className: "border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive", role: "alert" }, props.recipesError) : null,
        h("div", { className: "grid gap-4 sm:grid-cols-2" },
          h(FieldLabel, { htmlFor: "run-recipe-picker", required: true, control: h("select", { id: "run-recipe-picker", className: FIELD_CLASS, required: true, value: selectedKey, onChange: function (event) { setSelectedKey(event.target.value); } }, activeRecipes.map(function (recipe) { return h("option", { key: recipeKey(recipe), value: recipeKey(recipe) }, recipe.id + " · v" + recipe.version); })) }, "Recipe and version"),
          h(FieldLabel, { htmlFor: "run-recipe-board", required: true, control: h("input", { id: "run-recipe-board", className: FIELD_CLASS, required: true, value: board, onChange: function (event) { setBoard(event.target.value); } }) }, "Board")
        ),
        selected ? h("p", { className: "text-xs leading-relaxed text-text-tertiary" }, selected.description) : null,
        h(ParameterFields, { prefix: "run", recipe: selected, values: parameters, onChange: function (name, value) { setParameters(function (current) { return Object.assign({}, current, (function () { var next = {}; next[name] = value; return next; })()); }); } }),
        selected && selected.optional_steps.length ? h("fieldset", { className: "grid gap-2 border border-border p-3" },
          h("legend", { className: "px-1 text-xs text-text-secondary" }, "Skip optional steps"),
          selected.optional_steps.map(function (step) { return h("label", { key: step.id, className: "flex items-center gap-2 text-sm text-text-secondary" },
            h("input", { type: "checkbox", checked: skips.indexOf(step.id) >= 0, onChange: function (event) { setSkips(function (current) { return event.target.checked ? current.concat([step.id]) : current.filter(function (item) { return item !== step.id; }); }); } }),
            h("span", null, step.title), h(MonoChip, null, step.id)
          ); })
        ) : null,
        h("div", { className: "flex justify-end gap-2 border-t border-border pt-4" },
          h(Button, { type: "button", size: "sm", ghost: true, disabled: busy, onClick: props.onClose }, "Cancel"),
          h(Button, { type: "submit", size: "sm", disabled: busy || !selected }, busy ? h(Spinner, { label: "Starting" }) : "Run recipe")
        )
      )
    );
  }

  function TriageDialog(props) {
    var _a = useState(""), title = _a[0], setTitle = _a[1];
    var _b = useState(""), body = _b[0], setBody = _b[1];
    var _c = useState(props.board || "default"), board = _c[0], setBoard = _c[1];
    var _d = useState(""), error = _d[0], setError = _d[1];
    var _e = useState(false), busy = _e[0], setBusy = _e[1];
    useEffect(function () { if (props.open) { setBoard(props.board || "default"); setError(""); } }, [props.open, props.board]);
    function submit(event) {
      event.preventDefault(); setBusy(true); setError("");
      request("/triage", { method: "POST", body: JSON.stringify({ title: title, body: body, board: board }) })
        .then(function (result) { props.onCreated(result); setTitle(""); setBody(""); })
        .catch(function (err) { setError(errorText(err)); }).finally(function () { setBusy(false); });
    }
    return h(DialogFrame, { open: props.open, busy: busy, onClose: props.onClose, labelledBy: "new-triage-title", title: "New triage task", description: "Park an operator request in Kanban triage for Factory routing." },
      h("form", { className: "grid gap-4", onSubmit: submit },
        h("div", { className: "flex items-start gap-2 border p-3 text-xs " + (props.daemon && props.daemon.running ? "border-success/30 bg-success/10 text-text-secondary" : "border-destructive/30 bg-destructive/10 text-destructive") },
          h(StatePill, { value: props.daemon && props.daemon.running ? "running" : "stopped" }, props.daemon && props.daemon.running ? "Daemon running" : "Daemon stopped"),
          h("span", null, props.daemon && props.daemon.running ? "ShipFactory can route this task on its next tick." : "Triage routing only happens while the Factory daemon is running.")
        ),
        error ? h("div", { className: "border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive", role: "alert" }, error) : null,
        h(FieldLabel, { htmlFor: "triage-title", required: true, control: h("input", { id: "triage-title", className: FIELD_CLASS, required: true, value: title, onChange: function (event) { setTitle(event.target.value); }, placeholder: "What needs triage?" }) }, "Title"),
        h(FieldLabel, { htmlFor: "triage-body", control: h("textarea", { id: "triage-body", className: TEXTAREA_CLASS, value: body, onChange: function (event) { setBody(event.target.value); }, placeholder: "Context, constraints, and expected outcome" }) }, "Body"),
        h(FieldLabel, { htmlFor: "triage-board", required: true, control: h("input", { id: "triage-board", className: FIELD_CLASS, required: true, value: board, onChange: function (event) { setBoard(event.target.value); } }) }, "Board"),
        h("div", { className: "flex justify-end gap-2 border-t border-border pt-4" },
          h(Button, { type: "button", size: "sm", ghost: true, disabled: busy, onClick: props.onClose }, "Cancel"),
          h(Button, { type: "submit", size: "sm", disabled: busy }, busy ? h(Spinner, { label: "Creating" }) : "Create triage task")
        )
      )
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

  // Keep recipe retrieval behind a small read-only resource boundary so a
  // future editor can add its own mutations without restructuring views.
  function useRecipesResource(refreshKey) {
    return usePollingResource("/recipes", refreshKey);
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

  function ReviewStoryCard(props) {
    var story = props.story;
    if (!story) return null;
    return h("section", { className: "factory-review-story border border-border bg-background/30 p-3", "aria-label": "Review story" },
      h("div", { className: "flex flex-wrap items-start justify-between gap-2" },
        h("div", null,
          h("span", { className: "font-mondwest text-display text-xs tracking-[0.12em] text-text-tertiary" }, "Operator approval card"),
          h("h4", { className: "mt-1 text-sm font-medium text-foreground" }, story.headline)
        ),
        h(MonoChip, { title: "Exact change-set SHA-256" }, story.change_set_sha256)
      ),
      h("div", { className: "mt-3 grid gap-3" }, (story.changes || []).map(function (change, index) {
        return h("article", { key: index, className: "border-l-2 border-primary/40 pl-3" },
          h("div", { className: "flex flex-wrap items-center gap-2" },
            h(StatePill, { value: "running" }, "importance " + change.importance),
            (change.requirement_ids || []).map(function (id) { return h(MonoChip, { key: id }, id); })
          ),
          h("p", { className: "mt-2 text-xs leading-relaxed text-text-secondary" }, change.why),
          h("p", { className: "mt-1 text-xs leading-relaxed text-text-tertiary" }, "Risk: " + change.risk),
          h("div", { className: "mt-2 flex flex-wrap gap-2" },
            (change.files || []).map(function (path) { return h(MonoChip, { key: path, title: "Changed path" }, path); })
          ),
          h("div", { className: "mt-2 flex flex-wrap gap-2" },
            (change.evidence_case_ids || []).map(function (id) { return h(MonoChip, { key: id, title: "Evidence case" }, id); })
          )
        );
      })),
      (story.generated_or_mechanical_files || []).length ? h("div", { className: "mt-3" },
        h("strong", { className: "text-xs text-foreground" }, "Generated or mechanical files"),
        h("div", { className: "mt-1 flex flex-wrap gap-2" }, story.generated_or_mechanical_files.map(function (path) { return h(MonoChip, { key: path }, path); }))
      ) : null,
      (story.not_changed || []).length ? h("div", { className: "mt-3" },
        h("strong", { className: "text-xs text-foreground" }, "Explicitly not implemented"),
        h("ul", { className: "mt-1 grid gap-1 text-xs text-text-secondary" }, story.not_changed.map(function (item, index) {
          return h("li", { key: index }, ((item.requirement_ids || [item.requirement_id]).filter(Boolean)).join(", ") + ": " + (item.reason || item.why));
        }))
      ) : null,
      h("div", { className: "mt-3" },
        h("strong", { className: "text-xs text-foreground" }, "Residual risks"),
        (story.residual_risks || []).length
          ? h("ul", { className: "mt-1 grid gap-1 text-xs text-text-secondary" }, story.residual_risks.map(function (risk, index) { return h("li", { key: index }, risk); }))
          : h("p", { className: "mt-1 text-xs text-text-tertiary" }, "No residual risks declared.")
      )
    );
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
        ),
        h(ReviewStoryCard, { story: gate.review_story })
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
      // A gate is only decidable once /waiting has attached its verified
      // revision binding (finding #81). Without it the decision endpoint
      // fail-closes; refuse locally with the server's binding error rather
      // than posting a doomed 3-field body.
      if (!gate.revision_hash) {
        setToast({ ok: false, text: gate.binding_error || "This gate has no verified revision binding yet; refresh and try again." });
        return;
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
        body: JSON.stringify({
          instance: gate.instance_id,
          step: gate.step_id,
          activation: gate.activation,
          revision_hash: gate.revision_hash,
          evidence_bundle_hash: gate.evidence_bundle_hash == null ? null : gate.evidence_bundle_hash,
          nonce: newNonce(),
          actor_kind: "operator",
          actor_id: "dashboard-operator",
          channel: "dashboard",
          reason: reason,
        }),
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

  function CancelConfirmDialog(props) {
    var preview = props.preview;
    return h(DialogFrame, { open: !!preview, busy: props.busy, onClose: props.onClose, labelledBy: "cancel-instance-title", title: "Confirm instance cancellation", description: "Review the dry-run consequences. Nothing is cancelled until you confirm." },
      preview ? h("div", { className: "grid gap-4" },
        props.error ? h("div", { className: "border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive", role: "alert" }, props.error) : null,
        h("div", { className: "border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive" },
          h("strong", { className: "font-mondwest normal-case font-medium" }, "This stops future Factory work."),
          h("p", { className: "mt-1 text-xs leading-relaxed" }, "Completed external effects are not reversed. The collector remains blocked so cancellation cannot release outer dependencies.")
        ),
        h("div", { className: "grid gap-3 sm:grid-cols-2" },
          h(Card, null, h(CardContent, { className: "p-3" },
            h("h3", { className: "font-mondwest normal-case text-sm font-medium" }, "Active workers"),
            (preview.workers || []).length ? h("ul", { className: "mt-2 grid gap-2 text-xs text-text-secondary" }, preview.workers.map(function (worker) { return h("li", { key: worker.task_id, className: "flex flex-wrap items-center gap-2" }, h(MonoChip, null, worker.task_id), h("span", null, "PID " + worker.pid), worker.executor ? h(StatePill, { value: "running" }, worker.executor) : null); })) : h("p", { className: "mt-2 text-xs text-text-tertiary" }, "No active Factory worker processes.")
          )),
          h(Card, null, h(CardContent, { className: "p-3" },
            h("h3", { className: "font-mondwest normal-case text-sm font-medium" }, "Suppressed downstream tasks"),
            (preview.suppressed || []).length ? h("ul", { className: "mt-2 grid gap-1 text-xs text-text-secondary" }, preview.suppressed.map(function (task) { return h("li", { key: task }, h(MonoChip, null, task)); })) : h("p", { className: "mt-2 text-xs text-text-tertiary" }, "No created task rows to suppress.")
          ))
        ),
        h("div", { className: "border border-border bg-background/30 p-3" },
          h("h3", { className: "font-mondwest normal-case text-sm font-medium" }, "Nonterminal steps"),
          h("div", { className: "mt-2 flex flex-wrap gap-2" }, (preview.nonterminal_steps || []).map(function (step) { return h(StatePill, { key: step, value: "pending" }, step); }))
        ),
        h("div", { className: "flex justify-end gap-2 border-t border-border pt-4" },
          h(Button, { type: "button", size: "sm", ghost: true, disabled: props.busy, onClick: props.onClose }, "Keep instance"),
          h(Button, { type: "button", size: "sm", destructive: true, disabled: props.busy, onClick: props.onConfirm }, props.busy ? h(Spinner, { label: "Cancelling" }) : "Confirm cancellation")
        )
      ) : null
    );
  }

  function InstanceControls(props) {
    var activeRecipes = (props.recipes || []).filter(function (recipe) { return recipe.status === "active"; });
    var alternatives = activeRecipes.filter(function (recipe) { return recipeKey(recipe) !== props.detail.recipe; });
    var _a = useState(""), selectedKey = _a[0], setSelectedKey = _a[1];
    var _b = useState(""), error = _b[0], setError = _b[1];
    var _c = useState(""), busy = _c[0], setBusy = _c[1];
    var _d = useState(null), preview = _d[0], setPreview = _d[1];
    useEffect(function () {
      if (alternatives.length && !alternatives.some(function (recipe) { return recipeKey(recipe) === selectedKey; })) setSelectedKey(recipeKey(alternatives[0]));
    }, [props.detail.id, alternatives.map(recipeKey).join("|")]);

    function reroute() {
      var recipe = alternatives.find(function (item) { return recipeKey(item) === selectedKey; });
      if (!recipe) return;
      var existing = {};
      try { existing = JSON.parse(props.detail.parameters_json || "{}"); } catch (_ignore) { existing = {}; }
      setBusy("reroute"); setError("");
      request("/instances/" + encodeURIComponent(props.detail.id) + "/reroute", { method: "POST", body: JSON.stringify({ recipe: recipe.id, version: recipe.version, parameters: defaultParameters(recipe, existing) }) })
        .then(function (result) { props.onChanged("Instance rerouted to " + recipeKey(recipe) + ".", result); })
        .catch(function (err) { setError(errorText(err)); }).finally(function () { setBusy(""); });
    }

    function previewCancel() {
      setBusy("preview"); setError("");
      request("/instances/" + encodeURIComponent(props.detail.id) + "/cancel")
        .then(setPreview).catch(function (err) { setError(errorText(err)); }).finally(function () { setBusy(""); });
    }

    function confirmCancel() {
      setBusy("cancel"); setError("");
      request("/instances/" + encodeURIComponent(props.detail.id) + "/cancel", { method: "POST" })
        .then(function (result) { setPreview(null); props.onChanged("Instance cancelled.", result); })
        .catch(function (err) { setError(errorText(err)); }).finally(function () { setBusy(""); });
    }

    return h("section", { className: "grid gap-3 border border-border bg-background/20 p-3" },
      h(SectionHeading, { title: "Instance controls", description: "Reroute through the CLI replacement path or preview cancellation consequences." }),
      error ? h("div", { className: "border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive", role: "alert" }, error) : null,
      props.recipesError ? h("p", { className: "text-xs text-destructive" }, props.recipesError) : null,
      h("div", { className: "grid gap-3 sm:grid-cols-[1fr_auto]" },
        h(FieldLabel, { htmlFor: "reroute-recipe", control: h("select", { id: "reroute-recipe", className: FIELD_CLASS, disabled: busy || alternatives.length === 0, value: selectedKey, onChange: function (event) { setSelectedKey(event.target.value); } }, alternatives.length ? alternatives.map(function (recipe) { return h("option", { key: recipeKey(recipe), value: recipeKey(recipe) }, recipe.id + " · v" + recipe.version); }) : h("option", { value: "" }, "No alternative recipes")) }, "Reroute recipe"),
        h("div", { className: "flex items-end" }, h(Button, { type: "button", size: "sm", ghost: true, disabled: busy || !selectedKey, onClick: reroute }, busy === "reroute" ? h(Spinner, { label: "Rerouting" }) : "Reroute"))
      ),
      h("div", { className: "flex flex-col gap-2 border-t border-border pt-3 sm:flex-row sm:items-center sm:justify-between" },
        h("p", { className: "text-xs leading-relaxed text-text-tertiary" }, "Cancellation always opens a dry-run consequence review before confirmation."),
        h(Button, { type: "button", size: "sm", ghost: true, destructive: true, disabled: busy || ["done", "failed", "cancelled"].indexOf(props.detail.status) >= 0, onClick: previewCancel }, busy === "preview" ? h(Spinner, { label: "Previewing" }) : "Preview cancellation")
      ),
      h(CancelConfirmDialog, { preview: preview, busy: busy === "cancel", error: error, onClose: function () { if (busy !== "cancel") { setPreview(null); setError(""); } }, onConfirm: confirmCancel })
    );
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
          h(InstanceControls, { detail: detail, recipes: props.recipes, recipesError: props.recipesError, onChanged: props.onChanged }),
          h(ReviewStoryCard, { story: detail.review_story }),
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
    var recipesResource = useRecipesResource(props.refreshKey);
    var _a = useState(null), expandedId = _a[0], setExpandedId = _a[1];
    var _b = useState(null), selectedId = _b[0], setSelectedId = _b[1];
    var _c = useState(null), detail = _c[0], setDetail = _c[1];
    var _d = useState(false), detailLoading = _d[0], setDetailLoading = _d[1];
    var _e = useState(""), detailError = _e[0], setDetailError = _e[1];
    var _f = useState(""), dialog = _f[0], setDialog = _f[1];
    var _g = useState(null), toast = _g[0], setToast = _g[1];
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

    function activeStep(item) {
      var states = ["ready", "running", "waiting", "blocked"];
      return (item.latest_steps || []).find(function (step) {
        return states.indexOf(normalizedState(step.state)) >= 0;
      }) || null;
    }

    function stepLabel(item, step) {
      var recipe = (recipesResource.data || []).find(function (candidate) {
        return recipeKey(candidate) === item.recipe;
      });
      var definition = recipe && (recipe.steps || []).find(function (candidate) {
        return candidate.id === step.step_id;
      });
      return definition && definition.title || step.step_id;
    }

    function writeComplete(message) {
      setDialog(""); setToast({ ok: true, text: message });
      resource.reload().catch(function () {});
    }

    function controlComplete(message) {
      setSelectedId(null); setDetail(null); setDetailError("");
      writeComplete(message);
    }

    return h("section", { className: "factory-view flex min-w-0 flex-col gap-4" },
      h(ViewHeading, { title: "Instances", description: "Recipe progress and current workflow state.", action: h("div", { className: "flex flex-wrap gap-2" },
        h(Button, { type: "button", size: "sm", onClick: function () { setDialog("run"); } }, "Run recipe"),
        h(Button, { type: "button", size: "sm", outlined: true, onClick: function () { setDialog("triage"); } }, "New triage task")
      ) }),
      h(Toast, { toast: toast, onClose: function () { setToast(null); } }),
      resource.error && resource.data !== null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) : null,
      resource.loading && resource.data === null ? h(LoadingState, { label: "Loading recipe instances…" }) :
      resource.error && resource.data === null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) :
      instances.length === 0 ? h(EmptyState, { title: "No recipe instances yet", description: "Instances appear after a task is matched to an active recipe." }) :
      h("div", { className: "factory-instance-list border border-border bg-card" }, instances.map(function (item) {
        var expanded = expandedId === item.id;
        var current = activeStep(item);
        return h("article", { key: item.id, className: "border-b border-border/50 last:border-b-0" },
          h("button", {
            type: "button", className: "flex w-full flex-wrap items-center gap-3 p-3 text-left transition-colors hover:bg-secondary/20",
            "aria-expanded": expanded, onClick: function () { setExpandedId(expanded ? null : item.id); },
          },
            h("span", { className: "min-w-0 flex-1" }, h("strong", { className: "block truncate font-medium text-foreground" }, item.recipe || item.id), h("span", { className: "block font-mono-ui text-xs text-text-tertiary" }, item.id)),
            h(StatePill, { value: item.status }),
            h("span", { className: "text-xs text-text-secondary" }, current ? stepLabel(item, current) : "No active step")
          ),
          expanded ? h("div", { className: "border-t border-border bg-background/20 p-3" },
            h("ol", { className: "grid gap-2", "aria-label": "Latest step state" }, (item.latest_steps || []).map(function (step) {
              return h("li", { key: step.step_id }, h("button", {
                type: "button", className: "flex w-full flex-wrap items-center gap-2 border border-border bg-card p-2 text-left text-xs hover:bg-secondary/20",
                onClick: function () { loadDetail(item.id); },
              },
                h("strong", { className: "font-mono-ui text-foreground" }, stepLabel(item, step)),
                h(StatePill, { value: step.state }),
                step.rejected_by_step_id ? h(MonoChip, { title: "Rework provenance" }, "rework ← " + step.rejected_by_step_id + (step.rejected_by_activation != null ? " #" + step.rejected_by_activation : "")) : null
              ));
            }))
          ) : null
        );
      })),
      h(InstanceDrawer, {
        open: !!selectedId, detail: detail, loading: detailLoading, error: detailError,
        recipes: recipesResource.data || [], recipesError: recipesResource.error,
        onClose: function () { setSelectedId(null); setDetail(null); setDetailError(""); },
        onRetry: function () { return loadDetail(selectedId); },
        onChanged: controlComplete,
      }),
      h(RunRecipeDialog, { open: dialog === "run", recipes: recipesResource.data || [], recipesError: recipesResource.error, board: props.status && props.status.board, onClose: function () { setDialog(""); }, onCreated: function (result) { writeComplete("Started " + result.recipe + " as " + result.instance_id + "."); } }),
      h(TriageDialog, { open: dialog === "triage", daemon: props.status, board: props.status && props.status.board, onClose: function () { setDialog(""); }, onCreated: function (result) { writeComplete("Created triage task " + result.task_id + " on " + result.board + "."); } })
    );
  }

  function RecipeStepDetail(props) {
    var step = props.step;
    var caps = props.recipe.budgets && props.recipe.budgets.step_activation_caps;
    var cap = caps && caps[step.id];
    return h("section", { className: "border border-border bg-background/20 p-3 text-sm" },
      h("div", { className: "flex flex-wrap items-center gap-2" }, h("strong", { className: "font-mono-ui" }, step.id), h(MonoChip, null, step.primitive)),
      h("dl", { className: "mt-3 grid gap-2 text-xs" },
        h("div", null, h("dt", { className: "text-text-tertiary" }, "Instructions"), h("dd", { className: "mt-1 whitespace-pre-wrap text-text-secondary" }, step.instructions || "No instructions declared.")),
        h("div", null, h("dt", { className: "text-text-tertiary" }, "Needs"), h("dd", null, (step.needs || []).length ? step.needs.join(", ") : "None")),
        step.execution_profile ? h("div", null, h("dt", { className: "text-text-tertiary" }, "Execution profile"), h("dd", null, step.execution_profile)) : null,
        h("div", null, h("dt", { className: "text-text-tertiary" }, "Activation cap"), h("dd", null, cap == null ? "Not capped" : cap))
      )
    );
  }

  function RecipeCard(props) {
    var recipe = props.recipe;
    var expanded = props.expanded;
    return h("article", { className: "border border-border bg-card" },
      h("button", { type: "button", className: "flex w-full flex-wrap items-center gap-3 p-3 text-left hover:bg-secondary/20", "aria-expanded": expanded, onClick: props.onToggle },
        h("strong", { className: "font-mono-ui text-sm " + (recipe.status === "active" ? "text-primary" : "text-foreground") }, recipeKey(recipe)),
        h(StatePill, { value: recipe.status }),
        h("span", { className: "min-w-0 flex-1 truncate text-xs text-text-secondary" }, recipe.description)
      ),
      expanded ? h("div", { className: "border-t border-border p-3" },
        h("ol", { className: "grid gap-2", "aria-label": "Recipe step chain" }, (recipe.steps || []).map(function (step) {
          return h("li", { key: step.id },
            h("button", { type: "button", className: "flex w-full flex-wrap items-center gap-2 border border-border bg-background/20 p-2 text-left text-xs hover:bg-secondary/20", onClick: function () { props.onSelectStep(step); } },
              h("strong", { className: "font-mono-ui" }, step.id), h("span", null, step.title), h(MonoChip, null, step.primitive), step.seat ? h(MonoChip, { title: "Seat" }, "@" + step.seat) : null
            ),
            props.selectedStep && props.selectedStep.id === step.id ? h("div", { className: "mt-2" }, h(RecipeStepDetail, { recipe: recipe, step: step })) : null
          );
        }))
      ) : null
    );
  }

  function RecipesView(props) {
    var resource = useRecipesResource(props.refreshKey);
    var _a = useState(null), selectedKey = _a[0], setSelectedKey = _a[1];
    var _b = useState(null), selectedStep = _b[0], setSelectedStep = _b[1];
    var recipes = resource.data || [];
    useReportViewMeta(props, resource, []);
    var groups = {};
    recipes.forEach(function (recipe) { (groups[recipe.id] || (groups[recipe.id] = [])).push(recipe); });
    return h("section", { className: "factory-view flex min-w-0 flex-col gap-4" },
      h(ViewHeading, { title: "Recipes", description: "Published recipe contracts, read-only." }),
      resource.loading && resource.data === null ? h(LoadingState, { label: "Loading recipes…" }) :
      resource.error && resource.data === null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) :
      recipes.length === 0 ? h(EmptyState, { title: "No recipes configured", description: "Published recipe versions will appear here." }) :
      h("div", { className: "grid gap-4" }, Object.keys(groups).sort().map(function (id) {
        var versions = groups[id].slice().sort(function (left, right) { return Number(right.version) - Number(left.version); });
        return h("section", { key: id, className: "grid gap-2" },
          h("h2", { className: "font-mondwest normal-case text-sm font-medium text-foreground" }, id),
          versions.map(function (recipe) {
            var key = recipeKey(recipe);
            return h(RecipeCard, { key: key, recipe: recipe, expanded: selectedKey === key, selectedStep: selectedKey === key ? selectedStep : null,
              onToggle: function () { setSelectedKey(selectedKey === key ? null : key); setSelectedStep(null); }, onSelectStep: setSelectedStep });
          })
        );
      }))
    );
  }

  function SeatDialog(props) {
    var empty = { name: "", profile: "default", executor: "hermes", model: "", reasoning: "medium", role: "engineer", max_concurrent: 1, provider: "", base_url: "", provider_model: "" };
    var _a = useState(empty), values = _a[0], setValues = _a[1];
    var _b = useState(""), error = _b[0], setError = _b[1];
    var _c = useState(false), busy = _c[0], setBusy = _c[1];
    useEffect(function () {
      if (!props.open) return;
      var seat = props.seat || {};
      var template = seat.provider_config || props.providerTemplate || {};
      setValues({
        name: seat.name || "", profile: seat.profile || ((props.profiles || [])[0] || "default"),
        executor: seat.executor || "hermes", model: seat.model || template.model || "",
        reasoning: seat.reasoning || "medium", role: seat.role || "engineer",
        max_concurrent: seat.max_concurrent || 1, provider: template.provider || "",
        base_url: template.base_url || "", provider_model: template.model || seat.model || "",
      });
      setError("");
    }, [props.open, props.seat && props.seat.name, (props.profiles || []).join("|"), props.providerTemplate && props.providerTemplate.model]);
    function set(field, value) { setValues(function (current) { var next = Object.assign({}, current); next[field] = value; return next; }); }
    function submit(event) {
      event.preventDefault(); setBusy(true); setError("");
      var payload = { name: values.name, profile: values.profile, executor: values.executor, model: values.model, reasoning: values.reasoning, role: values.role, max_concurrent: Number(values.max_concurrent) };
      if (values.executor === "hermes") payload.provider_config = { provider: values.provider, base_url: values.base_url, model: values.provider_model || values.model };
      var path = props.seat ? "/seats/" + encodeURIComponent(props.seat.name) : "/seats";
      request(path, { method: props.seat ? "PUT" : "POST", body: JSON.stringify(payload) })
        .then(props.onSaved).catch(function (err) { setError(errorText(err)); }).finally(function () { setBusy(false); });
    }
    var profileOptions = (props.profiles || []).length ? props.profiles : ["default"];
    return h(DialogFrame, { open: props.open, busy: busy, onClose: props.onClose, labelledBy: "seat-contract-title", title: props.seat ? "Edit seat" : "Create seat", description: "A seat is an employment contract: profile, model, reasoning, role, and capacity." },
      h("form", { className: "grid gap-4", onSubmit: submit },
        error ? h("div", { className: "border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive", role: "alert" }, error) : null,
        h("div", { className: "grid gap-4 sm:grid-cols-2" },
          h(FieldLabel, { htmlFor: "seat-name", required: true, control: h("input", { id: "seat-name", className: FIELD_CLASS, required: true, disabled: !!props.seat, value: values.name, onChange: function (event) { set("name", event.target.value); } }) }, "Seat name"),
          h(FieldLabel, { htmlFor: "seat-profile", required: true, control: h("select", { id: "seat-profile", className: FIELD_CLASS, required: true, value: values.profile, onChange: function (event) { set("profile", event.target.value); } }, profileOptions.map(function (profile) { return h("option", { key: profile, value: profile }, profile); })) }, "Profile · labor pool"),
          h(FieldLabel, { htmlFor: "seat-executor", required: true, control: h("select", { id: "seat-executor", className: FIELD_CLASS, value: values.executor, onChange: function (event) { set("executor", event.target.value); } }, ["hermes", "codex", "claude", "grok", "opencode"].map(function (executor) { return h("option", { key: executor, value: executor }, executor); })) }, "Executor"),
          h(FieldLabel, { htmlFor: "seat-model", required: true, control: h("input", { id: "seat-model", className: FIELD_CLASS, required: true, value: values.model, onChange: function (event) { set("model", event.target.value); } }) }, "Seat model")
        ),
        values.executor === "hermes" ? h("fieldset", { className: "grid gap-3 border border-border p-3" },
          h("legend", { className: "px-1 text-xs text-text-secondary" }, "Provider lane · required to prevent finding #12 default-model inheritance"),
          h("div", { className: "grid gap-3 sm:grid-cols-3" },
            h(FieldLabel, { htmlFor: "seat-provider", required: true, control: h("input", { id: "seat-provider", className: FIELD_CLASS, required: true, value: values.provider, onChange: function (event) { set("provider", event.target.value); }, placeholder: "hermes-anthropic-proxy" }) }, "Provider name"),
            h(FieldLabel, { htmlFor: "seat-base-url", required: true, control: h("input", { id: "seat-base-url", className: FIELD_CLASS, required: true, value: values.base_url, onChange: function (event) { set("base_url", event.target.value); }, placeholder: "http://127.0.0.1:18808" }) }, "Base URL"),
            h(FieldLabel, { htmlFor: "seat-provider-model", required: true, control: h("input", { id: "seat-provider-model", className: FIELD_CLASS, required: true, value: values.provider_model, onChange: function (event) { set("provider_model", event.target.value); set("model", event.target.value); } }) }, "Provider model")
          )
        ) : null,
        h("div", { className: "grid gap-4 sm:grid-cols-3" },
          h(FieldLabel, { htmlFor: "seat-reasoning", required: true, control: h("select", { id: "seat-reasoning", className: FIELD_CLASS, value: values.reasoning, onChange: function (event) { set("reasoning", event.target.value); } }, ["low", "medium", "high", "max"].map(function (reasoning) { return h("option", { key: reasoning, value: reasoning }, reasoning); })) }, "Reasoning"),
          h(FieldLabel, { htmlFor: "seat-role", required: true, control: h("input", { id: "seat-role", className: FIELD_CLASS, required: true, list: "seat-role-suggestions", value: values.role, onChange: function (event) { set("role", event.target.value); } }) }, "Role"),
          h(FieldLabel, { htmlFor: "seat-concurrency", required: true, control: h("input", { id: "seat-concurrency", className: FIELD_CLASS, required: true, min: 1, type: "number", value: values.max_concurrent, onChange: function (event) { set("max_concurrent", event.target.value); } }) }, "Max concurrent")
        ),
        h("datalist", { id: "seat-role-suggestions" }, ["engineer", "qa", "designer"].map(function (role) { return h("option", { key: role, value: role }); })),
        h("div", { className: "flex justify-end gap-2 border-t border-border pt-4" },
          h(Button, { type: "button", size: "sm", ghost: true, disabled: busy, onClick: props.onClose }, "Cancel"),
          h(Button, { type: "submit", size: "sm", disabled: busy }, busy ? h(Spinner, { label: "Saving" }) : props.seat ? "Save seat" : "Create seat")
        )
      )
    );
  }

  function SeatCard(props) {
    var seat = props.seat;
    return h(Card, { className: "factory-seat-card" },
      h(CardContent, { className: "flex flex-col gap-3 p-4" },
        h("div", { className: "flex items-start justify-between gap-3" },
          h("div", null, h("span", { className: "font-mondwest text-display text-xs tracking-[0.12em] text-text-tertiary" }, seat.role || "operator"), h("h3", { className: "mt-1 font-mondwest normal-case text-sm font-medium text-foreground" }, seat.name)),
          h("div", { className: "flex items-center gap-2" },
            seat.model_mismatch ? h(Badge, { className: "factory-pill text-xs", tone: "destructive", title: "seats.yaml model and profile config model disagree" }, "MISMATCH") : null,
            h(StatePill, { value: seat.paused ? "paused" : "ready" }, seat.paused ? "paused" : "active")
          )
        ),
        h("div", { className: "flex flex-wrap items-center gap-2" },
          h(MonoChip, { title: "Executor" }, seat.executor || "default executor"),
          h(MonoChip, { title: "Model" }, seat.model || "default model"),
          h(MonoChip, { title: "Reasoning" }, seat.reasoning || "default reasoning")
        ),
        h("dl", { className: "grid gap-2 text-xs" },
          h("div", { className: "flex items-baseline justify-between gap-3" }, h("dt", { className: "text-text-tertiary" }, "Profile"), h("dd", { className: "font-mono-ui text-right text-foreground" }, seat.profile || "—")),
          h("div", { className: "flex items-baseline justify-between gap-3" }, h("dt", { className: "text-text-tertiary" }, "Profile model"), h("dd", { className: "font-mono-ui text-right text-foreground" }, seat.profile_model || "not explicit")),
          h("div", { className: "flex items-baseline justify-between gap-3" }, h("dt", { className: "text-text-tertiary" }, "Concurrency"), h("dd", { className: "font-mono-ui text-right text-foreground" }, formatNumber(seat.max_concurrent)))
        ),
        h("div", { className: "flex justify-end border-t border-border pt-3" }, h(Button, { type: "button", size: "sm", ghost: true, onClick: function () { props.onEdit(seat); } }, "Edit seat"))
      )
    );
  }

  function SeatsView(props) {
    var resource = usePollingResource("/seats", props.refreshKey);
    var profiles = usePollingResource("/profiles", props.refreshKey);
    var _a = useState(null), editing = _a[0], setEditing = _a[1];
    var _b = useState(null), toast = _b[0], setToast = _b[1];
    var seats = resource.data || [];
    var template = seats.map(function (seat) { return seat.provider_config; }).find(function (config) { return config && config.provider && config.base_url && config.model; }) || null;
    useReportViewMeta(props, resource, []);
    return h("section", { className: "factory-view flex min-w-0 flex-col gap-4" },
      h(ViewHeading, { title: "Seats", description: "Who is hired: profile, model, reasoning, role, and capacity.", action: h(Button, { size: "sm", onClick: function () { setEditing({}); } }, "Create seat") }),
      h(Toast, { toast: toast, onClose: function () { setToast(null); } }),
      resource.error && resource.data !== null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) : null,
      resource.loading && resource.data === null ? h(LoadingState, { label: "Loading seats…" }) :
      resource.error && resource.data === null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) :
      seats.length === 0 ? h(EmptyState, { title: "No seats configured", description: "Hire the first operator by creating a seat contract." }) :
      h("div", { className: "factory-card-grid" }, seats.map(function (seat) { return h(SeatCard, { key: seat.name, seat: seat, onEdit: setEditing }); })),
      h(SeatDialog, { open: editing !== null, seat: editing && editing.name ? editing : null, profiles: profiles.data || [], providerTemplate: template, onClose: function () { setEditing(null); }, onSaved: function (result) { setEditing(null); setToast({ ok: true, text: "Saved seat " + result.name + "." }); resource.reload().catch(function () {}); profiles.reload().catch(function () {}); } })
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

  // Journey view — Amendment 1 items A/B/G4. ONE logical card per recipe
  // instance: steps fold inside in recipe order, attempts stack under each
  // step as immutable history, and a rejected review visibly returns work to
  // the same logical card with the rejecting verdict attached.

  var JOURNEY_CURRENT_STATES = ["ready", "running", "waiting", "blocked"];

  function parseJsonArray(raw) {
    if (!raw) return [];
    try {
      var parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_ignore) { return []; }
  }

  function parseVerdict(raw) {
    if (!raw) return null;
    try {
      var parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
    } catch (_ignore) { return null; }
  }

  function findingText(finding) {
    if (typeof finding === "string") return finding;
    if (!finding || typeof finding !== "object") return "";
    return String(finding.title || finding.summary || finding.issue || finding.message || finding.body || finding.id || "");
  }

  // The rework annotation for a PRIOR attempt is carried by the NEXT attempt
  // row (rejected_by_step_id / rejected_by_activation / verdict_json describe
  // why that rework attempt exists). Tolerate null or malformed verdict_json
  // by falling back to the recorded finding_count alone.
  function reworkAnnotation(prior, next) {
    if (!next || !next.rejected_by_step_id) return null;
    var verdict = parseVerdict(next.verdict_json) || parseVerdict(prior && prior.verdict_json);
    var findings = verdict && Array.isArray(verdict.findings) ? verdict.findings : null;
    var count = findings ? findings.length
      : prior && prior.finding_count != null ? Number(prior.finding_count)
      : next.finding_count != null ? Number(next.finding_count)
      : null;
    return {
      gate: String(next.rejected_by_step_id),
      gateActivation: next.rejected_by_activation,
      count: count,
      findings: findings || [],
    };
  }

  function reworkHeadline(rework) {
    var text = "Rework ordered by " + rework.gate;
    if (rework.gateActivation != null) text += " (attempt " + rework.gateActivation + ")";
    if (rework.count != null) text += " — " + rework.count + (Number(rework.count) === 1 ? " finding" : " findings");
    return text;
  }

  function receiptsForAttempt(receipts, stepId, activation) {
    if (!Array.isArray(receipts)) return [];
    return receipts.filter(function (row) {
      return row.step_id === stepId && Number(row.activation) === Number(activation);
    });
  }

  function RunContentBlock(props) {
    var block = props.block;
    if (!block || !block.open) return null;
    return h("div", { className: "factory-journey-run" },
      h("div", { className: "flex flex-wrap items-center gap-2 text-xs text-text-tertiary" },
        h(MonoChip, { title: "Run artifact" }, props.kind + " · " + props.runId),
        block.truncated ? h(Badge, { className: "factory-pill text-xs", tone: "warning", title: "The API truncated this content" }, "truncated") : null
      ),
      block.loading ? h(Spinner, { label: "Loading " + props.kind + "…" }) :
      block.error ? h("p", { className: "text-xs text-destructive" }, block.error) :
      h("pre", { className: "factory-journey-run-content font-mono-ui text-xs text-text-secondary" }, block.content || "(empty)")
    );
  }

  function ReceiptLine(props) {
    var receipt = props.receipt;
    var lane = (receipt.executor || "executor?") + (receipt.provider ? "/" + receipt.provider : "");
    var logBlock = props.runBlocks[receipt.run_id + ":log"];
    var promptBlock = props.runBlocks[receipt.run_id + ":prompt"];
    return h("div", { className: "factory-journey-receipt" },
      h("div", { className: "flex flex-wrap items-center gap-2 text-xs" },
        h(MonoChip, { title: "Seat" }, receipt.seat || "unassigned"),
        h(MonoChip, { title: "Executor and provider lane" }, lane),
        h(MonoChip, { title: "Resolved model" }, receipt.resolved_model || receipt.model || "model unknown"),
        receipt.duration_s != null ? h("span", { className: "font-mono-ui tabular-nums text-text-secondary", title: "Run duration" }, formatDuration(receipt.duration_s)) : null,
        receipt.exit_code != null
          ? h(StatePill, { value: Number(receipt.exit_code) === 0 ? "done" : "failed", title: "Exit code" }, "exit " + receipt.exit_code)
          : receipt.result ? h(StatePill, { value: receipt.result }) : null,
        receipt.run_id && receipt.has_log ? h(Button, {
          type: "button", size: "xs", ghost: true,
          "aria-expanded": !!(logBlock && logBlock.open),
          onClick: function () { props.onToggleRun(receipt.run_id, "log"); },
        }, "log") : null,
        receipt.run_id && receipt.has_prompt ? h(Button, {
          type: "button", size: "xs", ghost: true,
          "aria-expanded": !!(promptBlock && promptBlock.open),
          onClick: function () { props.onToggleRun(receipt.run_id, "prompt"); },
        }, "prompt") : null
      ),
      h(RunContentBlock, { runId: receipt.run_id, kind: "log", block: logBlock }),
      h(RunContentBlock, { runId: receipt.run_id, kind: "prompt", block: promptBlock })
    );
  }

  function JourneyAttempt(props) {
    var attempt = props.attempt;
    var rework = props.rework;
    return h("li", {
      className: "factory-journey-attempt " + (props.latest ? "factory-journey-attempt--current" : "factory-journey-attempt--history"),
      "aria-label": "Attempt " + attempt.activation + (props.latest ? " (current)" : " (history)"),
    },
      h("div", { className: "flex flex-wrap items-center gap-2 text-xs" },
        h("strong", { className: "font-mono-ui text-foreground" }, "attempt " + attempt.activation),
        h(StatePill, { value: attempt.state }),
        props.latest
          ? h(Badge, { className: "factory-pill text-xs", tone: "secondary", title: "The live attempt for this step" }, "current")
          : h("span", { className: "text-text-tertiary" }, "history"),
        attempt.kanban_task_id
          ? h("a", { href: "/kanban?task=" + encodeURIComponent(attempt.kanban_task_id), className: "font-mono-ui text-xs text-primary hover:underline" }, attempt.kanban_task_id)
          : null,
        attempt.updated_at || attempt.created_at ? h(Ago, { value: attempt.updated_at || attempt.created_at }) : null
      ),
      attempt.blocked_reason ? h("p", { className: "text-xs leading-relaxed text-destructive" }, attempt.blocked_reason) : null,
      rework ? h("div", { className: "factory-journey-verdict border border-destructive/30 bg-destructive/10" },
        h("strong", { className: "text-xs font-medium text-destructive" }, reworkHeadline(rework)),
        rework.findings.length ? h("ul", { className: "mt-1 grid gap-1 text-xs text-text-secondary" },
          rework.findings.slice(0, 4).map(function (finding, index) {
            return h("li", { key: index }, findingText(finding) || "finding " + (index + 1));
          }),
          rework.findings.length > 4 ? h("li", { className: "text-text-tertiary" }, "… " + (rework.findings.length - 4) + " more") : null
        ) : null
      ) : null,
      props.receipts.map(function (receipt, index) {
        return h(ReceiptLine, { key: (receipt.run_id || "receipt") + ":" + index, receipt: receipt, runBlocks: props.runBlocks, onToggleRun: props.onToggleRun });
      })
    );
  }

  function JourneyAttemptStack(props) {
    if (props.loading) return h("div", { className: "factory-journey-attempts-shell" }, h(Spinner, { label: "Loading attempt history…" }));
    if (props.error) return h("div", { className: "factory-journey-attempts-shell" },
      h(ErrorState, { title: "Attempt history could not be loaded", message: props.error, onRetry: props.onRetry })
    );
    var detail = props.detail;
    var attempts = (detail && detail.activations && detail.activations[props.stepId]) || [];
    if (!attempts.length && detail && Array.isArray(detail.steps)) {
      attempts = detail.steps.filter(function (row) { return row.step_id === props.stepId; });
    }
    attempts = attempts.slice().sort(function (a, b) { return Number(a.activation) - Number(b.activation); });
    if (!attempts.length) return h("p", { className: "factory-journey-attempts-shell text-xs text-text-tertiary" }, "No activations recorded for this step yet.");
    return h("ol", { className: "factory-journey-attempts", "aria-label": "Attempt history, oldest first" }, attempts.map(function (attempt, index) {
      return h(JourneyAttempt, {
        key: props.stepId + ":" + attempt.activation,
        attempt: attempt,
        latest: index === attempts.length - 1,
        rework: reworkAnnotation(attempt, attempts[index + 1] || null),
        receipts: receiptsForAttempt(props.receipts, props.stepId, attempt.activation),
        runBlocks: props.runBlocks,
        onToggleRun: props.onToggleRun,
      });
    }));
  }

  function JourneyStepRow(props) {
    var step = props.step;
    return h("li", { className: "factory-journey-step" + (props.current ? " factory-journey-step--current" : "") },
      h("button", {
        type: "button",
        className: "factory-journey-step-row flex w-full flex-wrap items-center gap-2 text-left text-sm",
        "aria-expanded": props.expanded,
        onClick: function () { props.onToggle(step.step_id); },
      },
        h("span", { className: "factory-journey-marker", "aria-hidden": "true" }),
        h("strong", { className: "font-medium text-foreground" }, step.step_id),
        h(StatePill, { value: step.state }),
        Number(step.activation) > 1 ? h(Badge, {
          className: "factory-pill text-xs tabular-nums", tone: "secondary",
          title: "This step re-activated; earlier attempts are kept as immutable history.",
        }, "attempt " + step.activation) : null,
        step.rejected_by_step_id && step.rejected_by_step_id !== step.step_id ? h(MonoChip, { title: "Latest attempt is rework ordered by " + step.rejected_by_step_id }, "rework ← " + step.rejected_by_step_id) : null,
        h("span", { className: "ml-auto shrink-0 text-xs text-text-tertiary" }, props.expanded ? "Fold attempts" : "Attempts")
      ),
      step.blocked_reason ? h("p", { className: "mt-1 text-xs leading-relaxed text-destructive" }, step.blocked_reason) : null,
      props.expanded ? props.children : null
    );
  }

  function JourneyCard(props) {
    var instance = props.instance;
    var _a = useState(null), expandedStep = _a[0], setExpandedStep = _a[1];
    var _b = useState(null), detail = _b[0], setDetail = _b[1];
    var _c = useState(false), detailLoading = _c[0], setDetailLoading = _c[1];
    var _d = useState(""), detailError = _d[0], setDetailError = _d[1];
    var _e = useState(null), receipts = _e[0], setReceipts = _e[1];
    var _f = useState({}), runBlocks = _f[0], setRunBlocks = _f[1];

    var steps = (instance.latest_steps || []).slice().sort(function (a, b) {
      var left = a.step_position == null ? Number.MAX_SAFE_INTEGER : Number(a.step_position);
      var right = b.step_position == null ? Number.MAX_SAFE_INTEGER : Number(b.step_position);
      return left - right;
    });
    var currentStepId = null;
    steps.some(function (step) {
      if (JOURNEY_CURRENT_STATES.indexOf(normalizedState(step.state)) >= 0) { currentStepId = step.step_id; return true; }
      return false;
    });
    var parents = parseJsonArray(instance.parent_tasks_json);

    // Mirrors InstancesView.loadDetail; receipts errors stay quiet so legacy
    // rows without receipt history render nothing noisy.
    function loadDetail() {
      setDetailLoading(true);
      setDetailError("");
      request("/instances/" + encodeURIComponent(instance.id)).then(setDetail).catch(function (err) {
        setDetailError(errorText(err));
      }).finally(function () { setDetailLoading(false); });
      request("/instances/" + encodeURIComponent(instance.id) + "/receipts").then(function (rows) {
        setReceipts(Array.isArray(rows) ? rows : []);
      }).catch(function () { setReceipts([]); });
    }

    function toggleStep(stepId) {
      if (expandedStep === stepId) { setExpandedStep(null); return; }
      setExpandedStep(stepId);
      if (detail === null && !detailLoading) loadDetail();
    }

    function toggleRun(runId, kind) {
      var key = runId + ":" + kind;
      var existing = runBlocks[key];
      function put(block) {
        setRunBlocks(function (current) {
          var next = Object.assign({}, current);
          next[key] = block;
          return next;
        });
      }
      if (existing && existing.open) { put(Object.assign({}, existing, { open: false })); return; }
      if (existing && existing.content != null) { put(Object.assign({}, existing, { open: true })); return; }
      put({ open: true, loading: true, error: "", content: null, truncated: false });
      request("/runs/" + encodeURIComponent(runId) + "/" + kind).then(function (payload) {
        put({
          open: true, loading: false, error: "",
          content: payload && payload.content != null ? String(payload.content) : "",
          truncated: !!(payload && payload.truncated),
        });
      }).catch(function (err) {
        put({ open: true, loading: false, error: errorText(err), content: null, truncated: false });
      });
    }

    return h(Card, { className: "factory-journey-card" },
      h(CardContent, { className: "flex flex-col gap-3 p-4" },
        h("div", { className: "flex flex-wrap items-start justify-between gap-3" },
          h("div", { className: "min-w-0" },
            h("span", { className: "font-mondwest text-display text-xs tracking-[0.12em] text-text-tertiary" }, instance.board),
            h("h3", { className: "mt-1 font-mono-ui text-sm font-medium text-foreground" }, instance.recipe || (instance.recipe_id + "@" + instance.recipe_version)),
            h("div", { className: "mt-2 flex flex-wrap items-center gap-2" },
              h(MonoChip, { title: "Instance" }, instance.id),
              instance.collector_task_id ? h("a", {
                href: "/kanban?task=" + encodeURIComponent(instance.collector_task_id),
                className: "max-w-full truncate font-mono-ui text-xs text-primary hover:underline",
                title: "Collector task — this journey's logical card",
              }, instance.collector_task_id) : null,
              parents.map(function (parent, index) {
                var id = typeof parent === "string" ? parent : parent && (parent.task_id || parent.id);
                if (!id) return null;
                return h(MonoChip, { key: id + ":" + index, title: "Parent task (containment overlay)" }, "parent " + id);
              })
            )
          ),
          h("div", { className: "ml-auto flex shrink-0 flex-col items-end gap-2" },
            h("div", { className: "flex items-center gap-2" },
              h(StatePill, { value: instance.status }),
              h(Ago, { value: instance.updated_at })
            ),
            steps.length ? h(Button, {
              type: "button", size: "xs", ghost: true,
              onClick: function () { toggleStep(expandedStep || currentStepId || steps[0].step_id); },
            }, expandedStep ? "Fold" : "Unfold") : null
          )
        ),
        instance.blocked_reason ? h("p", { className: "border border-destructive/30 bg-destructive/10 p-3 text-xs leading-relaxed text-destructive" }, instance.blocked_reason) : null,
        steps.length === 0 ? h("p", { className: "text-xs text-text-tertiary" }, "No step activations yet.") :
        h("ol", { className: "factory-journey-steps", "aria-label": "Journey steps in recipe order" }, steps.map(function (step) {
          return h(JourneyStepRow, {
            key: step.step_id,
            step: step,
            current: step.step_id === currentStepId,
            expanded: expandedStep === step.step_id,
            onToggle: toggleStep,
          }, h(JourneyAttemptStack, {
            stepId: step.step_id,
            loading: detailLoading,
            error: detailError,
            detail: detail,
            receipts: receipts,
            runBlocks: runBlocks,
            onToggleRun: toggleRun,
            onRetry: loadDetail,
          }));
        })),
        props.children || null
      )
    );
  }

  function JourneyView(props) {
    var resource = usePollingResource("/instances", props.refreshKey);
    var journeys = resource.data || [];
    useReportViewMeta(props, resource, journeys.map(function (item) { return item.board; }));
    return h("section", { className: "factory-view flex min-w-0 flex-col gap-4" },
      h(ViewHeading, {
        title: "Journeys",
        description: "One logical card per recipe instance; attempts fold inside as immutable history.",
        action: h("a", { href: "/kanban", className: "font-mondwest text-display text-xs tracking-[0.1em] text-text-secondary hover:text-midground" }, "Open Kanban →"),
      }),
      resource.error && resource.data !== null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) : null,
      resource.loading && resource.data === null ? h(LoadingState, { label: "Loading journeys…" }) :
      resource.error && resource.data === null ? h(ErrorState, { message: resource.error, onRetry: resource.reload }) :
      journeys.length === 0 ? h(EmptyState, {
        title: "No journeys yet",
        description: "A journey card appears when a task is matched to an active recipe.",
      }) : h("div", { className: "factory-journey-grid" }, journeys.map(function (item) {
        return h(JourneyCard, { key: item.id, instance: item });
      }))
    );
  }

  var VIEW_REGISTRY = [
    { id: "journey", label: "Journeys", component: JourneyView },
    { id: "waiting", label: "Waiting gates", component: WaitingView },
    { id: "instances", label: "Instances", component: InstancesView },
    { id: "recipes", label: "Recipes", component: RecipesView },
    { id: "seats", label: "Seats", component: SeatsView },
    { id: "costs", label: "Costs", component: CostsView },
  ];

  function FactoryHeader(props) {
    var daemon = props.status;
    var daemonBoards = daemon && Array.isArray(daemon.boards) ? daemon.boards : daemon && daemon.board ? [{ board: daemon.board, last_tick_at: daemon.last_tick_at, stale: false }] : [];
    var staleBoards = daemonBoards.filter(function (item) { return item.stale; });
    var daemonLabel = props.statusError ? "Daemon: status unavailable" : !daemon ? "Daemon: checking…" : daemon.running
      ? "Daemon: " + daemonBoards.length + (daemonBoards.length === 1 ? " board" : " boards") + (staleBoards.length ? " (" + staleBoards.length + " stale)" : "")
      : "Daemon: STOPPED — " + daemonBoards.length + (daemonBoards.length === 1 ? " board" : " boards");
    var daemonTitle = daemonLabel + (daemonBoards.length ? "\n" + daemonBoards.map(function (item) {
      return item.board + " — tick " + timeAgo(item.last_tick_at) + (item.stale ? " (stale)" : "");
    }).join("\n") : "");
    var daemonState = daemon && daemon.running ? (staleBoards.length ? "waiting" : "running") : daemon ? "stopped" : "waiting";
    return h("header", { className: "flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between" },
      h("div", { className: "min-w-0" },
        h("div", { className: "flex flex-wrap items-center gap-2" },
          h(StatePill, { value: "running" }, props.board || "All boards"),
          h(StatePill, { value: daemonState, title: daemonTitle }, daemonLabel),
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
    var _a = useState(VIEW_REGISTRY[0].id), activeId = _a[0], setActiveId = _a[1];
    var _b = useState(0), refreshKey = _b[0], setRefreshKey = _b[1];
    var _c = useState({ board: "All boards", loadedAt: null }), meta = _c[0], setMeta = _c[1];
    var statusResource = usePollingResource("/status", refreshKey);
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
        status: statusResource.data, statusError: statusResource.error,
        onRefresh: function () { setRefreshKey(function (value) { return value + 1; }); },
      }),
      h(SegmentedNav, {
        value: activeId,
        onChange: function (id) { setActiveId(id); setMeta({ board: "All boards", loadedAt: null }); },
      }),
      h(ActiveView, { refreshKey: refreshKey, onMeta: onMeta, status: statusResource.data })
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("shipfactory", FactoryPage);
  }
})();
