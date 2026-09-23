const screenEl = document.getElementById('screen');
const chatEl = document.getElementById('chat');
const statusChip = document.getElementById('status-chip');
const paneScreen = document.getElementById('pane-screen');
const paneChat = document.getElementById('pane-chat');
const tabScreen = document.getElementById('tab-screen');
const tabChat = document.getElementById('tab-chat');
const msgBox = document.getElementById('msg');
const sendBtn = document.getElementById('send');

let activeTab = 'screen';
let chatAfter = 0;
let chatLoaded = false;

function setTab(t) {
  activeTab = t;
  paneScreen.hidden = t !== 'screen';
  paneChat.hidden = t !== 'chat';
  tabScreen.classList.toggle('active', t === 'screen');
  tabChat.classList.toggle('active', t === 'chat');
  if (t === 'chat') refreshChat();
  else refreshScreen();
}
tabScreen.onclick = () => setTab('screen');
tabChat.onclick = () => setTab('chat');

function setStatus(st) {
  if (!st) return;
  statusChip.textContent = st;
  statusChip.className = `chip ${st}`;
}

function toast(text) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = text;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2200);
}

// ---- screen ----
async function refreshScreen() {
  if (!LIVE || !screenEl) return;
  try {
    const r = await fetch(`${PREFIX}/api/screen/${SID}`);
    if (!r.ok) { screenEl.textContent = 'session went away'; return; }
    const d = await r.json();
    const nearBottom = paneScreen.scrollHeight - paneScreen.scrollTop - paneScreen.clientHeight < 60;
    screenEl.innerHTML = d.html;
    if (nearBottom) paneScreen.scrollTop = paneScreen.scrollHeight;
    setStatus(d.status);
  } catch (e) { /* transient */ }
}

// ---- chat ----
function renderItem(it) {
  if (it.kind === 'user' || it.kind === 'assistant')
    return `<div class="msg ${it.kind}">${it.html}</div>`;
  if (it.kind === 'tool')
    return `<details class="mini"><summary><span class="toolname">${it.name}</span> ${it.html}</summary></details>`;
  if (it.kind === 'result')
    return `<details class="mini${it.error ? ' err' : ''}"><summary><span class="toolname">${it.error ? '✗' : '→'} ${it.name || 'output'}</span> ${it.html.slice(0, 120)}</summary><pre>${it.html}</pre></details>`;
  if (it.kind === 'thinking')
    return `<details class="mini"><summary>💭 thinking</summary><pre>${it.html}</pre></details>`;
  if (it.kind === 'cmd')
    return `<div class="cmdline">${it.html}</div>`;
  return '';
}

async function refreshChat() {
  try {
    const r = await fetch(`${PREFIX}/api/transcript/${SID}?after=${chatAfter}`);
    if (!r.ok) { if (!chatLoaded) chatEl.innerHTML = '<div class="muted pad">no transcript found</div>'; return; }
    const d = await r.json();
    setStatus(d.status);
    if (d.unchanged) return;
    const nearBottom = paneChat.scrollHeight - paneChat.scrollTop - paneChat.clientHeight < 120;
    const htmlStr = (d.items || []).map(renderItem).join('');
    if (!chatLoaded) { chatEl.innerHTML = htmlStr; chatLoaded = true; paneChat.scrollTop = paneChat.scrollHeight; }
    else if (htmlStr) {
      chatEl.insertAdjacentHTML('beforeend', htmlStr);
      if (nearBottom) paneChat.scrollTop = paneChat.scrollHeight;
    }
    chatAfter = d.size;
  } catch (e) { /* transient */ }
}

// ---- composer ----
async function send(body) {
  try {
    const r = await fetch(`${PREFIX}/api/send/${SID}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) { toast(d.error || 'send failed'); return false; }
    return true;
  } catch (e) { toast('send failed'); return false; }
}

if (LIVE && sendBtn) {
  sendBtn.onclick = async () => {
    const text = msgBox.value.trim();
    if (!text) return;
    sendBtn.disabled = true;
    const ok = await send({ text });
    sendBtn.disabled = false;
    if (ok) {
      msgBox.value = '';
      msgBox.style.height = 'auto';
      toast('sent ✓');
      setTimeout(refreshScreen, 600);
    }
  };
  msgBox.addEventListener('input', () => {
    msgBox.style.height = 'auto';
    msgBox.style.height = Math.min(msgBox.scrollHeight, 140) + 'px';
  });
  msgBox.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendBtn.click(); }
  });
  document.querySelectorAll('.quickkeys button').forEach(b => {
    b.onclick = async () => {
      if (await send({ key: b.dataset.key })) setTimeout(refreshScreen, 400);
    };
  });
}

// ---- polling ----
setInterval(() => {
  if (document.hidden) return;
  if (activeTab === 'screen') refreshScreen(); else refreshChat();
}, 3000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) (activeTab === 'screen' ? refreshScreen() : refreshChat());
});
refreshScreen();
