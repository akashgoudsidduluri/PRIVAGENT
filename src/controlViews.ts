import { escapeHtml, safeImageSrc } from './sanitize';
import { apiFetch, artifactUrl } from './auth';

const API = 'http://localhost:8000';
const safe = (value: unknown) => escapeHtml(String(value ?? ''));
const empty = (title: string, message: string) => '<div class="cc-empty"><span>◇</span><h3>' + title + '</h3><p>' + message + '</p></div>';
const card = (title: string, body: string, label = 'LOCAL RUNTIME') => '<section class="cc-card"><div class="cc-card-heading"><h2>' + title + '</h2><span class="eyebrow">' + label + '</span></div>' + body + '</section>';
const tag = (value: unknown) => '<span class="cc-tag">' + safe(value) + '</span>';
const button = (text: string, action: string, value: unknown) => '<button class="cc-button cc-button--outline" data-' + action + '="' + safe(value) + '">' + text + '</button>';

async function request(path: string, options?: RequestInit, timeoutMs = 15000): Promise<any> {
  let lastError: any;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const response = await apiFetch(API + path, { ...options, signal: AbortSignal.timeout(timeoutMs) });
      if (!response.ok) throw Error('Backend returned ' + response.status);
      return await response.json();
    } catch (err: any) {
      lastError = err;
      if (attempt === 0 && (err.name === 'TimeoutError' || String(err).includes('signal timed out') || String(err).includes('Failed to fetch'))) {
        await new Promise(res => setTimeout(res, 500));
        continue;
      }
      throw err;
    }
  }
  throw lastError;
}
const jsonOptions = (method: string, value: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });

// Deduplicated /api/connectors reads: the Connectors view and the
// Email Intelligence view both need provider status, and every
// Control Center action re-mounts the view (rerender -> hashchange).
// Without this, overlapping mounts fired concurrent GET
// /api/connectors bursts — each one slow on the backend — and the
// re-mounts stacked. Concurrent callers share one in-flight request
// and rapid re-mounts reuse a fresh result for 10s; the cache expires
// on its own after that window.
let connectorsInFlight: Promise<any> | null = null;
let connectorsCache: { at: number; data: any } | null = null;
const CONNECTORS_CACHE_MS = 10000;
async function getConnectors(force = false): Promise<any> {
  const now = Date.now();
  if (!force && connectorsCache && now - connectorsCache.at < CONNECTORS_CACHE_MS) {
    return connectorsCache.data;
  }
  if (!force && connectorsInFlight) return connectorsInFlight;
  const p = request('/api/connectors').then(data => {
    connectorsCache = { at: Date.now(), data };
    return data;
  }).finally(() => {
    if (connectorsInFlight === p) connectorsInFlight = null;
  });
  connectorsInFlight = p;
  return p;
}
const showError = (scope: Element, error: unknown) => {
  let note = scope.querySelector<HTMLElement>('.cc-error');
  if (!note) { note = document.createElement('p'); note.className = 'cc-error'; scope.append(note); }
  note.textContent = String(error);
};
const rerender = () => window.dispatchEvent(new Event('hashchange'));
let emailTimerInterval: number | undefined;

