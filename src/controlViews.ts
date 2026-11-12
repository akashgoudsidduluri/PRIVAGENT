import { escapeHtml, safeImageSrc } from './sanitize';

const API = 'http://localhost:8000';
const safe = (value: unknown) => escapeHtml(String(value ?? ''));
const empty = (title: string, message: string) => '<div class="cc-empty"><span>◇</span><h3>' + title + '</h3><p>' + message + '</p></div>';
const card = (title: string, body: string, label = 'LOCAL RUNTIME') => '<section class="cc-card"><div class="cc-card-heading"><h2>' + title + '</h2><span class="eyebrow">' + label + '</span></div>' + body + '</section>';
const tag = (value: unknown) => '<span class="cc-tag">' + safe(value) + '</span>';
const button = (text: string, action: string, value: unknown) => '<button class="cc-button cc-button--outline" data-' + action + '="' + safe(value) + '">' + text + '</button>';

async function request(path: string, options?: RequestInit): Promise<any> {
  const response = await fetch(API + path, { ...options, signal: AbortSignal.timeout(9000) });
  if (!response.ok) throw Error('Backend returned ' + response.status);
  return response.json();
}
const jsonOptions = (method: string, value: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
const showError = (scope: Element, error: unknown) => {
  let note = scope.querySelector<HTMLElement>('.cc-error');
  if (!note) { note = document.createElement('p'); note.className = 'cc-error'; scope.append(note); }
  note.textContent = String(error);
};
const rerender = () => window.dispatchEvent(new Event('hashchange'));

export async function mountControlView(index: number, content: HTMLElement, isCurrent: () => boolean): Promise<void> {
  content.innerHTML = empty('Reading local runtime…', 'Connecting to localhost:8000');
  try {
    if (index === 0) {
      const [health, chats, missions] = await Promise.all([request('/api/health'), request('/api/chats'), request('/api/control/missions')]);
      if (!isCurrent()) return;
      const active = missions.filter((m: any) => m.status === 'active').length;
      content.innerHTML = '<div class="cc-hero"><div><span class="eyebrow">LESS FRICTION · MORE OVERSIGHT</span><h2>Your next move,<br><em>orchestrated.</em></h2><p>Research, navigate and build with a visible path from intent to verified evidence.</p><a class="cc-button" href="#agent-workspace">Launch workspace ↗</a></div><div class="orbit-art" aria-hidden="true"><i></i><i></i><i></i><strong>P<span>•</span></strong><small>INTENT → EVIDENCE</small></div></div>' +
        '<div class="cc-grid cc-metrics">' + card('Browser relay', '<strong class="cc-number">' + (health.extensionConnected ? 'Online' : 'Offline') + '</strong><p>Live backend health</p>') + card('Mission history', '<strong class="cc-number">' + chats.length + '</strong><p>Persisted conversations</p>') + card('Active monitors', '<strong class="cc-number">' + active + '</strong><p>Of ' + missions.length + ' saved missions</p>') + '</div>' +
        card('Recent work', chats.slice(0, 5).map((c: any) => '<article class="cc-row"><div><h3>' + safe(c.title) + '</h3><p>' + safe(c.status) + '</p></div><a class="cc-button cc-button--outline" href="#agent-workspace">Open workspace ↗</a></article>').join('') || empty('Your canvas is clear', 'Start a mission to see its trace here.'), 'MISSION JOURNAL');
    }
    if (index === 3) {
      const missions = await request('/api/control/missions');
      if (!isCurrent()) return;
      content.innerHTML = card('Create a monitor', '<form id="missionForm" class="cc-form"><label>Mission title<input name="title" required maxlength="120" placeholder="Track a product price"></label><label>Public URL<input name="target_url" type="url" required placeholder="https://example.org/product"></label><label>Interval in seconds<input name="interval_seconds" type="number" min="60" value="300" required></label><button class="cc-button">Create paused</button></form><p class="cc-note">New monitors start paused. Activate one when its target is ready.</p>', 'PERSISTENT WATCH') +
        card('Scheduled missions', missions.map((m: any) => '<article class="cc-row cc-mission-row"><div><small>' + safe(m.condition_type) + ' · every ' + safe(m.interval_seconds) + 's</small><h3>' + safe(m.title) + '</h3><p>' + safe(m.target_url) + '</p></div><div class="cc-row-actions">' + tag(m.status) + button(m.status === 'active' ? 'Pause' : 'Activate', 'mission-action', m.id) + button('View diffs', 'mission-events', m.id) + '</div></article>').join('') || empty('No monitors yet', 'Create a persistent watch above.'), 'MONITORING ENGINE') + '<div id="missionDiffs"></div>';
      content.querySelector<HTMLFormElement>('#missionForm')?.addEventListener('submit', async event => {
        event.preventDefault(); const form = event.currentTarget as HTMLFormElement;
        const values = Object.fromEntries(new FormData(form));
        try { await request('/api/control/missions', jsonOptions('POST', { ...values, interval_seconds: Number(values.interval_seconds) })); rerender(); }
        catch (error) { showError(form, error); }
      });
      content.querySelectorAll<HTMLButtonElement>('[data-mission-action]').forEach(b => b.onclick = async () => {
        const active = b.textContent === 'Pause'; b.disabled = true;
        try { await request('/api/control/missions/' + encodeURIComponent(b.dataset.missionAction || ''), jsonOptions('PATCH', { status: active ? 'paused' : 'active' })); rerender(); }
        catch (error) { showError(b.closest('.cc-row') || content, error); b.disabled = false; }
      });
      content.querySelectorAll<HTMLButtonElement>('[data-mission-events]').forEach(b => b.onclick = async () => {
        const target = content.querySelector<HTMLElement>('#missionDiffs')!;
        try { const events = await request('/api/control/missions/' + encodeURIComponent(b.dataset.missionEvents || '') + '/events');
          target.innerHTML = card('Detected changes', events.map((e: any) => '<article class="cc-row"><div><h3>' + safe(e.title || 'Change detected') + '</h3><p>' + safe(e.summary) + '</p></div><time>' + new Date(e.detected_at * 1000).toLocaleString() + '</time></article>').join('') || empty('No changes recorded', 'The next observed difference will appear here.'), 'EVENT HISTORY');
        } catch (error) { showError(target, error); }
      });
    }
    if (index === 4) {
      const providers = await request('/api/connectors');
      if (!isCurrent()) return;
      const names: Record<string, string> = { google: 'Google · Gmail / Calendar / Drive', microsoft: 'Microsoft · Outlook', slack: 'Slack · Conversations', github: 'GitHub · Repositories' };
      content.innerHTML = '<div class="cc-grid cc-provider-grid">' + providers.map((p: any) => card(names[p.provider] || safe(p.provider), '<div class="cc-provider-mark">' + safe(p.provider.slice(0, 1).toUpperCase()) + '</div><p>Native account adapter · in-memory session</p><div class="cc-provider-actions">' + tag(p.connected ? 'Connected' : p.configured ? 'Ready to connect' : 'Client not configured') + '<button class="cc-button" data-connect="' + safe(p.provider) + '" ' + (!p.configured || p.connected ? 'disabled' : '') + '>Connect ↗</button>' + (p.connected ? button('Disconnect', 'disconnect', p.provider) : '') + '</div>', 'NATIVE CONNECTION')).join('') + '</div><p class="cc-note">OAuth client IDs and secrets must be configured on the local backend. Access tokens remain in process memory.</p>';
      content.querySelectorAll<HTMLButtonElement>('[data-connect]').forEach(b => b.onclick = async () => {
        try { const result = await request('/api/connectors/' + encodeURIComponent(b.dataset.connect || '') + '/start', { method: 'POST' }); const url = new URL(result.authorizationUrl);
          if (!['accounts.google.com','login.microsoftonline.com','slack.com','github.com'].includes(url.hostname) || url.protocol !== 'https:') throw Error('Unexpected OAuth destination');
          window.location.assign(url.href);
        } catch (error) { showError(b.closest('.cc-card') || content, error); }
      });
      content.querySelectorAll<HTMLButtonElement>('[data-disconnect]').forEach(b => b.onclick = async () => {
        try { await request('/api/connectors/' + encodeURIComponent(b.dataset.disconnect || ''), { method: 'DELETE' }); rerender(); }
        catch (error) { showError(b.closest('.cc-card') || content, error); }
      });
    }
    if (index === 5) await mountMcp(content, isCurrent);
    if (index === 6) await mountMemory(content, isCurrent);
    if (index === 7) {
      const chats = await request('/api/chats');
      const groups = await Promise.all(chats.slice(0, 25).map((c: any) => request('/api/chats/' + encodeURIComponent(c.id) + '/artifacts')));
      if (!isCurrent()) return;
      content.innerHTML = card('Deliverables', groups.flat().map((a: any) => '<article class="cc-row"><div><small>' + safe(a.file_format?.toUpperCase()) + ' · ' + safe(a.file_size_bytes) + ' bytes</small><h3>' + safe(a.title || a.filename) + '</h3><p>' + safe(a.summary) + '</p></div><a class="cc-button" href="' + API + '/api/artifacts/' + encodeURIComponent(a.artifact_id) + '/download">Download ↗</a></article>').join('') || empty('Room for your next discovery', 'Completed research exports will appear here.'), 'ARTIFACT MEMORY');
    }
    if (index === 9) {
      const info = await request('/api/control/privacy');
      if (!isCurrent()) return;
      const counts = info.counters;
      content.innerHTML = '<div class="cc-grid cc-metrics">' + card('Observations filtered', '<strong class="cc-number">' + safe(counts.observations) + '</strong><p>This backend process</p>') + card('Redacted observations', '<strong class="cc-number">' + safe(counts.redactedObservations) + '</strong><p>Pattern-based masking</p>') + card('Screenshots withheld', '<strong class="cc-number">' + safe(counts.screenshotsWithheld) + '</strong><p>Privacy policy decisions</p>') + '</div>' + card('Screenshot consent', '<div class="cc-toggle-row"><div><h3>Remote vision input</h3><p>Screenshots are withheld by default and when detected sensitive content is present. This setting resets when the backend restarts.</p></div><label class="cc-switch"><input type="checkbox" id="screenshotToggle" ' + (info.screenshotsAllowed ? 'checked' : '') + '><span aria-hidden="true"></span><b>' + (info.screenshotsAllowed ? 'On' : 'Off') + '</b></label></div>', 'OPERATOR CONTROL') + card('Recent privacy decisions', (info.events || []).slice(0, 25).map((e: any) => '<article class="cc-row"><div><h3>' + safe(String(e.type).replaceAll('_', ' ')) + '</h3><p>' + new Date(e.at * 1000).toLocaleString() + '</p></div>' + tag('Local') + '</article>').join('') || empty('No redactions recorded', 'Privacy decisions from this backend process will appear here.'), 'NO RAW VALUES STORED') + card('Privacy policy', '<div class="cc-grid">' + ['Local redaction before planning','Credential and PII storage boundaries','One-use mutation approvals'].map(x => '<div class="cc-policy"><span>✓</span><p>' + x + '</p></div>').join('') + '</div><p class="cc-note">Counters report detections, not a guarantee that every private value in arbitrary pixels was recognized.</p>', 'SENTINEL');
      content.querySelector<HTMLInputElement>('#screenshotToggle')?.addEventListener('change', async event => {
        const input = event.currentTarget as HTMLInputElement;
        try { await request('/api/control/privacy/screenshot', jsonOptions('POST', { enabled: input.checked })); rerender(); }
        catch (error) { input.checked = !input.checked; showError(input.closest('.cc-card') || content, error); }
      });
    }
    if (index === 10) {
      const [health, config, timeouts] = await Promise.all([request('/api/health'), request('/api/llm/config'), request('/api/control/settings')]);
      if (!isCurrent()) return;
      content.innerHTML = card('Planner configuration', '<div class="cc-settings-grid"><div><small>ACTIVE PROVIDER</small><strong>' + safe(config.provider || health.provider) + '</strong></div><div><small>STEP BUDGET</small><strong>' + safe(health.maxSteps) + '</strong></div><div><small>BROWSER RELAY</small><strong>' + (health.extensionConnected ? 'Online' : 'Offline') + '</strong></div></div><p>Provider and model changes apply to the next mission. The runtime keeps API keys write-only.</p><button class="cc-button" id="ccSettings">Configure provider & model ↗</button>', 'RUNTIME SETTINGS') + card('Mission timeouts', '<form class="cc-form cc-form--timeouts" id="timeoutForm"><label>Task timeout (seconds)<input name="taskSeconds" type="number" min="60" max="3600" value="' + safe(timeouts.taskSeconds) + '" required></label><label>Planner timeout (seconds)<input name="plannerSeconds" type="number" min="5" max="180" value="' + safe(timeouts.plannerSeconds) + '" required></label><button class="cc-button">Apply for next mission</button></form><p class="cc-note">Changes are in-memory and reset when the backend restarts.</p>', 'TIME BUDGET');
      content.querySelector<HTMLButtonElement>('#ccSettings')!.onclick = () => document.querySelector<HTMLButtonElement>('#btnLlmSettings')!.click();
      content.querySelector<HTMLFormElement>('#timeoutForm')!.onsubmit = async event => {
        event.preventDefault(); const form = event.currentTarget as HTMLFormElement;
        const values = Object.fromEntries(new FormData(form));
        try { await request('/api/control/settings', jsonOptions('POST', { taskSeconds: Number(values.taskSeconds), plannerSeconds: Number(values.plannerSeconds) })); rerender(); }
        catch (error) { showError(form, error); }
      };
    }
  } catch (error) {
    if (isCurrent()) content.innerHTML = empty('Runtime unavailable', safe(error) + '. Start the local backend to load live data.');
  }
}

async function mountMcp(content: HTMLElement, isCurrent: () => boolean): Promise<void> {
  const [servers, tools] = await Promise.all([request('/api/mcp/servers'), request('/api/mcp/tools')]);
  if (!isCurrent()) return;
  content.innerHTML = card('Register a server', '<form id="mcpServerForm" class="cc-form cc-form--mcp"><label>Server name<input name="name" required pattern="[A-Za-z0-9_-]+" placeholder="trusted_tools"></label><label>Transport<select name="transport"><option value="sse">HTTPS / SSE</option><option value="stdio">Local executable</option></select></label><label>HTTPS URL or absolute executable path<input name="endpoint" required placeholder="https://tools.example.org/sse"></label><label>Read-only tools (comma separated)<input name="readOnlyTools" placeholder="lookup, search"></label><button class="cc-button">Register</button></form><p class="cc-note">Only register servers you trust. Tools are treated as mutating unless explicitly marked read-only.</p>', 'TRUSTED CONFIGURATION') +
    card('Servers', servers.map((s: any) => '<article class="cc-row"><div><h3>' + safe(s.name) + '</h3><p>' + safe(s.transport) + ' · ' + safe(s.tools?.length || 0) + ' tools</p></div>' + button('Discover tools', 'discover', s.name) + '</article>').join('') || empty('No servers registered', 'Add a trusted server to discover its tools.'), 'DISCOVERY') +
    card('Tool catalog', tools.map((t: any) => '<article class="cc-row"><div><small>' + safe(t.server) + '</small><h3>' + safe(t.name) + '</h3><p>' + safe(t.description || 'No description supplied') + '</p></div><div class="cc-row-actions">' + tag(t.mutating ? 'Approval required' : 'Read only') + button('Inspect', 'tool', t.name) + '</div></article>').join('') || empty('No tools discovered', 'Discover a server to inspect its schema and permissions.'), 'SCHEMA · PERMISSIONS') + '<div id="mcpToolDetail"></div>';
  content.querySelector<HTMLFormElement>('#mcpServerForm')!.onsubmit = async event => {
    event.preventDefault(); const form = event.currentTarget as HTMLFormElement;
    const values = Object.fromEntries(new FormData(form)); const transport = String(values.transport);
    const endpoint = String(values.endpoint).trim();
    try { await request('/api/mcp/servers', jsonOptions('POST', { name: values.name, transport,
      ...(transport === 'stdio' ? { command: endpoint } : { url: endpoint }),
      readOnlyTools: String(values.readOnlyTools || '').split(',').map(x => x.trim()).filter(Boolean) })); rerender(); }
    catch (error) { showError(form, error); }
  };
  content.querySelectorAll<HTMLButtonElement>('[data-discover]').forEach(b => b.onclick = async () => {
    b.disabled = true;
    try { await request('/api/mcp/servers/' + encodeURIComponent(b.dataset.discover || '') + '/discover', { method: 'POST' }); rerender(); }
    catch (error) { showError(b.closest('.cc-row') || content, error); b.disabled = false; }
  });
  content.querySelectorAll<HTMLButtonElement>('[data-tool]').forEach(b => b.onclick = () => {
    const tool = tools.find((t: any) => t.name === b.dataset.tool);
    if (!tool) return;
    const target = content.querySelector<HTMLElement>('#mcpToolDetail')!;
    target.innerHTML = card(safe(tool.name), '<p>' + safe(tool.description) + '</p><pre class="cc-code">' + safe(JSON.stringify(tool.schema, null, 2)) + '</pre><label class="cc-field">Arguments JSON<textarea id="mcpArgs" rows="5">{}</textarea></label><button class="cc-button" id="mcpExecute">' + (tool.mutating ? 'Prepare approval & execute' : 'Execute read-only tool') + '</button><div id="mcpResult" aria-live="polite"></div>', tool.mutating ? 'MUTATION · APPROVAL REQUIRED' : 'READ ONLY');
    target.querySelector<HTMLButtonElement>('#mcpExecute')!.onclick = async () => {
      const output = target.querySelector<HTMLElement>('#mcpResult')!;
      try {
        const args = JSON.parse(target.querySelector<HTMLTextAreaElement>('#mcpArgs')!.value);
        if (typeof args !== 'object' || args === null || Array.isArray(args)) throw Error('Arguments must be a JSON object');
        const body = { name: tool.name, args };
        let gateTaskId: string | undefined;
        if (tool.mutating) {
          const gate = await request('/api/mcp/prepare', jsonOptions('POST', body));
          gateTaskId = gate.gateTaskId;
          const approved = window.confirm('Approve ' + tool.name + ' with these exact arguments?\n' + JSON.stringify(args, null, 2));
          await request('/api/task/' + encodeURIComponent(gateTaskId || '') + '/confirm', jsonOptions('POST', { decision: approved ? 'approve' : 'reject' }));
          if (!approved) { output.textContent = 'Tool call rejected.'; return; }
        }
        const result = await request('/api/mcp/execute', jsonOptions('POST', { ...body, gateTaskId }));
        output.textContent = (tool.mutating ? 'Call returned; external effect is unverified.\n' : '') + result.content;
      } catch (error) { output.textContent = String(error); }
    };
  });
}

async function mountMemory(content: HTMLElement, isCurrent: () => boolean): Promise<void> {
  const [facts, preferences, chats] = await Promise.all([request('/api/memory/facts'), request('/api/preferences'), request('/api/chats')]);
  if (!isCurrent()) return;
  content.innerHTML = card('Remember a fact', '<form class="cc-form cc-form--memory" id="factForm"><label>Category<select name="category"><option value="coding">Coding</option><option value="retail">Retail</option><option value="personal">Personal</option></select></label><label>Key<input name="key" required maxlength="80" placeholder="Preferred language"></label><label>Value<input name="value" required maxlength="1000" placeholder="Python"></label><label>Retention (days)<input type="number" name="ttlDays" min="1" max="365" value="90"></label><button class="cc-button">Remember</button></form><p class="cc-note">Credentials and personal identifiers are rejected. Facts expire automatically.</p>', 'EXPLICIT MEMORY') +
    card('Semantic facts', facts.map((f: any) => '<article class="cc-row"><div><small>' + safe(f.category) + ' · expires ' + new Date(f.expires_at * 1000).toLocaleDateString() + '</small><h3>' + safe(f.fact_key) + '</h3><p>' + safe(f.fact_value) + '</p></div>' + button('Forget', 'forget', f.id) + '</article>').join('') || empty('No saved facts', 'Store a stable preference above when useful.'), 'RETENTION') +
    card('Preferences', Object.entries(preferences).map(([key, value]) => '<article class="cc-row"><div><h3>' + safe(key) + '</h3><p>' + safe(value) + '</p></div>' + button('Delete', 'preference', key) + '</article>').join('') || empty('No preferences', 'Provider and workspace preferences will appear here.'), 'USER CONTROL') +
    card('Episodic history', chats.slice(0, 15).map((c: any) => '<article class="cc-row"><div><h3>' + safe(c.title) + '</h3><p>' + safe(c.status) + '</p></div><div class="cc-row-actions">' + button('Inspect', 'history', c.id) + button('Delete chat', 'delete-chat', c.id) + '</div></article>').join('') || empty('No episodes', 'Past missions will appear here.'), 'MISSION RECORDS') + '<div id="episodeDetail"></div>';
  content.querySelector<HTMLFormElement>('#factForm')!.onsubmit = async event => {
    event.preventDefault(); const form = event.currentTarget as HTMLFormElement;
    const values = Object.fromEntries(new FormData(form));
    try { await request('/api/memory/facts', jsonOptions('POST', { ...values, ttlDays: Number(values.ttlDays) })); rerender(); }
    catch (error) { showError(form, error); }
  };
  content.querySelectorAll<HTMLButtonElement>('[data-forget]').forEach(b => b.onclick = async () => {
    try { await request('/api/memory/facts/' + encodeURIComponent(b.dataset.forget || ''), { method: 'DELETE' }); rerender(); }
    catch (error) { showError(b.closest('.cc-row') || content, error); }
  });
  content.querySelectorAll<HTMLButtonElement>('[data-preference]').forEach(b => b.onclick = async () => {
    try { await request('/api/preferences/' + encodeURIComponent(b.dataset.preference || ''), { method: 'DELETE' }); rerender(); }
    catch (error) { showError(b.closest('.cc-row') || content, error); }
  });
  content.querySelectorAll<HTMLButtonElement>('[data-history]').forEach(b => b.onclick = async () => {
    const target = content.querySelector<HTMLElement>('#episodeDetail')!;
    try { const chat = await request('/api/chat/' + encodeURIComponent(b.dataset.history || ''));
      target.innerHTML = card(safe(chat.title), (chat.messages || []).map((m: any) => '<article class="cc-row"><div><small>' + safe(m.role) + '</small><p>' + safe(m.content) + '</p></div></article>').join('') || empty('No messages', 'This mission has no recorded messages.'), 'EPISODE');
    } catch (error) { showError(target, error); }
  });
  content.querySelectorAll<HTMLButtonElement>('[data-delete-chat]').forEach(b => b.onclick = async () => {
    if (!window.confirm('Delete this chat history? This cannot be undone.')) return;
    try { await request('/api/chat/' + encodeURIComponent(b.dataset.deleteChat || ''), { method: 'DELETE' }); rerender(); }
    catch (error) { showError(b.closest('.cc-row') || content, error); }
  });
}

export async function mountInspector(content: HTMLElement, isCurrent: () => boolean): Promise<void> {
  const stage = content.querySelector<HTMLElement>('.cc-inspector-data');
  const verification = content.querySelector<HTMLElement>('.cc-verification-data');
  if (!stage || !verification) return;
  try {
    const state = await request('/api/control/inspector');
    if (!isCurrent()) return;
    const semantic = state.semantic || [];
    const receipts = state.verification || [];
    stage.innerHTML = card('Semantic element map', '<p class="cc-note">' + (state.taskId ? 'Latest task · ' + safe(state.state) + ' · ' + semantic.length + ' elements' : 'Start a browser mission to populate this view.') + '</p><div class="cc-semantic-list">' + semantic.map((e: any) => '<div class="cc-semantic-row"><span>' + safe(e.role) + '</span><strong>' + safe(e.name || '(unnamed)') + '</strong><small>' + safe(e.control) + (e.bounds ? ' · ' + safe(Math.round(e.bounds.x)) + ',' + safe(Math.round(e.bounds.y)) : '') + '</small></div>').join('') + '</div>', 'LIVE PERCEPTION');
    verification.innerHTML = card('Six-layer verification', receipts.length ? receipts.map((receipt: any, i: number) => '<div class="cc-receipt"><small>RECEIPT ' + (receipts.length - i) + '</small><pre class="cc-code">' + safe(JSON.stringify(receipt, null, 2)) + '</pre></div>').join('') : empty('No action receipts', 'Verification evidence appears after an executed browser step.'), 'ACTION EVIDENCE');
    const image = document.querySelector<HTMLImageElement>('#inspectorScreenshot');
    const placeholder = document.querySelector<HTMLElement>('#inspectorScreenshotPlaceholder');
    const src = safeImageSrc(state.screenshot);
    if (src && image) { image.src = src; image.hidden = false; if (placeholder) placeholder.hidden = true; }
  } catch (error) { if (isCurrent()) stage.innerHTML = empty('Inspector unavailable', safe(error)); }
}

export async function mountActivity(content: HTMLElement, isCurrent: () => boolean): Promise<void> {
  const audit = content.querySelector<HTMLElement>('.cc-activity-audit');
  if (!audit) return;
  try {
    const receipts = await request('/api/control/activity');
    if (!isCurrent()) return;
    audit.innerHTML = card('Cross-channel activity', receipts.map((r: any) => '<article class="cc-row"><div><small>' + safe(r.type) + ' · ' + new Date(r.at * 1000).toLocaleString() + '</small><h3>' + safe(r.title) + '</h3><p>' + safe(r.detail) + '</p></div>' + tag(r.status) + '</article>').join('') || empty('No action receipts yet', 'Browser, MCP and monitoring activity will appear here.'), 'LOCAL AUDIT');
  } catch (error) { if (isCurrent()) audit.innerHTML = empty('Audit unavailable', safe(error)); }
}
