/*
  Shared plumbing for the FPL Jakarta pages.

  The data-loading dance is the interesting part. A deployed copy of a page is
  only as fresh as the host's last build, but the data files are refreshed in
  the repo every few minutes. So each page asks GitHub for the repo copy as
  well as the deployed one and keeps whichever was generated later. If GitHub
  is unreachable the deployed copy still carries the page, and if the deploy is
  stale the repo copy does.
*/

const REPO_DATA =
  'https://raw.githubusercontent.com/fpljakarta/fpl-jakarta-dashboard/main/';

function esc(s){
  return String(s ?? '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

async function grabJson(url, timeoutMs){
  const stop = new AbortController();
  const timer = setTimeout(() => stop.abort(), timeoutMs || 8000);
  try{
    const res = await fetch(url, { cache: 'no-store', signal: stop.signal });
    if(!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

async function loadJson(name){
  const [local, remote] = await Promise.all([
    grabJson(name).catch(() => null),
    // Never let a slow or blocked GitHub hold up the page: the deployed copy
    // is good enough if the repo copy does not answer quickly.
    grabJson(REPO_DATA + name, 5000).catch(() => null),
  ]);
  if(!local) return remote;
  if(!remote) return local;
  return (remote.generated_at || '') > (local.generated_at || '') ? remote : local;
}

const CHIPS = { bboost:'BB', '3xc':'TC', freehit:'FH', wildcard:'WC', manager:'AM' };

const CHIP_LONG = {
  bboost:'Bench Boost', '3xc':'Triple Captain', freehit:'Free Hit',
  wildcard:'Wildcard', manager:'Assistant Manager',
};

function stampText(generatedAt){
  if(!generatedAt) return '';
  return 'Last updated ' + new Date(generatedAt).toLocaleString('en-GB',
    { dateStyle:'medium', timeStyle:'short', timeZone:'Asia/Jakarta' })
    + ' Jakarta time.';
}

/* Ranks run to eight figures, which is unreadable unbroken. */
function commas(n){
  return (n === null || n === undefined) ? '—' : Number(n).toLocaleString('en-GB');
}

function signed(n){
  if(!n) return '0';
  return (n > 0 ? '+' : '') + n;
}

/* A single place to decide what a two-way league switch does, since three
   pages need exactly the same one. */
function wireLeagueToggle(el, onChange){
  el.addEventListener('click', e => {
    const btn = e.target.closest('button');
    if(!btn) return;
    el.querySelectorAll('button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    onChange(btn.dataset.league);
  });
}
