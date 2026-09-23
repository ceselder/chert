const cards = document.getElementById('cards');
const deadWrap = document.getElementById('dead-wrap');
const deadDiv = document.getElementById('dead');
const summary = document.getElementById('summary');
const diskEl = document.getElementById('disk');

// Outer Wilds readings of Claude Code's registry statuses
const WORD = { busy: '🔭 exploring', shell: '🚀 in flight', idle: '🔥 at the campfire',
               waiting: '📡 needs you', ended: '🌌 loop ended' };

function ago(tsMs, now) {
  const s = Math.max(0, now - tsMs / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${(s / 86400).toFixed(1)}d ago`;
}

function esc(t) {
  const d = document.createElement('div');
  d.textContent = t;
  return d.innerHTML;
}

async function refresh() {
  let data;
  try {
    const r = await fetch(`${PREFIX}/api/sessions`);
    data = await r.json();
  } catch (e) {
    summary.textContent = 'signal lost — refresh failed';
    return;
  }
  const ss = data.sessions;
  const nBusy = ss.filter(s => s.status === 'busy' || s.status === 'shell').length;
  const nWait = ss.filter(s => s.status === 'waiting').length;
  summary.textContent = `${ss.length} traveler${ss.length === 1 ? '' : 's'} · ${nBusy} exploring` +
    (nWait ? ` · ${nWait} need you` : '');
  if (data.disk && diskEl) {
    const d = data.disk;
    diskEl.textContent = `💾 ${d.free_gb} GB free`;
    diskEl.className = 'disk ' + (d.free_gb < 5 ? 'crit' : d.free_gb < 15 ? 'low' : 'ok');
  }
  cards.innerHTML = ss.map(s => `
    <a class="card ${esc(s.status)}" href="${PREFIX}/s/${s.sid}">
      <div class="row1">
        <span class="name">${esc(s.name)}</span>
        <span class="chip ${esc(s.status)}">${esc(WORD[s.status] || s.status)}</span>
      </div>
      <div class="proj">${esc(s.project)} · <span class="ago">${ago(s.updatedAt, data.now)}</span>${s.pane ? '' : ' · 👁️ read-only'}</div>
      ${s.snippet ? `<div class="snippet">${esc(s.snippet)}</div>` : ''}
    </a>`).join('') || '<div class="muted pad">🔭 no signals — no live claudes right now</div>';
  if (data.dead && data.dead.length) {
    deadWrap.hidden = false;
    deadDiv.innerHTML = data.dead.map(d => `
      <a href="${PREFIX}/s/${d.sid}">
        <span class="sid">${esc(d.sid.slice(0, 8))}</span>
        <span>${esc(d.slug.replace(/^-home-[^-]+-/, ''))}</span>
        <span class="sid" style="margin-left:auto">${ago(d.mtime * 1000, data.now)}</span>
      </a>`).join('');
  }
}

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 6000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