export async function mountControlView(index: number, content: HTMLElement, isCurrent: () => boolean): Promise<void> {
  if (!content.children.length) {
    content.innerHTML = empty('Reading local runtime…', 'Connecting to localhost:8000');
  }
  try {
    if (index === 0) {
      const [health, chats, missions] = await Promise.all([request('/api/health'), request('/api/chats'), request('/api/control/missions')]);
      if (!isCurrent()) return;
      const active = missions.filter((m: any) => m.status === 'active').length;
      content.innerHTML = '<div class="cc-hero"><div><span class="eyebrow">LESS FRICTION · MORE OVERSIGHT</span><h2>Your next move,<br><em>orchestrated.</em></h2><p>Research, navigate and build with a visible path from intent to verified evidence.</p><a class="cc-button" href="#agent-workspace">Launch workspace ↗</a></div><div class="orbit-art" aria-hidden="true"><i></i><i></i><i></i><strong>P<span>•</span></strong><small>INTENT → EVIDENCE</small></div></div>' +
        '<div class="cc-grid cc-metrics">' + card('Browser relay', '<strong class="cc-number">' + (health.extensionConnected ? 'Online' : 'Offline') + '</strong><p>Live backend health</p>') + card('Mission history', '<strong class="cc-number">' + chats.length + '</strong><p>Persisted conversations</p>') + card('Active monitors', '<strong class="cc-number">' + active + '</strong><p>Of ' + missions.length + ' saved missions</p>') + '</div>' +
        card('Recent work', chats.slice(0, 5).map((c: any) => '<article class="cc-row"><div><h3>' + safe(c.title) + '</h3><p>' + safe(c.status) + '</p></div><a class="cc-button cc-button--outline" href="#agent-workspace">Open workspace ↗</a></article>').join('') || empty('Your canvas is clear', 'Start a mission to see its trace here.'), 'MISSION JOURNAL');
    }
    // Monitoring is chat-only (2026-11-18): there is no monitor form
    // or scheduled-missions page anymore. A watch is created by telling
    // the agent in chat ("watch the Helio strap, alert below ₹8000")
    // and its confirmation + controls live in that conversation. The
    // old index-3 view was removed; the indices below shifted down by 1.
    if (index === 3) {
      await mountEmailIntelligence(content, isCurrent);
    }
    if (index === 4) {
      // While the providers fetch is in flight the view is in the
      // 'Connecting' state.
      content.innerHTML = tag('Connecting') + '<p class="cc-note">Loading your connected services…</p>';
      const providers = await getConnectors();
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
          (p.error && !p.connected ? '<p style="font-size:11px;color:var(--danger);margin:4px 0">⚠️ ' + safe(p.error) + '</p>' : '') +
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
    if (index === 5) await mountMcp(content, isCurrent);
    if (index === 6) await mountMemory(content, isCurrent);
    if (index === 7) {
      const chats = await request('/api/chats');
      const sample = (chats || []).slice(0, 10);
      const groups = await Promise.all(sample.map((c: any) => request('/api/chats/' + encodeURIComponent(c.id) + '/artifacts').catch(() => [])));
      if (!isCurrent()) return;
      content.innerHTML = card('Deliverables', groups.flat().map((a: any) => '<article class="cc-row"><div><small>' + safe(a.file_format?.toUpperCase()) + ' · ' + safe(a.file_size_bytes) + ' bytes</small><h3>' + safe(a.title || a.filename) + '</h3><p>' + safe(a.summary) + '</p></div><a class="cc-button" href="' + artifactUrl(API + '/api/artifacts/' + encodeURIComponent(a.artifact_id) + '/download') + '">Download ↗</a></article>').join('') || empty('Room for your next discovery', 'Completed research exports will appear here.'), 'ARTIFACT MEMORY');
    }
    if (index === 9) {
      // Preserve the operator's audit filter/limit across the 5s live
      // refresh: the full re-mount below rebuilds the selects, and
      // without this both reset to their defaults every interval tick.
      const prevAuditEvent = content.querySelector<HTMLSelectElement>('#auditEventFilter')?.value;
      const prevAuditLimit = content.querySelector<HTMLSelectElement>('#auditLimit')?.value;
      const info = await request('/api/control/privacy');
      if (!isCurrent()) return;
      const counts = info.counters;
      const planner = info.planner || {};
      const trackerByCategory: Record<string, string[]> = {};
      for (const h of info.trackerHosts || []) {
        (trackerByCategory[h.category] ||= []).push(h.host);
      }
      // Task 12: Privacy Center — planner status, audit viewer, tracker
      // blocklist, DPIA summary, alongside the existing counters.
      // Planner / zero-retention flags are backend env vars: read-only
      // here, and the UI says so instead of pretending a toggle
      // changes the server.
      const plannerLine = planner.provider
        ? safe(planner.provider) + (planner.model ? ' · ' + safe(planner.model) : '')
          + (planner.local ? ' · runs on this machine' : ' · cloud')
          + (planner.keyConfigured ? '' : ' · no key configured')
        : 'No planner configured';
      const plannerCard = card('Planner status', '<div class="cc-settings-grid"><div><small>ACTIVE PLANNER</small><strong>' + plannerLine + '</strong></div><div><small>LOCAL-ONLY MODE</small><strong>' + (planner.localOnly ? 'On' : 'Off') + '</strong></div><div><small>ZERO RETENTION</small><strong>' + (info.zeroRetention ? 'On' : 'Off') + '</strong></div></div><p>Local-only and zero retention are set by PRIVAGENT_PLANNER_LOCAL_ONLY and PRIVAGENT_ZERO_RETENTION on the backend — read-only here. ' + (planner.localOnly ? 'Local-only is on: the planner never falls back to a cloud provider.' : 'Local-only is off: the planner may use a cloud provider.') + ' ' + (info.zeroRetention ? 'Zero retention is on: tasks leave no checkpoints, screenshots, or history.' : 'Zero retention is off: checkpoints and trajectories are kept on this machine.') + '</p>', 'TASK 10 · LOCAL FIRST');
      // Task 14: connector configuration status. `configured` is a
      // local check (credentials present in the encrypted store) —
      // liveness is only known when a call is made, and the router
      // fails honestly when the connector is not configured.
      const googleConfigured = Boolean((info.connectors || {}).google?.configured);
      const connectorsCard = card('Connectors', '<div class="cc-settings-grid"><div><small>GOOGLE (GMAIL · CALENDAR · DRIVE)</small><strong>' + (googleConfigured ? 'Configured' : 'Not connected') + '</strong></div></div><p>' + (googleConfigured ? 'Google credentials are stored locally, so Gmail, Calendar and Drive tasks route through the native API connectors instead of driving the web UI. This is not a live sign-in check — a task still fails honestly if no account token is present.' : 'No Google account is connected, so Gmail, Calendar and Drive tasks fail honestly instead of driving the web UI with your credentials. Connect it in the Connectors tab of the Control Center.') + '</p>', 'TASK 14 · NATIVE APIS');
      const auditCard = card('Audit log', '<div class="cc-audit-controls"><label>Event <select id="auditEventFilter"><option value="">All events</option>' + ['auth_failure', 'allowlist_denied', 'confirmation_approved', 'confirmation_denied', 'takeover_started', 'takeover_ended', 'payment_attempt', 'memory_write', 'memory_delete', 'memory_forget_all', 'scheduler_claim'].map(e => '<option value="' + e + '">' + e + '</option>').join('') + '</select></label><label>Limit <select id="auditLimit"><option value="25">25</option><option value="50">50</option><option value="100">100</option></select></label><button class="cc-button cc-button--outline" id="auditRefresh">Refresh</button><span id="auditChain" class="cc-tag">Checking chain…</span></div><div id="auditEntries" aria-live="polite"><p class="cc-note">Loading audit entries…</p></div><p class="cc-note">Append-only, hash-chained log on the backend. Summaries are scrubbed — no secret ever lands in the file. A broken chain means the log was edited after the fact.</p>', 'TASK 10 · TAMPER-EVIDENT');
      const trackerCard = card('Tracker blocklist', '<p>' + safe(info.trackerNote || 'The agent profile blocks known tracker, ad, session-replay and fingerprinting hosts.') + '</p>' + Object.entries(trackerByCategory).map(([cat, hosts]) => '<div class="cc-tracker-group"><h3>' + safe(cat) + ' (' + hosts.length + ')</h3><p>' + hosts.map(h => safe(h)).join(', ') + '</p></div>').join('') || empty('No tracker hosts', 'The blocklist is empty.'), 'TASK 10 · 24 HOSTS');
      const dpiaCard = card('Data protection impact assessment', '<p>' + safe((info.dpia || {}).summary || '') + '</p><p><a class="cc-button cc-button--outline" href="' + safe((info.dpia || {}).url || 'https://github.com/Prateek2007-cmd/PRIVAGENT/blob/main/docs/DPIA.md') + '" target="_blank" rel="noopener noreferrer">Read the full DPIA ↗</a></p>', 'DPIA');
      content.innerHTML = '<div class="cc-grid cc-metrics">' + card('Observations filtered', '<strong class="cc-number">' + safe(counts.observations) + '</strong><p>This backend process</p>') + card('Redacted observations', '<strong class="cc-number">' + safe(counts.redactedObservations) + '</strong><p>Pattern-based masking</p>') + card('Screenshots withheld', '<strong class="cc-number">' + safe(counts.screenshotsWithheld) + '</strong><p>Privacy policy decisions</p>') + '</div>' + card('Screenshot consent', '<div class="cc-toggle-row"><div><h3>Remote vision input</h3><p>Screenshots are withheld by default and when detected sensitive content is present. This setting resets when the backend restarts.</p></div><label class="cc-switch"><input type="checkbox" id="screenshotToggle" ' + (info.screenshotsAllowed ? 'checked' : '') + '><span aria-hidden="true"></span><b>' + (info.screenshotsAllowed ? 'On' : 'Off') + '</b></label></div>', 'OPERATOR CONTROL') + card('Recent privacy decisions', (info.events || []).slice(0, 25).map((e: any) => '<article class="cc-row"><div><h3>' + safe(String(e.type).replaceAll('_', ' ')) + '</h3><p>' + new Date(e.at * 1000).toLocaleString() + '</p></div>' + tag('Local') + '</article>').join('') || empty('No redactions recorded', 'Privacy decisions from this backend process will appear here.'), 'NO RAW VALUES STORED') + card('Privacy policy', '<div class="cc-grid">' + ['Local redaction before planning','Credential and PII storage boundaries','One-use mutation approvals'].map(x => '<div class="cc-policy"><span>✓</span><p>' + x + '</p></div>').join('') + '</div><p class="cc-note">Counters report detections, not a guarantee that every private value in arbitrary pixels was recognized.</p>', 'SENTINEL') + plannerCard + connectorsCard + auditCard + trackerCard + dpiaCard;
      // Restore the operator's audit filter/limit (read above) so the
      // audit list below loads with their selection, not the defaults.
      if (prevAuditEvent !== undefined) {
        const eventFilter = content.querySelector<HTMLSelectElement>('#auditEventFilter');
        if (eventFilter) eventFilter.value = prevAuditEvent;
      }
      if (prevAuditLimit !== undefined) {
        const limitSel = content.querySelector<HTMLSelectElement>('#auditLimit');
        if (limitSel) limitSel.value = prevAuditLimit;
      }
      content.querySelector<HTMLInputElement>('#screenshotToggle')?.addEventListener('change', async event => {
        const input = event.currentTarget as HTMLInputElement;
        try { await request('/api/control/privacy/screenshot', jsonOptions('POST', { enabled: input.checked })); rerender(); }
        catch (error) { input.checked = !input.checked; showError(input.closest('.cc-card') || content, error); }
      });
      const loadAudit = async () => {
        const entriesEl = content.querySelector<HTMLElement>('#auditEntries');
        const chainEl = content.querySelector<HTMLElement>('#auditChain');
        const eventFilter = content.querySelector<HTMLSelectElement>('#auditEventFilter')?.value || '';
        const limit = content.querySelector<HTMLSelectElement>('#auditLimit')?.value || '25';
        if (!entriesEl) return;
        try {
          const audit = await request('/api/audit?limit=' + encodeURIComponent(limit) + (eventFilter ? '&event=' + encodeURIComponent(eventFilter) : ''));
          if (chainEl) {
            chainEl.textContent = audit.chainOk ? '✓ Chain intact' : '⚠ Chain broken at entry ' + audit.brokenIndex;
          }
          const entries = audit.entries || [];
          entriesEl.innerHTML = entries.length
            ? entries.map((e: any) => '<article class="cc-row"><div><small>' + safe(e.event) + ' · ' + new Date((e.ts || 0) * 1000).toLocaleString() + '</small><p>' + safe(e.summary || '') + '</p></div>' + tag('Local') + '</article>').join('')
            : empty('No audit entries', 'No events match this filter yet.');
        } catch (error) {
          if (chainEl) chainEl.textContent = 'Audit unavailable';
          entriesEl.innerHTML = empty('Audit unavailable', safe(error));
        }
      };
      content.querySelector<HTMLSelectElement>('#auditEventFilter')?.addEventListener('change', () => void loadAudit());
      content.querySelector<HTMLSelectElement>('#auditLimit')?.addEventListener('change', () => void loadAudit());
      content.querySelector<HTMLButtonElement>('#auditRefresh')?.addEventListener('click', () => void loadAudit());
      void loadAudit();
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
  content.innerHTML = card('Remember a fact', '<p>Facts are learned from the conversation now — tell the agent in chat, for example “remember my preferred language is Python”, and it stores the fact (same 90-day retention, same credential/PII rejection as the old form). There is no manual entry form anymore.</p><a class="cc-button" href="#agent-workspace">Open workspace ↗</a>', 'CHAT-ONLY MEMORY') +
    card('Semantic facts', facts.map((f: any) => '<article class="cc-row"><div><small>' + safe(f.category) + ' · expires ' + new Date(f.expires_at * 1000).toLocaleDateString() + '</small><h3>' + safe(f.fact_key) + '</h3><p>' + safe(f.fact_value) + '</p></div>' + button('Forget', 'forget', f.id) + '</article>').join('') || empty('No saved facts', 'Tell the agent in chat — “remember my preferred language is Python”.'), 'RETENTION') +
    card('Preferences', Object.entries(preferences).map(([key, value]) => '<article class="cc-row"><div><h3>' + safe(key) + '</h3><p>' + safe(value) + '</p></div>' + button('Delete', 'preference', key) + '</article>').join('') || empty('No preferences', 'Provider and workspace preferences will appear here.'), 'USER CONTROL') +
    card('Episodic history', chats.slice(0, 15).map((c: any) => '<article class="cc-row"><div><h3>' + safe(c.title) + '</h3><p>' + safe(c.status) + '</p></div><div class="cc-row-actions">' + button('Inspect', 'history', c.id) + button('Delete chat', 'delete-chat', c.id) + '</div></article>').join('') || empty('No episodes', 'Past missions will appear here.'), 'MISSION RECORDS') + '<div id="episodeDetail"></div>';
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
  if (emailTimerInterval !== undefined) {
    window.clearInterval(emailTimerInterval);
    emailTimerInterval = undefined;
  }
  const missions = await request('/api/email/missions');
  if (!isCurrent()) return;

  if (!missions || !missions.length) {
    const connectors = await getConnectors();
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

  // The auto-sync countdown runs on the mission's own frequency, not a
  // hardcoded 5 minutes: a 60-minute Gmail sweep used to sync every 5
  // minutes anyway while the banner printed the real frequency.
  const syncPeriodSeconds = Number.isFinite(mission.frequency_seconds) && mission.frequency_seconds >= 60
    ? Math.floor(mission.frequency_seconds)
    : 300;
  const formatSyncPeriod = (secs: number): string =>
    secs % 3600 === 0 ? `${secs / 3600} hr${secs === 3600 ? "" : "s"}` : secs % 60 === 0 ? `${secs / 60} mins` : `${secs}s`;
  const formatCountdown = (secs: number): string => {
    const m = String(Math.floor(secs / 60)).padStart(2, "0");
    const s = String(secs % 60).padStart(2, "0");
    return `${m}:${s}`;
  };
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

  // Policy & Controls Banner with Live Timer (mission frequency)
  const controlsHtml = card('Monitoring Controls & Deletion Policy',
    '<div class="cc-row"><div>' +
    '<small>' + safe(mission.provider.toUpperCase()) + ' · ' + safe(mission.account_email) + ' · auto-sync every ' + formatSyncPeriod(syncPeriodSeconds) + ' (' + safe(syncPeriodSeconds) + 's)</small>' +
    '<h3>Status: ' + safe(mission.status.toUpperCase()) + ' ' + modeBadge + '</h3>' +
    '<div style="display:flex;align-items:center;gap:10px;margin:10px 0;padding:8px 14px;background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.25);border-radius:6px;width:fit-content">' +
    '<span style="font-size:16px">⏱</span>' +
    '<span style="color:var(--text-secondary);font-size:13px;font-weight:500">Auto-sync timer:</span>' +
    '<strong id="emailSyncCountdown" style="font-family:var(--font-mono);font-size:12px;color:var(--privacy);background:var(--privacy-soft);padding:2px 8px;border-radius:4px;border:1px solid rgba(45,90,60,0.25)">' + formatCountdown(syncPeriodSeconds) + '</strong>' +
    '</div>' +
    '<p>Protected categories (<strong>Banking, Financial, Security, Academic, Recruitment, Personal, Deadlines</strong>) are strictly locked and never automatically deleted.</p>' +
    '</div><div class="cc-row-actions">' +
    (isAuth
      ? button('Switch to Dry-Run Mode', 'email-dryrun', mission.id)
      : '<button class="cc-button" data-email-authorize="' + safe(mission.id) + '">✓ Authorize Autonomous Cleanup</button>') +
    button(mission.status === 'active' ? 'Pause' : 'Resume', 'email-toggle-status', mission.id) +
    '<button class="cc-button" id="btnManualSync" data-email-sync-now="' + safe(mission.id) + '"><span id="syncBtnIcon">↻</span> <span id="syncBtnText">Sync Now (Manual)</span></button>' +
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

  // Auto-Sync Countdown (mission frequency) and Manual Sync logic
  let remainingSeconds = syncPeriodSeconds;
  if (mission.next_run_at) {
    const diff = Math.round(Number(mission.next_run_at) - (Date.now() / 1000));
    if (diff > 0 && diff <= syncPeriodSeconds) {
      remainingSeconds = diff;
    }
  }

  const countdownEl = content.querySelector<HTMLElement>('#emailSyncCountdown');
  const syncBtn = content.querySelector<HTMLButtonElement>('#btnManualSync');
  const syncBtnText = content.querySelector<HTMLElement>('#syncBtnText');

  const updateCountdownDisplay = (secs: number) => {
    if (!countdownEl) return;
    countdownEl.textContent = formatCountdown(secs);
  };

  let isSyncing = false;
  const executeSync = async (isManual = false) => {
    if (isSyncing) return;
    isSyncing = true;
    if (countdownEl) countdownEl.textContent = 'Syncing...';
    if (syncBtn) syncBtn.disabled = true;
    if (syncBtnText) syncBtnText.textContent = 'Syncing inbox... ⏳';

    try {
      await request('/api/email/missions/' + encodeURIComponent(mission.id) + '/sync', { method: 'POST' });
      rerender();
    } catch (err) {
      showError(content, err);
      isSyncing = false;
      if (syncBtn) syncBtn.disabled = false;
      if (syncBtnText) syncBtnText.textContent = 'Sync Now (Manual)';
      remainingSeconds = syncPeriodSeconds;
      updateCountdownDisplay(remainingSeconds);
    }
  };

  updateCountdownDisplay(remainingSeconds);

  if (mission.status === 'active') {
    emailTimerInterval = window.setInterval(() => {
      if (!isCurrent()) {
        if (emailTimerInterval !== undefined) {
          window.clearInterval(emailTimerInterval);
          emailTimerInterval = undefined;
        }
        return;
      }
      if (isSyncing) return;
      if (remainingSeconds > 0) {
        remainingSeconds--;
        updateCountdownDisplay(remainingSeconds);
      } else {
        remainingSeconds = syncPeriodSeconds;
        executeSync(false);
      }
    }, 1000);
  } else {
    if (countdownEl) countdownEl.textContent = 'PAUSED';
  }

  syncBtn?.addEventListener('click', () => {
    executeSync(true);
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
    if (src && image) {
      // Re-assigning the same data URL every 4s re-decodes the image
      // and flickers the frame; only swap it when it actually changed.
      if (image.getAttribute("src") !== src) image.src = src;
      image.hidden = false; if (placeholder) placeholder.hidden = true;
    }
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
