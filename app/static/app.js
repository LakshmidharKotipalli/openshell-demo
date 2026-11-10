(() => {
  const $ = (id) => document.getElementById(id);
  const el = {
    task: $("task"), url: $("url"), run: $("btn-run"), retry: $("btn-retry"), del: $("btn-del"),
    sb: $("sb-name"), dot: $("status-dot"), stext: $("status-text"), tl: $("timeline"),
    policy: $("policy"), polMeta: $("pol-meta"), applyTime: $("apply-time"), apply: $("btn-apply"),
    strict: $("btn-strict"), research: $("btn-research"), result: $("result"), banner: $("banner"),
    pMode: $("pill-mode"), pGw: $("pill-gw"), pProv: $("pill-prov"), pConn: $("pill-conn"),
  };
  let ws, lastSeq = 0, state = { status: "idle", can_retry: false }, policyDirty = false, retryDelay = 500;
  const denialCards = {};  // host -> {card, actions}

  // ---------- helpers ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
  const uid = () => Math.random().toString(36).slice(2, 9);
  function banner(msg, kind = "") {
    if (!msg) { el.banner.classList.add("hidden"); return; }
    el.banner.className = "banner " + kind; el.banner.textContent = msg;
  }
  function send(type, payload = {}) {
    if (!ws || ws.readyState !== 1) { banner("Not connected to the server — retrying…"); return; }
    banner("");
    ws.send(JSON.stringify({ id: uid(), type, ...payload }));
  }
  function addRow(ts, icon, html, cls = "", detail = "") {
    if (el.tl.querySelector(".empty")) el.tl.innerHTML = "";
    const d = document.createElement("div");
    d.className = "ev " + cls;
    d.innerHTML = `<span class="t">${clock(ts)}</span><span class="ic">${icon}</span><div class="body">${html}${detail ? `<div class="detail">${esc(detail)}</div>` : ""}</div>`;
    el.tl.appendChild(d);
    el.tl.scrollTop = el.tl.scrollHeight;
    return d;
  }

  // ---------- state ----------
  function applyState(s) {
    state = s;
    el.dot.className = "dot " + s.status;
    el.stext.textContent = s.status + (s.mode === "replay" ? " (replay)" : "");
    el.sb.textContent = s.sandbox ? "sandbox: " + s.sandbox : "no sandbox";
    const busy = s.status === "running" || s.status === "creating";
    el.run.disabled = busy; el.retry.disabled = busy || !s.can_retry; el.del.disabled = busy || !s.sandbox;
    el.apply.disabled = busy; el.strict.disabled = busy; el.research.disabled = busy;
    el.strict.classList.toggle("active", s.preset === "strict");
    el.research.classList.toggle("active", s.preset === "research");
    el.pMode.textContent = s.mode === "replay" ? "REPLAY MODE" : "live · " + s.model;
    el.pMode.className = "pill " + (s.mode === "replay" ? "warn" : "ok");
    if (s.mode === "replay") banner("Replay mode: events come from a recorded run, not a live sandbox.", "info");
  }

  // ---------- timeline ----------
  function render(ev) {
    const k = ev.kind, ts = ev.ts;
    switch (k) {
      case "sandbox_creating":
        return addRow(ts, "⏳", `Creating sandbox <code>${esc(ev.name)}</code> · image <code>${esc(ev.image)}</code> · policy <b>strict</b> · provider <code>${esc(ev.provider)}</code>`, "info");
      case "sandbox_ready":
        return addRow(ts, "✔", `Sandbox ready in ${ev.seconds}s — Landlock + seccomp + proxy active`, "ok");
      case "sandbox_deleted":
        return addRow(ts, "🗑", `Sandbox <code>${esc(ev.name)}</code> deleted`, "dim");
      case "run_started":
        return addRow(ts, "▶", `Run started · policy preset <b>${esc(ev.preset)}</b>`, "info", `${ev.task}\n${ev.url}`);
      case "agent.agent_start":
        return addRow(ts, "🤖", `Agent started · model <code>${esc(ev.model)}</code>`, "", `API key in sandbox env: ${ev.key_in_env} (real key never enters the sandbox)`);
      case "agent.llm_request":
        return addRow(ts, "💭", `Thinking (step ${esc(ev.step)})…`, "dim");
      case "agent.llm_retry":
        return addRow(ts, "↻", `Model call failed, retrying (attempt ${esc(ev.attempt)})`, "warn", ev.reason);
      case "agent.llm_response":
        return addRow(ts, "💬", ev.tools && ev.tools.length ? `Model chose tool: <code>${esc(ev.tools.join(", "))}</code>` : "Model replied with text", "dim", ev.text || "");
      case "agent.tool_call":
        return addRow(ts, "🔧", `<code>${esc(ev.tool)}</code>(${esc(JSON.stringify(ev.args))})`, "");
      case "agent.tool_result":
        return addRow(ts, ev.ok ? "✔" : "✖", `<code>${esc(ev.tool)}</code> ${ev.ok ? "returned" : "failed"} · ${esc(ev.chars)} chars`, ev.ok ? "ok" : "warn", ev.preview);
      case "agent.policy_denied":
        return addRow(ts, "⛔", `Agent: network request to <code>${esc(ev.host)}</code> was blocked`, "bad", ev.detail);
      case "agent.fs_denied":
        return addRow(ts, "⛔", `Agent: file access denied <code>${esc(ev.path)}</code> (Landlock)`, "bad", ev.detail);
      case "agent.final":
        return addRow(ts, ev.status === "ok" ? "🏁" : "■", `Agent finished: <b>${esc(ev.status)}</b>`, ev.status === "ok" ? "ok" : "warn", ev.summary || "");
      case "agent.error": case "error":
        return addRow(ts, "✖", esc(ev.message || ev.detail || "error"), "bad");
      case "stderr":
        return addRow(ts, "·", "stderr", "dim", ev.line);
      case "denial": return renderDenial(ev);
      case "blocked":
        return addRow(ts, "⏸", esc(ev.message), "warn");
      case "policy_applying":
        return addRow(ts, "⚙", `Applying policy (${esc(ev.label)})…`, "info");
      case "policy_applied":
        markAllowed(ev);
        return addRow(ts, "✔", `Policy live in <b>${ev.seconds}s</b> (${esc(ev.label)})`, "ok");
      case "done":
        return addRow(ts, "🏁", esc(ev.message), "ok");
      case "info":
        return addRow(ts, "ℹ", esc(ev.message), "dim");
      default:
        return addRow(ts, "·", esc(k), "dim", JSON.stringify(ev));
    }
  }

  function renderDenial(ev) {
    if (el.tl.querySelector(".empty")) el.tl.innerHTML = "";
    const card = document.createElement("div");
    card.className = "denial";
    const what = ev.scope === "http" ? `${esc(ev.method)} ${esc(ev.path)}` : "TCP/TLS connection";
    card.innerHTML = `<h3>⛔ Policy denial — ${esc(ev.host)}:${ev.port}</h3>
      <div class="kv"><span>host</span><span>${esc(ev.host)}</span><span>port</span><span>${ev.port}</span>
      <span>binary</span><span>${esc(ev.binary)}</span><span>request</span><span>${what}</span>
      <span>reason</span><span>${esc(ev.reason || "not allowed by any policy")}</span></div>
      <div class="actions"></div>`;
    const actions = card.querySelector(".actions");
    el.tl.appendChild(card);
    el.tl.scrollTop = el.tl.scrollHeight;
    if (ev.scope === "http") {
      // The host is reachable but this method/path is not. A read-only host rule would change nothing,
      // so there is no one-click fix: the operator decides how far to widen access.
      actions.textContent = "This host is already reachable; the request's method or path is not allowed. Edit the rule's access in the policy YAML and click “Apply edited YAML”.";
      return;
    }
    const allow = document.createElement("button");
    allow.className = "allow"; allow.textContent = "Allow this host";
    allow.title = `Adds a minimal read-only rule for ${ev.host}:${ev.port} limited to ${ev.binary}`;
    allow.onclick = () => { allow.disabled = true; send("allow_host", { host: ev.host, port: ev.port, binary: ev.binary }); };
    actions.appendChild(allow);
    denialCards[ev.host] = { card, actions, allow };
  }

  function markAllowed(ev) {
    const host = (ev.label || "").startsWith("allow:") ? ev.label.slice(6) : null;
    const c = host && denialCards[host];
    if (!c) return;
    c.card.classList.add("allowed");
    c.card.querySelector("h3").textContent = `✔ Allowed ${host} — rule ${ev.rule} applied live in ${ev.seconds}s`;
    c.allow.remove();
    const b = document.createElement("button");
    b.className = "primary"; b.textContent = "Retry task"; b.onclick = () => send("retry");
    c.actions.appendChild(b);
  }

  // ---------- policy / result ----------
  function applyPolicyMsg(m) {
    if (!policyDirty || m.applied_seconds != null) { el.policy.value = m.yaml; policyDirty = false; }
    el.polMeta.textContent = (m.sandbox ? m.sandbox + " · " : "preview (no sandbox yet) · ") + (m.version ? "revision " + m.version + " · " : "") + m.preset;
    if (m.applied_seconds != null) {
      el.applyTime.textContent = `applied in ${m.applied_seconds}s`;
      el.applyTime.classList.remove("hidden");
    }
  }
  function showResult(m) {
    el.result.innerHTML = `<div class="file">${esc(m.file)}</div>${esc(m.text || m.summary || "(empty)")}`;
  }

  // ---------- websocket ----------
  function connect() {
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.onopen = () => {
      el.pConn.textContent = "connected"; el.pConn.className = "pill ok"; retryDelay = 500; banner("");
      ws.send(JSON.stringify({ id: uid(), type: "hello", last_seq: lastSeq }));
      fetch("/api/health").then((r) => r.json()).then(health).catch(() => {});
    };
    ws.onclose = () => {
      el.pConn.textContent = "disconnected"; el.pConn.className = "pill bad";
      setTimeout(connect, retryDelay); retryDelay = Math.min(retryDelay * 2, 5000);
    };
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.type === "event") { if (m.seq <= lastSeq) return; lastSeq = m.seq; render(m); }
      else if (m.type === "reset") {
        lastSeq = 0; el.tl.innerHTML = '<div class="empty">New session. Events appear here as the sandbox is created and the agent runs.</div>';
        el.result.innerHTML = '<div class="empty">The agent\'s output file appears here.</div>'; el.applyTime.classList.add("hidden");
        for (const k of Object.keys(denialCards)) delete denialCards[k];
      }
      else if (m.type === "state") applyState(m);
      else if (m.type === "policy") applyPolicyMsg(m);
      else if (m.type === "result") showResult(m);
      else if (m.type === "error") { banner("Error: " + m.message); if (m.ref) for (const c of Object.values(denialCards)) c.allow && (c.allow.disabled = false); }
    };
  }
  function health(h) {
    if (h.mode === "replay") { el.pGw.textContent = "gateway n/a"; el.pProv.textContent = "provider n/a"; return; }
    el.pGw.textContent = h.openshell ? "gateway ok" : "gateway down"; el.pGw.className = "pill " + (h.openshell ? "ok" : "bad");
    el.pProv.textContent = h.provider ? `provider ${h.provider_name}` : "provider missing"; el.pProv.className = "pill " + (h.provider ? "ok" : "bad");
    if (!h.ok) banner("Preflight problem: " + (!h.openshell ? "OpenShell gateway is not reachable (brew services restart openshell)." : "provider not found — run `make setup`."));
  }

  // ---------- wiring ----------
  el.run.onclick = () => { el.result.innerHTML = '<div class="empty">Running…</div>'; send("run_task", { task: el.task.value.trim(), url: el.url.value.trim() }); };
  el.retry.onclick = () => { el.result.innerHTML = '<div class="empty">Running…</div>'; send("retry"); };
  el.del.onclick = () => { if (confirm("Delete the sandbox?")) { send("delete_sandbox", { name: "" }); } };
  el.strict.onclick = () => send("apply_preset", { preset: "strict" });
  el.research.onclick = () => send("apply_preset", { preset: "research" });
  el.apply.onclick = () => { send("apply_policy", { yaml: el.policy.value }); policyDirty = false; };
  el.policy.addEventListener("input", () => { policyDirty = true; });
  connect();
})();
