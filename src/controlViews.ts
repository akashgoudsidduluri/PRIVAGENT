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
      await mountEmailIntelligence(content, isCurrent);
    }
    if (index === 5) {
      const providers = await request('/api/connectors');
      if (!isCurrent()) return;
      const names: Record<string, string> = {
        google: 'Google · Gmail / Calendar / Drive',
        microsoft: 'Microsoft · Outlook / Calendar',
        slack: 'Slack · Conversations',
        github: 'GitHub · Repositories',
      };
      const guides: Record<string, { title: string; link: string; linkText: string; redirect: string; scopes: string }> = {
        google: {
          title: 'Google Cloud Setup Guide',
          link: 'https://console.cloud.google.com/apis/credentials',
          linkText: 'Google Cloud Console (Credentials)',
          redirect: 'http://localhost:8000/api/connectors/google/callback',
          scopes: 'Gmail API, Google Calendar API, Google Drive API',
        },
        microsoft: {
          title: 'Microsoft Azure Setup Guide',
          link: 'https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade',
          linkText: 'Azure Portal (App Registrations)',
          redirect: 'http://localhost:8000/api/connectors/microsoft/callback',
          scopes: 'Mail.ReadWrite, Calendars.ReadWrite',
        },
        slack: {
          title: 'Slack App Setup Guide',
          link: 'https://api.slack.com/apps',
          linkText: 'Slack API Console',
          redirect: 'http://localhost:8000/api/connectors/slack/callback',
          scopes: 'channels:read, chat:write',
        },
        github: {
          title: 'GitHub OAuth Setup Guide',
          link: 'https://github.com/settings/developers',
          linkText: 'GitHub Developer Settings',
          redirect: 'http://localhost:8000/api/connectors/github/callback',
          scopes: 'repo',
        },
      };

      content.innerHTML = '<div class="cc-grid cc-provider-grid">' + providers.map((p: any) => {
        const guide = guides[p.provider] || guides.google;
        return card(names[p.provider] || safe(p.provider),
          '<div class="cc-provider-mark">' + safe(p.provider.slice(0, 1).toUpperCase()) + '</div>' +
          '<p>Native account adapter · in-memory session</p>' +
          (p.account ? '<p style="font-size:12px;color:#8cf2ac;margin:4px 0">✓ Account: <strong>' + safe(p.account) + '</strong></p>' : '') +
          (p.error && !p.connected ? '<p style="font-size:11px;color:#f87171;margin:4px 0">⚠️ ' + safe(p.error) + '</p>' : '') +
          '<div class="cc-provider-actions">' +
          tag(p.connected ? 'Connected' : p.configured ? 'Token Expired / Disconnected' : 'Setup Required') +
          (p.connected
            ? '<button class="cc-button cc-button--outline" disabled style="color:#8cf2ac;border-color:#357a53">✓ Connected</button>'
            : '<button class="cc-button" data-toggle-guide="' + safe(p.provider) + '">Setup / Reconnect ⚙</button>') +
          (p.connected ? button('Disconnect', 'disconnect', p.provider) : '') +
          (p.connected && (p.provider === 'google' || p.provider === 'microsoft') ? '<a class="cc-button" href="#email-intelligence">Monitor Inbox ✉</a>' : '') +
          '</div>' +
          '<div class="cc-provider-form" id="form-' + safe(p.provider) + '" style="display:' + (!p.configured && !p.connected ? 'none' : 'none') + ';margin-top:14px;padding:14px;border:1px solid #3a3a3a;border-radius:8px;background:#141414;">' +
          '<h4 style="margin:0 0 8px;color:#eee;font-size:13px">⚡ ' + safe(guide.title) + '</h4>' +
          '<ol style="margin:0 0 12px;padding-left:18px;font-size:12px;color:#aaa;line-height:1.6">' +
          '<li>Open <a href="' + safe(guide.link) + '" target="_blank" rel="noopener" style="color:#8cf2ac;text-decoration:underline">' + safe(guide.linkText) + ' ↗</a>.</li>' +
          '<li>Create an <strong>OAuth Client ID</strong> (Web application) and enable <em>' + safe(guide.scopes) + '</em>.</li>' +
          '<li>Set Authorized Redirect URI to:<br>' +
          '<div style="display:flex;gap:6px;margin:4px 0">' +
          '<input type="text" readonly value="' + safe(guide.redirect) + '" style="flex:1;padding:4px 8px;background:#222;border:1px solid #444;color:#8cf2ac;font-family:monospace;font-size:11px;border-radius:4px" id="uri-' + safe(p.provider) + '">' +
          '<button type="button" class="cc-button cc-button--outline" style="padding:2px 8px;font-size:11px" data-copy-uri="' + safe(guide.redirect) + '">Copy 📋</button>' +
          '</div></li>' +
          '<li>Paste the generated Client ID & Secret below:</li>' +
          '</ol>' +
          '<label style="display:block;font-size:11px;margin-bottom:4px;color:#ccc">Client ID:' +
          '<input type="text" class="cc-input" style="width:100%;margin-top:2px;padding:6px;background:#1e1e1e;border:1px solid #444;color:#fff;border-radius:4px" placeholder="e.g. 123456789-abc.apps.googleusercontent.com" data-client-id="' + safe(p.provider) + '">' +
          '</label>' +
          '<label style="display:block;font-size:11px;margin-bottom:8px;color:#ccc">Client Secret:' +
          '<input type="password" class="cc-input" style="width:100%;margin-top:2px;padding:6px;background:#1e1e1e;border:1px solid #444;color:#fff;border-radius:4px" placeholder="OAuth Client Secret" data-client-secret="' + safe(p.provider) + '">' +
          '</label>' +
          '<div style="text-align:center;font-size:10px;color:#666;margin:8px 0">— OR QUICK TEST WITH ACCESS TOKEN —</div>' +
          '<label style="display:block;font-size:11px;margin-bottom:4px;color:#ccc">Direct Access Token (from OAuth Playground or CLI):' +
          '<input type="password" class="cc-input" style="width:100%;margin-top:2px;padding:6px;background:#1e1e1e;border:1px solid #444;color:#fff;border-radius:4px" placeholder="Paste access token (e.g. ya29...)" data-token-input="' + safe(p.provider) + '">' +
          '</label>' +
          '<label style="display:block;font-size:11px;margin-bottom:8px;color:#ccc">Refresh Token (optional, enables permanent auto-refresh):' +
          '<input type="password" class="cc-input" style="width:100%;margin-top:2px;padding:6px;background:#1e1e1e;border:1px solid #444;color:#fff;border-radius:4px" placeholder="Paste refresh token (from Step 3 of OAuth Playground)" data-refresh-token-input="' + safe(p.provider) + '">' +
          '</label>' +
          '<div style="margin-top:10px;display:flex;gap:8px">' +
          '<button class="cc-button" data-save-config="' + safe(p.provider) + '">Save & Authorize ↗</button>' +
          '<button class="cc-button cc-button--outline" data-toggle-guide="' + safe(p.provider) + '">Cancel</button>' +
          '</div></div>', 'NATIVE CONNECTION');
      }).join('') +
      '</div><p class="cc-note">Credentials are saved locally in your backend .env file and never transmitted to external servers.</p>';

      content.querySelectorAll<HTMLButtonElement>('[data-toggle-guide]').forEach(b => b.onclick = () => {
        const prov = b.dataset.toggleGuide;
        const form = content.querySelector<HTMLElement>('#form-' + prov);
        if (form) form.style.display = form.style.display === 'none' ? 'block' : 'none';
      });

      content.querySelectorAll<HTMLButtonElement>('[data-copy-uri]').forEach(b => b.onclick = async () => {
        const uri = b.dataset.copyUri || '';
        try {
          await navigator.clipboard.writeText(uri);
          b.textContent = 'Copied! ✓';
          setTimeout(() => { b.textContent = 'Copy 📋'; }, 2000);
        } catch {
          b.textContent = 'Copied!';
        }
      });

      content.querySelectorAll<HTMLButtonElement>('[data-save-config]').forEach(b => b.onclick = async () => {
        const prov = b.dataset.saveConfig;
        const tokenInput = content.querySelector<HTMLInputElement>('[data-token-input="' + prov + '"]')?.value.trim();
        const refreshInput = content.querySelector<HTMLInputElement>('[data-refresh-token-input="' + prov + '"]')?.value.trim();
        const cidInput = content.querySelector<HTMLInputElement>('[data-client-id="' + prov + '"]')?.value.trim();
        const csecInput = content.querySelector<HTMLInputElement>('[data-client-secret="' + prov + '"]')?.value.trim();
        b.disabled = true;
        try {
          let res: any;
          if (tokenInput || refreshInput) {
            res = await request('/api/connectors/' + encodeURIComponent(prov || '') + '/configure', jsonOptions('POST', {
              accessToken: tokenInput || undefined,
              refreshToken: refreshInput || undefined,
              clientId: cidInput || undefined,
              clientSecret: csecInput || undefined,
            }));
          } else if (cidInput && csecInput) {
            res = await request('/api/connectors/' + encodeURIComponent(prov || '') + '/configure', jsonOptions('POST', { clientId: cidInput, clientSecret: csecInput }));
          } else {
            throw Error('Please provide an Access Token, Refresh Token, or Client ID & Client Secret');
          }
          if (res?.authorizationUrl) {
            window.location.assign(res.authorizationUrl);
          } else {
            rerender();
          }
        } catch (err) {
          showError(b.closest('.cc-card') || content, err);
          b.disabled = false;
        }
      });

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
    if (index === 6) await mountMcp(content, isCurrent);
    if (index === 7) await mountMemory(content, isCurrent);
    if (index === 8) {
      const chats = await request('/api/chats');
      const groups = await Promise.all(chats.slice(0, 25).map((c: any) => request('/api/chats/' + encodeURIComponent(c.id) + '/artifacts')));
      if (!isCurrent()) return;
      content.innerHTML = card('Deliverables', groups.flat().map((a: any) => '<article class="cc-row"><div><small>' + safe(a.file_format?.toUpperCase()) + ' · ' + safe(a.file_size_bytes) + ' bytes</small><h3>' + safe(a.title || a.filename) + '</h3><p>' + safe(a.summary) + '</p></div><a class="cc-button" href="' + API + '/api/artifacts/' + encodeURIComponent(a.artifact_id) + '/download">Download ↗</a></article>').join('') || empty('Room for your next discovery', 'Completed research exports will appear here.'), 'ARTIFACT MEMORY');
    }
    if (index === 10) {
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
    if (index === 11) {
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

async function mountEmailIntelligence(content: HTMLElement, isCurrent: () => boolean): Promise<void> {
  const missions = await request('/api/email/missions');
  if (!isCurrent()) return;

  if (!missions || !missions.length) {
    const connectors = await request('/api/connectors');
    const connectedAcc = connectors?.find((c: any) => c.connected && c.account);
    const targetAccount = connectedAcc ? connectedAcc.account : 'demo.privagent@gmail.com';
    const targetProvider = connectedAcc ? (connectedAcc.provider === 'microsoft' ? 'outlook' : 'gmail') : 'gmail';
    const isLive = Boolean(connectedAcc);

    content.innerHTML = card('Autonomous Email Intelligence',
      '<div class="cc-hero"><div><span class="eyebrow">CONTINUOUS SCAN · SAFE CLEANUP · NEGLECT DETECTION</span>' +
      '<h2>Autonomous Email<br><em>Sentinel.</em></h2>' +
      '<p>PrivAgent continuously monitors your inbox, identifies unnecessary newsletters and marketing campaigns, detects important deadlines you may be neglecting, and safely moves authorized messages to Trash.</p>' +
      (isLive
        ? '<p style="color:#8cf2ac;margin:8px 0;font-size:13px">✓ Connected Account: <strong>' + safe(targetAccount) + '</strong></p>' +
          '<button class="cc-button" id="btnInitEmailMission">Start Continuous Monitoring for ' + safe(targetAccount) + ' ↗</button>'
        : '<p style="color:#ffcb65;margin:8px 0;font-size:13px">ℹ No live email connected yet. You can connect in the Connectors tab or start in sandbox mode.</p>' +
          '<div style="display:flex;gap:8px">' +
          '<button class="cc-button" id="btnInitEmailMission">Start in Sandbox Mode (Demo Mailbox) ↗</button>' +
          '<a class="cc-button cc-button--outline" href="#connectors">Go to Connectors ⚙</a>' +
          '</div>'
      ) +
      '</div></div>', 'INITIALIZE');
    content.querySelector('#btnInitEmailMission')?.addEventListener('click', async () => {
      try {
        await request('/api/email/missions', jsonOptions('POST', {
          account_email: targetAccount,
          provider: targetProvider,
          frequency_seconds: 300,
        }));
        rerender();
      } catch (err) {
        showError(content, err);
      }
    });
    return;
  }

  const activeMission = missions.find((m: any) => m.account_email && !m.account_email.includes('demo') && m.status === 'active') || missions[0];
  const payload = await request('/api/email/missions/' + encodeURIComponent(activeMission.id) + '/dashboard');
  if (!isCurrent()) return;

  const { mission, stats, sections } = payload;
  const isAuth = mission.policy_authorized;
  const modeBadge = isAuth
    ? '<span class="cc-tag" style="background:#1b3d2b;border-color:#357a53;color:#8cf2ac">✓ AUTONOMOUS DELETION ACTIVE</span>'
    : '<span class="cc-tag" style="background:#3d2f1b;border-color:#7a5a35;color:#ffcb65">⚠ DRY-RUN MODE (NO DELETIONS)</span>';

  // Metrics Grid
  const metricsHtml = '<div class="cc-grid cc-metrics">' +
    card('Total Processed', '<strong class="cc-number">' + safe(stats.total_processed) + '</strong><p>Messages evaluated</p>') +
    card('Unnecessary', '<strong class="cc-number">' + safe(stats.unnecessary_detected) + '</strong><p>' + safe(stats.moved_to_trash) + ' trashed · ' + safe(stats.dry_run_flagged) + ' dry-run flagged</p>') +
    card('Needs Attention', '<strong class="cc-number" style="color:#ff8888">' + safe(stats.neglected_awaiting_attention) + '</strong><p>Important unreplied</p>') +
    card('Important Handled', '<strong class="cc-number" style="color:#8cf2ac">' + safe(stats.important_handled) + '</strong><p>Attended / replied</p>') +
    '</div>';

  // Policy & Controls Banner
  const controlsHtml = card('Monitoring Controls & Deletion Policy',
    '<div class="cc-row"><div>' +
    '<small>' + safe(mission.provider.toUpperCase()) + ' · ' + safe(mission.account_email) + ' · every ' + safe(mission.frequency_seconds) + 's</small>' +
    '<h3>Status: ' + safe(mission.status.toUpperCase()) + ' ' + modeBadge + '</h3>' +
    '<p>Protected categories (<strong>Banking, Financial, Security, Academic, Recruitment, Personal, Deadlines</strong>) are strictly locked and never automatically deleted.</p>' +
    '</div><div class="cc-row-actions">' +
    (isAuth
      ? button('Switch to Dry-Run Mode', 'email-dryrun', mission.id)
      : '<button class="cc-button" data-email-authorize="' + safe(mission.id) + '">✓ Authorize Autonomous Cleanup</button>') +
    button(mission.status === 'active' ? 'Pause' : 'Resume', 'email-toggle-status', mission.id) +
    button('Sync Now ↻', 'email-sync-now', mission.id) +
    '</div></div>', 'GOVERNANCE');

  // Subtabs navigation
  const subtabsHtml = '<div class="cc-tabs" style="display:flex;gap:8px;margin:22px 0 14px;flex-wrap:wrap">' +
    '<button class="cc-button cc-tab-btn selected" data-tab="attention">Needs Attention (' + sections.needs_attention.length + ')</button>' +
    '<button class="cc-button cc-tab-btn cc-button--outline" data-tab="handled">Handled (' + sections.handled.length + ')</button>' +
    '<button class="cc-button cc-tab-btn cc-button--outline" data-tab="unnecessary">Unnecessary (' + sections.unnecessary_detected.length + ')</button>' +
    '<button class="cc-button cc-tab-btn cc-button--outline" data-tab="trash">Moved to Trash (' + sections.moved_to_trash.length + ')</button>' +
    '<button class="cc-button cc-tab-btn cc-button--outline" data-tab="uncertain">Uncertain Review (' + sections.uncertain_review.length + ')</button>' +
    '<button class="cc-button cc-tab-btn cc-button--outline" data-tab="audit">Audit Log (' + sections.audit_history.length + ')</button>' +
    '</div><div id="emailTabContent"></div>';

  content.innerHTML = metricsHtml + controlsHtml + subtabsHtml;

  // Render Subtab Content
  const tabContent = content.querySelector<HTMLElement>('#emailTabContent')!;
  const renderTab = (tabName: string) => {
    content.querySelectorAll('.cc-tab-btn').forEach(b => {
      const active = b.getAttribute('data-tab') === tabName;
      b.classList.toggle('selected', active);
      b.classList.toggle('cc-button--outline', !active);
    });

    if (tabName === 'attention') {
      tabContent.innerHTML = card('Important Emails — Needs Attention',
        sections.needs_attention.map((m: any) =>
          '<article class="cc-row cc-attention-row" style="border-left:3px solid #ff7777;padding-left:14px"><div>' +
          '<small style="color:#ffaaaa">' + safe(m.subcategory?.toUpperCase()) + (m.deadline ? ' · ⏰ DEADLINE: ' + safe(m.deadline) : '') + '</small>' +
          '<h3>' + safe(m.subject) + '</h3>' +
          '<p><strong>From:</strong> ' + safe(m.sender) + ' · <em>' + safe(m.reasoning) + '</em></p>' +
          '<p style="color:#ffd8a8"><strong>Required action:</strong> ' + safe(m.required_action || 'Review and reply') + '</p>' +
          '</div><div class="cc-row-actions">' +
          tag('Unattended') +
          button('Dismiss Alert', 'dismiss-notif', m.message_id) +
          '</div></article>'
        ).join('') || empty('All clear', 'No important emails are currently neglected.'), 'PRIORITY ATTENTION');
    } else if (tabName === 'handled') {
      tabContent.innerHTML = card('Important Emails — Handled',
        sections.handled.map((m: any) =>
          '<article class="cc-row"><div>' +
          '<small>' + safe(m.subcategory?.toUpperCase()) + '</small>' +
          '<h3>' + safe(m.subject) + '</h3>' +
          '<p><strong>From:</strong> ' + safe(m.sender) + ' · ' + safe(m.reasoning) + '</p>' +
          '</div><div class="cc-row-actions">' +
          tag('✓ Replied / Resolved') +
          '</div></article>'
        ).join('') || empty('No handled emails yet', 'Important emails you reply to will appear here.'), 'RESOLVED');
    } else if (tabName === 'unnecessary') {
      tabContent.innerHTML = card('Unnecessary Emails — Detected',
        sections.unnecessary_detected.map((m: any) =>
          '<article class="cc-row"><div>' +
          '<small>' + safe(m.subcategory?.toUpperCase()) + ' · Confidence: ' + Math.round((m.unnecessary_confidence || 0) * 100) + '%</small>' +
          '<h3>' + safe(m.subject) + '</h3>' +
          '<p><strong>From:</strong> ' + safe(m.sender) + ' · ' + safe(m.reasoning) + '</p>' +
          '</div><div class="cc-row-actions">' +
          tag(m.action_taken === 'dry_run_flagged' ? 'Dry-Run Flagged' : 'Detected') +
          '</div></article>'
        ).join('') || empty('No unnecessary emails', 'Promotions or newsletters detected will appear here.'), 'CLEANUP CANDIDATES');
    } else if (tabName === 'trash') {
      tabContent.innerHTML = card('Moved to Trash (Zero Permanent Deletions)',
        sections.moved_to_trash.map((m: any) =>
          '<article class="cc-row"><div>' +
          '<small>' + safe(m.subcategory?.toUpperCase()) + ' · Verified in Trash</small>' +
          '<h3>' + safe(m.subject) + '</h3>' +
          '<p><strong>From:</strong> ' + safe(m.sender) + ' · ' + safe(m.reasoning) + '</p>' +
          '</div><div class="cc-row-actions">' +
          tag('In Trash') +
          button('Restore to Inbox ↩', 'email-restore', m.message_id) +
          '</div></article>'
        ).join('') || empty('Trash is empty', 'Emails moved to Trash under authorized policy will appear here with instant restore option.'), 'REVERSIBLE TRASH');
    } else if (tabName === 'uncertain') {
      tabContent.innerHTML = card('Uncertain Emails — Review Required',
        sections.uncertain_review.map((m: any) =>
          '<article class="cc-row"><div>' +
          '<small>UNCERTAIN · PROTECTED</small>' +
          '<h3>' + safe(m.subject) + '</h3>' +
          '<p><strong>From:</strong> ' + safe(m.sender) + ' · ' + safe(m.reasoning) + '</p>' +
          '</div><div class="cc-row-actions">' +
          tag('Protected') +
          '</div></article>'
        ).join('') || empty('No uncertain emails', 'Ambiguous emails requiring manual verification will appear here.'), 'HUMAN REVIEW');
    } else if (tabName === 'audit') {
      tabContent.innerHTML = card('Monitoring History & Audit Log',
        sections.audit_history.map((a: any) =>
          '<article class="cc-row"><div>' +
          '<small>' + safe(a.action?.toUpperCase()) + ' · ' + new Date(a.created_at * 1000).toLocaleTimeString() + '</small>' +
          '<h3>' + safe(a.details?.subject || a.action) + '</h3>' +
          '<p>' + safe(a.details?.reason || a.details?.error || JSON.stringify(a.details)) + '</p>' +
          '</div><time>' + new Date(a.created_at * 1000).toLocaleDateString() + '</time></article>'
        ).join('') || empty('No audit entries', 'Events will appear here.'), 'ACCOUNTABILITY RECORD');
    }

    // Attach restore listeners
    tabContent.querySelectorAll<HTMLButtonElement>('[data-email-restore]').forEach(btn => {
      btn.onclick = async () => {
        const msgId = btn.dataset.emailRestore;
        btn.disabled = true;
        try {
          await request('/api/email/missions/' + encodeURIComponent(mission.id) + '/restore/' + encodeURIComponent(msgId || ''), { method: 'POST' });
          rerender();
        } catch (e) {
          showError(btn.closest('.cc-row') || tabContent, e);
          btn.disabled = false;
        }
      };
    });

    // Attach dismiss listeners
    tabContent.querySelectorAll<HTMLButtonElement>('[data-dismiss-notif]').forEach(btn => {
      btn.onclick = async () => {
        const msgId = btn.dataset.dismissNotif;
        const notif = sections.notifications?.find((n: any) => n.message_id === msgId);
        if (notif) {
          btn.disabled = true;
          try {
            await request('/api/email/notifications/' + encodeURIComponent(notif.id) + '/dismiss', { method: 'POST' });
            rerender();
          } catch (e) { showError(btn.closest('.cc-row') || tabContent, e); btn.disabled = false; }
        }
      };
    });
  };

  renderTab('attention');

  content.querySelectorAll<HTMLButtonElement>('.cc-tab-btn').forEach(btn => {
    btn.onclick = () => renderTab(btn.dataset.tab || 'attention');
  });

  // Action button event listeners
  content.querySelector<HTMLButtonElement>('[data-email-authorize]')?.addEventListener('click', async () => {
    try {
      await request('/api/email/missions/' + encodeURIComponent(mission.id) + '/authorize', jsonOptions('POST', { authorized: true }));
      rerender();
    } catch (e) { showError(content, e); }
  });

  content.querySelector<HTMLButtonElement>('[data-email-dryrun]')?.addEventListener('click', async () => {
    try {
      await request('/api/email/missions/' + encodeURIComponent(mission.id) + '/authorize', jsonOptions('POST', { authorized: false }));
      rerender();
    } catch (e) { showError(content, e); }
  });

  content.querySelector<HTMLButtonElement>('[data-email-toggle-status]')?.addEventListener('click', async () => {
    const isAct = mission.status === 'active';
    try {
      await request('/api/email/missions/' + encodeURIComponent(mission.id) + '/' + (isAct ? 'pause' : 'resume'), { method: 'POST' });
      rerender();
    } catch (err) { showError(content, err); }
  });

  content.querySelector<HTMLButtonElement>('[data-email-sync-now]')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget as HTMLButtonElement;
    btn.disabled = true;
    try {
      await request('/api/email/missions/' + encodeURIComponent(mission.id) + '/sync', { method: 'POST' });
      rerender();
    } catch (err) { showError(content, err); btn.disabled = false; }
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
