"""Responsive admin dashboard for Sync-Music."""
HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Music Sync Hub</title>
<style>
:root{color-scheme:dark;--bg:#101216;--panel:#181c24;--surface:#202631;--border:#303848;--ink:#f1f4fa;--muted:#adb8cb;--accent:#a8e8cf;--dark:#142820}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(ellipse at 25% 0,#1a2631 0,transparent 640px),var(--bg);color:var(--ink);font:15px/1.55 "Geist",system-ui,sans-serif}
main,.topbar{max-width:1056px;margin:auto;padding-inline:clamp(14px,4vw,40px)}
.topbar{min-height:68px;display:flex;align-items:center;justify-content:space-between;gap:16px}
header{border-bottom:1px solid var(--border);background:#101216aa}.brand{font-size:18px;font-weight:750;letter-spacing:-.03em;display:flex;gap:10px;align-items:center}
.brand-mark{display:grid;place-items:center;width:32px;height:32px;border-radius:10px;color:var(--dark);background:var(--accent);font-size:17px}
main{padding-top:clamp(16px,5vw,46px);padding-bottom:48px}
h1,h2,h3,p{margin:0 0 12px}h1{font-size:clamp(26px,5vw,38px);letter-spacing:-.05em;line-height:1.1}
h2{font-size:19px;letter-spacing:-.02em}h3{font-size:17px}.muted,small{color:var(--muted)}
section,details.panel{background:var(--panel);border:1px solid var(--border);padding:clamp(16px,4vw,26px);border-radius:18px;margin:16px 0;min-width:0}
#direct{margin-top:0}#login{max-width:520px;margin:clamp(20px,8vw,80px) auto}
.providers-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.provider{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px;min-width:0}.provider p{overflow-wrap:anywhere}.provider-status{min-height:48px}
.provider[data-connected="true"] .provider-status{color:var(--accent)}
button,input,select{min-height:42px;border:1px solid #4a586d;border-radius:10px;padding:8px 12px;background:#252e3c;color:var(--ink);font:inherit}
button{cursor:pointer;font-weight:650}button:hover:not(:disabled){background:#364459}button:disabled,select:disabled{opacity:.52;cursor:default}
button.primary{background:var(--accent);border-color:transparent;color:var(--dark)}button.primary:hover:not(:disabled){background:#d0f5e8}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
input:not([type="checkbox"]){width:min(100%,420px)}input[type="checkbox"]{min-height:auto;accent-color:#90daba;width:18px;height:18px}
label.inline,.flex{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.flex{margin:12px 0}
label.choose{display:grid;grid-template-columns:minmax(0,1fr);gap:6px;max-width:335px;margin:16px 0}label.choose select{width:100%;min-width:0;max-width:100%}
.jobbox{padding:15px;background:var(--surface);border-radius:12px;margin-top:16px}.jobbox p:last-child,.provider p:last-child{margin-bottom:0}
progress{width:100%;height:10px;accent-color:var(--accent);vertical-align:middle}
a{color:var(--accent)}summary{cursor:pointer;font-weight:650}details[open] summary{margin-bottom:12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace}code{overflow-wrap:anywhere}
#undoHistory{list-style:none;margin:0;padding:0}#undoHistory li{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;border-top:1px solid var(--border);padding:10px 0}
#undoHistory li:last-child{padding-bottom:0}
#directGroups{display:grid;gap:10px}
.result-group{border:1px solid var(--border);border-radius:12px;background:var(--surface);padding:12px 14px}
.result-group summary{overflow-wrap:anywhere}.result-group summary>small{display:block;padding-left:14px}
.result-group ul{list-style:none;margin:0;padding:0}
.result-group li{padding:10px 0;border-top:1px solid var(--border);overflow-wrap:anywhere}
.result-group li strong,.result-group li small{display:block}
.result-group .flex{margin-bottom:0}
dialog{width:min(560px,calc(100vw - 28px));max-height:85dvh;overflow:auto;padding:20px;border:1px solid var(--border);border-radius:16px;background:var(--panel);color:var(--ink)}
dialog::backdrop{background:#080a0ed0}
#matchChoices{display:grid;gap:10px}
#matchChoices button{text-align:left;overflow-wrap:anywhere}
#runHistory{width:min(100%,420px);text-overflow:ellipsis}
.progress-steps{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 8px;padding:0;list-style:none}
.progress-steps li{padding:2px 8px;border:1px solid var(--border);border-radius:8px;color:var(--muted)}
.progress-steps [aria-current="step"]{border-color:var(--accent);color:var(--ink)}

.workspace-nav{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}
.workspace-nav a{border:1px solid var(--border);border-radius:24px;padding:8px 15px;text-decoration:none;color:var(--ink)}
.workspace-nav a:hover{border-color:var(--accent)}
.provider[data-state="error"] .provider-status{color:#ffbfaf}
.provider-status{border-left:3px solid currentColor;padding-left:10px}
#serverSummary{display:grid;grid-template-columns:max-content 1fr;gap:8px 18px}
#serverSummary dd{margin:0}#serverSummary dt{color:var(--muted)}
[hidden]{display:none!important}
@media(max-width:760px){.providers-grid{grid-template-columns:1fr}.provider-status{min-height:initial}}
</style></head>
<body>
<header><div class="topbar"><span class="brand"><span class="brand-mark" aria-hidden="true">♫</span>Sync-Music</span><button id="logout" hidden>Đăng xuất</button></div></header>
<main>
<section id="login">
  <h1>Đăng nhập</h1>
  <form id="loginForm">
    <label for="token">Khóa quản trị</label>
    <div class="flex"><input id="token" type="password" autocomplete="off" placeholder="APP_API_KEY" required><button id="connect" class="primary">Đăng nhập</button></div>
  </form>
  <p id="loginMessage" class="muted" role="status"></p>
</section>
<div id="workspace" hidden>
<nav class="workspace-nav" aria-label="Đi tới"><a href="#direct">Đồng bộ</a><a href="#providers">Kết nối dịch vụ</a><a href="#importPanel">Lịch sử nhập</a></nav>
<p id="overviewError" role="status" hidden></p>
<section id="direct">
  <h1>Đồng bộ thư viện</h1>
  <p class="muted">Chuyển bài đã thích vào Liked Music hoặc Liked Songs. Bài trong playlist riêng được thêm vào playlist tương ứng.</p>
  <label class="choose" for="direction">Hướng đồng bộ
    <select id="direction">
      <option value="ytmusic:spotify">YouTube Music → Spotify</option>
      <option value="spotify:ytmusic">Spotify → YouTube Music</option>
      <option value="both:both">Spotify ↔ YouTube Music</option>
      <option value="apple:spotify">Apple Music → Spotify</option>
      <option value="apple:ytmusic">Apple Music → YouTube Music</option>
      <option value="all:all">Spotify ↔ YouTube Music + nguồn Apple Music</option>
    </select>
  </label>
  <label class="inline"><input id="schedule" type="checkbox">Tự kiểm tra theo mức thay đổi của thư viện</label>
  <div class="flex"><button id="runDirect" class="primary" disabled>Đồng bộ ngay</button><button id="saveSchedule" disabled>Lưu lịch</button></div>
  <p id="directHelp" class="muted"></p>
  <p id="directMessage" role="status"></p>
  <div class="jobbox" id="directJob" hidden>
    <p id="directPhase" aria-live="polite"></p>
    <ol id="progressSteps" class="progress-steps" aria-label="Các bước đồng bộ">
      <li data-step="read">Đọc</li><li data-step="match">Ghép</li>
      <li data-step="write">Ghi và xác minh</li>
    </ol>
    <progress id="directProgress" aria-label="Tiến độ đồng bộ"></progress>
    <p id="directResult" class="muted"></p>
  </div>
</section>
<section id="historyPanel" hidden>
  <h2>Hoàn tác đồng bộ</h2>
  <p class="muted">Chọn lượt mới nhất trên từng dịch vụ. Hoàn tác sẽ tạm dừng lịch đồng bộ.</p>
  <ul id="undoHistory"></ul>
</section>
<details id="directItemsPanel" class="panel">
  <summary>Kết quả theo playlist</summary>
  <label class="choose" for="runHistory">Lượt đồng bộ
    <select id="runHistory"><option value="">Kết quả mới nhất</option></select>
  </label>
  <div class="flex">
    <label for="itemStatus">Trạng thái</label>
    <select id="itemStatus">
      <option value="">Tất cả</option>
      <option value="failed">Gặp lỗi</option>
      <option value="pending">Chờ ghi</option>
      <option value="deferred">Chờ lượt sau</option>
      <option value="unmatched">Chưa ghép</option>
      <option value="synced">Đã có ở đích</option>
    </select>
    <button id="reloadItems" type="button">Làm mới</button>
  </div>
  <p id="itemSummary" class="muted" role="status"></p>
  <div id="directGroups"></div>
  <div id="groupPagination" class="flex" hidden>
    <button id="previousGroups" type="button" disabled>Nhóm trước</button>
    <button id="nextGroups" type="button" disabled>Nhóm sau</button>
  </div>
</details>
<dialog id="matchDialog" aria-labelledby="matchHeading">
  <form method="dialog"><button>Đóng</button></form>
  <h2 id="matchHeading">Chọn bản ghi</h2>
  <p id="matchMessage" role="status"></p>
  <div id="matchChoices"></div>
</dialog>
<section id="providers"><h2>Dịch vụ âm nhạc</h2>
<div class="providers-grid">
<article class="provider" id="spotifyCard">
<h3>Spotify</h3><p class="provider-status" id="spotifyStatus"></p>
<p id="spotifyRedirect" hidden>Redirect URI trong Spotify Developer Dashboard <code id="spotifyUri"></code></p>
<button id="spotifyAuthorize" disabled>Kết nối Spotify</button>
<a id="spotifyLink" href="#" target="_blank" rel="noopener noreferrer" hidden>Mở Spotify để cấp quyền</a>
<p id="spotifyMessage" role="status"></p>
</article><article class="provider" id="ytmusicCard"><h3>YouTube Music</h3><p class="provider-status" id="ytmusicStatus"></p>
<button id="ytmusicAuthorize" disabled>Kết nối YouTube Music</button>
<a id="ytmusicLink" target="_blank" rel="noopener noreferrer" hidden>Mở Google để nhập mã</a>
<p id="ytmusicMessage" role="status"></p>
<p id="ytmusicHelp" class="muted">Đặt tệp ytmusicapi tại <code>private/ytmusic_browser.json</code> trên máy chủ.</p>
</article><article class="provider" id="appleCard"><h3>Apple Music</h3><p class="provider-status" id="appleStatus"></p><p class="muted" id="appleCapabilities"></p>
<button id="appleAuthorize" disabled>Kết nối Apple Music</button>
<a id="appleLink" target="_blank" rel="noopener noreferrer" hidden>Mở MusicKit để cấp quyền</a>
<p id="appleMessage" role="status"></p>
<p id="appleHelp" class="muted">Đặt khóa MusicKit và Music User Token trong <code>private/</code>, rồi cấu hình <code>APPLE_TEAM_ID</code> và <code>APPLE_KEY_ID</code>.</p>
</article></div>
</section>
<details class="panel" id="requestPanel">
  <summary>Lượt gọi API trong 7 ngày</summary>
  <ul id="providerRequests"></ul>
</details>
<details id="importPanel" class="panel"><summary>Nhập dữ liệu về máy chủ</summary>
<div class="flex"><button data-provider="all">Tất cả</button><button data-provider="spotify">Spotify</button><button data-provider="ytmusic">YouTube Music</button><button data-provider="apple">Apple Music</button></div>
<p id="message" role="status"></p><div id="imports"></div></details>
<details id="review" class="panel">
<summary>Duyệt ghép Spotify và YouTube Music</summary>
<p class="muted">Dùng ID trong danh sách sự kiện hoặc ID đã nhập vào danh mục của từng dịch vụ.</p>
<label for="sourceProvider">Nguồn</label>
<select id="sourceProvider"><option value="spotify">Spotify</option><option value="ytmusic">YouTube Music</option></select>
<label for="entityKind">Loại</label>
<select id="entityKind"><option value="track">Bài hát</option><option value="artist">Nghệ sĩ</option></select>
<div class="flex"><input id="sourceId" aria-label="ID nguồn" placeholder="ID nguồn"><button id="findMatch">Tìm bản ghép</button></div>
<pre id="matchResults"></pre>
<div class="flex"><input id="targetId" aria-label="ID đích" placeholder="ID đích"><button id="approveMatch">Duyệt bản ghép</button></div>
<div class="flex"><button id="audit">Xem sự kiện và thao tác</button><button id="baseline">Duyệt bản nhập ban đầu</button></div>
<p id="reviewMessage" role="status"></p><pre id="auditResults"></pre>
</details>
<details id="transfer" class="panel">
<summary>Xuất JSON và nhập playlist thủ công</summary>
<p><label>Nguồn <select id="transferSource"><option value="ytmusic">YouTube Music Liked Music</option><option value="spotify">Spotify Liked Songs</option><option value="apple">Thư viện Apple Music</option></select></label></p>
<div class="flex"><button id="transferExport">Tải JSON</button><button id="transferLoad">Lấy dữ liệu nguồn</button></div>
<p><label>Mở tệp đã xuất <input id="transferFile" type="file" accept=".json,application/json"></label></p>
<p><label>Đích <select id="transferTarget"><option value="spotify">Spotify</option><option value="ytmusic">YouTube Music</option></select></label></p>
<button id="transferPreview">Xem trước bản ghép</button>
<ol id="transferTracks"></ol>
<p><label>Tên playlist riêng <input id="transferTitle" maxlength="100"></label></p>
<button id="transferExecute" disabled>Tạo playlist và nhập bài đã ghép</button>
<p id="transferStatus" role="status"></p>
</details>
<details class="panel"><summary>Chi tiết máy chủ</summary><dl id="serverSummary"><dt>Chế độ</dt><dd id="syncMode"></dd><dt>Thư viện gốc</dt><dd id="baselineState"></dd></dl></details>
</div>
</main>
<script>
let connected=false, refreshing=false, ruleDirty=false, ruleApplied='', undoRendered='', directBusy=false;
let googleGrant, googleTimer;
const $=x=>document.getElementById(x);
document.querySelector('a[href="#importPanel"]').addEventListener('click',()=>{
  $('importPanel').open=true;
});
async function request(path,method='GET',body,headers={}) {
  const options={method,headers,cache:'no-store'};
  if (body!==undefined) {
    options.headers['Content-Type']='application/json';
    options.body=JSON.stringify(body);
  }
  const r=await fetch(path,options);
  let payload;try{payload=await r.json()}catch{payload={detail:'Phản hồi không hợp lệ'}}
  if (!r.ok) {
    if (r.status===401 && connected) showSession(false,'Phiên đăng nhập đã hết hạn.');
    throw new Error(typeof payload.detail==='string'?payload.detail:'HTTP '+r.status);
  }
  return payload;
}
async function update(){
  if (!connected || refreshing) return;
  refreshing=true;
  try {
  const {accounts,jobs,config,direct}=await request('/v1/overview');
  latestDirect=[direct,config];
  if (!connected) return;
  renderDirect(direct,config);
  renderImports(jobs);
  $('baseline').disabled=config.baseline_approved || !accounts.every(account=>account.last_success_at && account.status==='connected');
  $('spotifyAuthorize').disabled=!config.spotify_oauth_configured;
  $('ytmusicAuthorize').disabled=!config.ytmusic_oauth_configured || Boolean(googleGrant);
  $('appleAuthorize').disabled=!config.apple_oauth_configured;
  $('ytmusicHelp').hidden=config.ytmusic_oauth_configured;
  $('appleHelp').hidden=config.apple_oauth_configured;
  $('appleCapabilities').textContent=config.apple_favorites_configured
    ? 'Nguồn đồng bộ playlist và playlist Favorites đã cấu hình.'
    : 'Nguồn đồng bộ playlist. Chọn ID playlist Favorites trên máy chủ để chuyển bài yêu thích.';
  $('spotifyRedirect').hidden=!config.spotify_redirect_uri;
  $('spotifyUri').textContent=config.spotify_redirect_uri;
  for (const provider of ['spotify','ytmusic','apple']) {
    const account=accounts.find(a=>a.provider===provider);
    let status=provider==='apple'?'Chưa kết nối · Tùy chọn':'Thiếu cấu hình trên máy chủ';
    if (config.provider_ready[provider]) {
      status='Đã cấu hình, chờ nhập lần đầu';
      if (account?.last_error) status='Lần nhập gần nhất gặp lỗi · '+account.last_error;
      else if (account?.last_success_at) status='Đã nhập thành công lúc '+account.last_success_at+' UTC';
    } else if (config[provider+'_oauth_configured']) {
      status='Chờ cấp quyền';
    }
    if (provider==='spotify' && config.provider_ready.spotify) {
      const retryAt=jobs.spotify?.retry_at || config.spotify_retry_at;
      status='Đã cấp quyền Spotify · '+(retryAt
        ? 'Spotify giới hạn yêu cầu. Tự thử lại từ '+retryTime(retryAt)
        : status==='Đã cấu hình, chờ nhập lần đầu' ? 'Chờ nhập thư viện' : status);
    }
    if(provider==='ytmusic' && config.ytmusic_retry_at && config.provider_ready.ytmusic)
      status='Đã kết nối · Chờ YouTube Music tới '+retryTime(config.ytmusic_retry_at);
    $(provider+'Status').textContent=status;
    $(provider+'Card').dataset.state=config.provider_ready[provider] && account?.last_error?'error':'ready';
    $(provider+'Card').dataset.connected=String(provider==='spotify' ? config.provider_ready.spotify : account?.status==='connected');
  }
  $('syncMode').textContent=config.write_enabled?'Duyệt trước khi thêm':direct.enabled?'Chuyển trực tiếp':'Chỉ đọc';
  $('baselineState').textContent=config.baseline_approved?'Đã duyệt':'Chưa duyệt';
  $('overviewError').hidden=true;
  const busy=Object.values(jobs).some(x=>x.state==='queued'||x.state==='running');
  refreshDelay=busy || ['queued','running'].includes(direct.job?.state) ? 3500 : 20000;
  document.querySelectorAll('button[data-provider]').forEach(b=>{
    const available=Object.keys(config.provider_ready).some(provider=>
      (b.dataset.provider==='all' || b.dataset.provider===provider) && config.provider_ready[provider]
      && !['running','queued'].includes(jobs[provider]?.state));
    b.disabled=!available;
  });
  } catch(e) { $('overviewError').textContent=e.message; $('overviewError').hidden=false; }
    finally { refreshing=false; }
}
function showSession(ready,message=''){
  connected=ready;
  $('login').hidden=ready;
  $('workspace').hidden=!ready;
  $('logout').hidden=!ready;
  $('loginMessage').textContent=message;
  if (!ready) {
    clearTimeout(googleTimer);
    googleGrant=undefined;
    for (const provider of ['spotify','ytmusic','apple']) {
      $(provider+'Link').hidden=true;
      $(provider+'Message').textContent='';
    }
  }
}
$('loginForm').addEventListener('submit',async event=>{
  event.preventDefault();
  $('connect').disabled=true;
  try {
    await request('/v1/session','POST',undefined,{'X-API-Key':$('token').value.trim()});
    $('token').value='';
    showSession(true);
    await update();
  } catch(e) { showSession(false,e.message); }
  finally { $('connect').disabled=false; }
});
$('logout').onclick=async()=>{
  try { await request('/v1/session','DELETE'); showSession(false); }
  catch(e) { $('directMessage').textContent=e.message; }
};
const names={spotify:'Spotify',ytmusic:'YouTube Music',apple:'Apple Music'};
const syncLabel=(source,target)=>source==='both' ? 'Spotify ↔ YouTube Music'
  : source==='all' ? 'Spotify ↔ YouTube Music + nguồn Apple Music' : names[source]+' → '+names[target];
const stages={waiting:'Đang chờ',check_permissions:'Đang kiểm tra quyền ghi',read_source:'Đang đọc thư viện nguồn',read_destination:'Đang đọc thư viện đích',
  match_tracks:'Đang ghép bài hát', write_collections:'Đang cập nhật lượt thích và playlist',undo:'Đang hoàn tác'};
const states={idle:'Chưa chạy',queued:'Đang chờ',running:'Đang nhập',completed:'Hoàn tất',failed:'Gặp lỗi',
  interrupted:'Bị gián đoạn',partial:'Hoàn tất một phần'};
const count=(v,n)=>v ? v+' '+n : '';
function directionReady(config,source,target){
  const providers=source==='all'?['apple','spotify','ytmusic']:source==='both'?['spotify','ytmusic']:[source,target];
  return providers.every(provider=>config.provider_ready[provider]);
}
const retryTime=seconds=>new Date(seconds*1000).toLocaleString('vi-VN',{dateStyle:'short',timeStyle:'short'});
function showProgress(el,done,total) {
  if(total){el.max=total;el.value=done;}else{el.removeAttribute('value');}
}
function renderDirect(data,config) {
  const {job,rule,history,enabled,next_check_at}=data;
  updateItemView(job);
  const ruleKey=JSON.stringify(rule);
  if ((!ruleDirty || job?.stage.startsWith('undo') || job?.result.schedule_paused) && ruleKey!==ruleApplied) {
    if(rule) $('direction').value=rule.source+':'+rule.target;
    $('schedule').checked=Boolean(rule?.scheduled);
    ruleDirty=false;
    ruleApplied=ruleKey;
  }
  for(const option of $('direction').options){
    const [source,target]=option.value.split(':');
    option.disabled=!directionReady(config,source,target);
  }
  const [source,target]=$('direction').value.split(':');
  const ready=directionReady(config,source,target);
  const busy=job?.state==='queued'||job?.state==='running';
  $('runDirect').disabled=!enabled || !ready || busy || directBusy;
  $('saveSchedule').disabled=!enabled || busy || directBusy || (!ready && $('schedule').checked);
  $('schedule').disabled=!enabled || busy || directBusy;
  $('directHelp').textContent=!enabled ? 'Bật AUTO_TRANSFER_ENABLED trên Botkeep, rồi cấp lại quyền Spotify.'
    : !ready ? 'Kết nối các dịch vụ trong hướng đã chọn.'
    : rule?.scheduled ? 'Lịch đang bật · '+syncLabel(rule.source,rule.target)
      +(next_check_at && !busy?' · Kiểm tra lại lúc '+retryTime(next_check_at):'') : '';
  $('directJob').hidden=!job;
  if(job){
    const waiting=job.retry_provider||'spotify';
    const phase=busy && job.retry_at ? 'Đang chờ '+names[waiting]
      +(waiting==='spotify'&&config.spotify_limit_reason==='quota'?' do hết quota':'')
      : job.stage==='no_changes' ? 'Thư viện không đổi' : busy ? stages[job.stage] : states[job.state];
    const step=job.stage==='match_tracks'?'match':job.stage==='write_collections'?'write':'read';
    $('progressSteps').hidden=!busy || job.stage==='undo';
    $('progressSteps').querySelectorAll('li').forEach(li=>{
      if(li.dataset.step===step)li.setAttribute('aria-current','step');else li.removeAttribute('aria-current');
    });
    const direction=['both','all'].includes(job.source)&&busy&&job.result.active_source
      ? syncLabel(job.result.active_source,job.result.active_target) : syncLabel(job.source,job.target);
    const text=(phase||states[job.state])+' · '+direction
      +(busy&&job.total?' · '+job.done+'/'+job.total:'');
    if ($('directPhase').textContent!==text) $('directPhase').textContent=text;
    $('directProgress').hidden=['failed','interrupted'].includes(job.state);
    showProgress($('directProgress'),job.done,job.total || (busy?0:1));
    const r=job.result;
    const pieces=job.stage.startsWith('undo')
      ? [count(r.likes_removed,'lượt thích đã gỡ'),count(r.playlist_tracks_removed,'mục playlist đã gỡ'),
         count(r.conflicts,'mục playlist cần kiểm tra'),count(r.uncertain,'mục chưa xác minh')]
      : [count(r.liked_added,'lượt thích đã thêm'),count(r.playlist_tracks_added,'mục playlist đã thêm'),
         ...(!busy ? [count(r.unmatched,'mục chưa ghép'),count(r.tracks_failed,'mục gặp lỗi'),
           count(r.search_deferred,'mục chờ tra cứu'),count(r.writes_deferred,'mục chờ ghi lượt sau'),
           count(r.playlists_need_review,'playlist cần kiểm tra')] : [])];
    const retry=busy && job.retry_at ? 'Tự tiếp tục từ '+retryTime(job.retry_at) : '';
    const error=r.error && stages[job.stage] ? `${stages[job.stage]} · ${r.error}` : r.error;
    $('directResult').textContent=[error,...pieces,
      r.apple_error?'Apple Music · '+r.apple_error:'',
      retry || (r.spotify_rate_limited?'Spotify đang giới hạn yêu cầu. Sync-Music sẽ tiếp tục ở lượt sau.':''),
      r.schedule_paused?'Đã tạm dừng lịch.':''].filter(Boolean).join(' · ');
    if(!busy && $('directMessage').textContent==='Đã xếp hàng.') $('directMessage').textContent='';
  }
  const historyKey=JSON.stringify([history,enabled,busy,directBusy]);
  if(historyKey!==undoRendered){
    undoRendered=historyKey;
    $('historyPanel').hidden=history.length===0;
    $('undoHistory').replaceChildren();
    const targets=new Set();
    for (const entry of history) {
      const row=document.createElement('li'), label=document.createElement('span'), button=document.createElement('button');
      const date=new Date(entry.created_at.replace(' ','T')+'Z');
      label.textContent=syncLabel(entry.source,entry.target)+' · '+date.toLocaleString('vi-VN');
      if (!entry.confirmed && entry.uncertain) label.textContent+=' · Chưa xác minh có thay đổi';
      button.textContent='Hoàn tác';
      button.disabled=targets.has(entry.target)||!entry.confirmed||!enabled||busy||directBusy;
      targets.add(entry.target);
      button.onclick=()=>{
        if(confirm('Hoàn tác các mục Sync-Music đã thêm trong lượt này và tạm dừng lịch?')){
          directAction('/v1/direct-sync/'+entry.job_id+'/undo','POST');
        }
      };
      row.append(label,button);
      $('undoHistory').append(row);
    }
  }
}
const itemLabels={failed:'Gặp lỗi',pending:'Chờ ghi',deferred:'Chờ lượt sau',unmatched:'Chưa ghép',synced:'Đã có ở đích'};
const trackPageSize=25, groupPageSize=20, resultGroups=new Map();
let groupOffset=0, groupRequest=0, itemVersion='', itemJob='';
const resultPanel=$('directItemsPanel');
const visibleResults=()=>connected && resultPanel.open;

function resultQuery(offset,limit){
  const query=new URLSearchParams({offset,limit});
  const status=$('itemStatus').value;
  if(status)query.set('status',status);
  if($('runHistory').value)query.set('job_id',$('runHistory').value);
  return query;
}
function pageButton(label){
  const button=document.createElement('button');
  button.type='button';
  button.textContent=label;
  button.disabled=true;
  return button;
}

function makeGroup(jobId,meta){
  const panel=document.createElement('details'), summary=document.createElement('summary');
  const title=document.createElement('span'), info=document.createElement('small');
  const items=document.createElement('ul'), message=document.createElement('small'), links=document.createElement('div');
  const previous=pageButton('Bài trước'), next=pageButton('Bài sau');
  panel.className='result-group';
  panel.setAttribute('name','direct-track-results');
  links.className='flex';
  links.hidden=true;
  message.setAttribute('role','status');
  summary.append(title,info);
  links.append(previous,next);
  panel.append(summary,items,message,links);
  const group={jobId,panel,title,info,items,message,links,previous,next,meta,offset:0,request:0};
  previous.onclick=()=>{group.offset=Math.max(0,group.offset-trackPageSize);void loadTracks(group);};
  next.onclick=()=>{group.offset+=trackPageSize;void loadTracks(group);};
  panel.addEventListener('toggle',()=>{if(panel.open)void loadTracks(group);});
  return group;
}

function updateGroup(group,meta){
  group.meta=meta;
  group.title.textContent=meta.collection===null ? 'Bài đã thích' : meta.collection||'Playlist chưa có tên';
  const count=meta.visible===meta.total ? meta.total : meta.visible+'/'+meta.total;
  group.info.textContent=syncLabel(meta.source,meta.target)+' · '+count+' mục';
}

async function loadTracks(group){
  if(!visibleResults() || !group.panel.open || !group.panel.isConnected)return;
  const version=++group.request, offset=group.offset, {source,target,key}=group.meta;
  const query=resultQuery(offset,trackPageSize);
  query.set('source',source);
  query.set('target',target);
  query.set('collection_key',key);
  const current=()=>version===group.request && visibleResults() && group.panel.open && group.panel.isConnected;
  try{
    const {items,total,job_id}=await request('/v1/direct-sync/items?'+query);
    if(!current() || job_id!==group.jobId)return;
    if(offset && offset>=total){
      group.offset=total?Math.floor((total-1)/trackPageSize)*trackPageSize:0;
      return loadTracks(group);
    }
    const rows=items.map(item=>{
      const row=document.createElement('li'), title=document.createElement('strong'), details=document.createElement('small');
      title.textContent=[item.title||'Bài chưa có tên',item.artist].filter(Boolean).join(' · ');
      details.textContent=[itemLabels[item.status],item.reason].filter(Boolean).join(' · ');
      row.append(title,details);
      if(item.status==='unmatched' && /^[A-Za-z0-9._-]{1,128}$/.test(item.source_id||'')){
        const button=document.createElement('button');
        button.textContent='Xem bản ghép';
        button.onclick=()=>void chooseMatch(item);
        row.append(button);
      }
      return row;
    });
    group.items.replaceChildren(...rows);
    group.message.textContent=total?(offset+1)+'–'+(offset+items.length)+' / '+total+' mục':'Chưa có bài ở trạng thái này.';
    group.links.hidden=total<=trackPageSize;
    group.previous.disabled=offset===0;
    group.next.disabled=offset+trackPageSize>=total;
  }catch(error){
    if(current())group.message.textContent=error.message;
  }
}

function clearGroups(){
  groupRequest++;
  resultGroups.clear();
  $('directGroups').replaceChildren();
  $('groupPagination').hidden=true;
}

async function loadGroups(){
  if(!visibleResults())return;
  const version=++groupRequest, offset=groupOffset;
  try{
    const {groups,total,job_id}=await request('/v1/direct-sync/groups?'+resultQuery(offset,groupPageSize));
    if(version!==groupRequest || !visibleResults())return;
    if(offset && offset>=total){
      groupOffset=total?Math.floor((total-1)/groupPageSize)*groupPageSize:0;
      return loadGroups();
    }
    const keys=new Set();
    for(const [index,meta] of groups.entries()){
      const key=JSON.stringify([job_id,meta.source,meta.target,meta.key]);
      keys.add(key);
      let group=resultGroups.get(key);
      if(!group){
        group=makeGroup(job_id,meta);
        resultGroups.set(key,group);
      }
      updateGroup(group,meta);
      const container=$('directGroups'), current=container.children[index];
      if(current!==group.panel)container.insertBefore(group.panel,current||null);
      if(group.panel.open)void loadTracks(group);
    }
    for(const [key,group] of resultGroups){
      if(!keys.has(key)){group.panel.remove();resultGroups.delete(key);}
    }
    $('itemSummary').textContent=total?(offset+1)+'–'+(offset+groups.length)+' / '+total+' nhóm':'Chưa có nhóm chứa bài ở trạng thái này.';
    $('groupPagination').hidden=total<=groupPageSize;
    $('previousGroups').disabled=offset===0;
    $('nextGroups').disabled=offset+groupPageSize>=total;
  }catch(error){
    if(version===groupRequest && visibleResults())$('itemSummary').textContent=error.message;
  }
}

function updateItemView(job){
  if($('runHistory').value)return;
  const id=job?.stage.startsWith('undo')||job?.stage==='no_changes'?itemJob:job?.job_id||'';
  if(id!==itemJob){itemJob=id;groupOffset=0;clearGroups();}
  const version=JSON.stringify([itemJob,job?.stage,job?.updated_at,job?.done,job?.state]);
  if(version!==itemVersion){itemVersion=version;void loadGroups();}
}
async function loadRunHistory(){
  if(!visibleResults())return;
  try{
    const runs=await request('/v1/direct-sync/history');
    const select=$('runHistory'), selected=select.value;
    const options=runs.map(run=>new Option(
      retryTime(run.finished_at)+' · '+syncLabel(run.source,run.target)+' · '+states[run.state],run.job_id));
    select.replaceChildren(new Option('Kết quả mới nhất',''),...options);
    if(runs.some(run=>run.job_id===selected))select.value=selected;
  }catch(error){$('itemSummary').textContent=error.message;}
}
resultPanel.addEventListener('toggle',()=>{void loadRunHistory();void loadGroups();});
$('runHistory').onchange=()=>{groupOffset=0;clearGroups();void loadGroups();};

async function chooseMatch(item){
  const dialog=$('matchDialog'), message=$('matchMessage'), list=$('matchChoices');
  const fields={source:item.source,target:item.target,source_id:item.source_id};
  $('matchHeading').textContent=item.title||'Chọn bản ghi';
  message.textContent='Đang tải ứng viên…';
  list.replaceChildren();
  dialog.showModal();
  try{
    const matches=await request('/v1/direct-sync/candidates?'+new URLSearchParams(fields));
    message.textContent=matches.length?'So sánh nghệ sĩ, phiên bản và thời lượng trước khi chọn.'
      :'Chưa có ứng viên. Hãy thử lại sau khi làm mới kết quả tra cứu.';
    for(const match of matches){
      const button=document.createElement('button');
      const seconds=Math.round((match.duration_ms||0)/1000);
      const duration=seconds?Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0'):'';
      button.textContent=[match.title,match.artist,duration,match.isrc].filter(Boolean).join(' · ');
      button.onclick=async()=>{
        button.disabled=true;
        try{
          await request('/v1/direct-sync/candidates','POST',{...fields,target_id:match.id});
          message.textContent='Đã lưu bản ghép. Lượt đồng bộ tiếp theo sẽ áp dụng.';
          list.replaceChildren();
        }catch(error){message.textContent=error.message;button.disabled=false;}
      };
      list.append(button);
    }
  }catch(error){message.textContent=error.message;}
}

$('requestPanel').addEventListener('toggle',async()=>{
  if(!$('requestPanel').open || !connected)return;
  const list=$('providerRequests');
  const operations={read:'Đọc',search:'Tìm kiếm',write:'Ghi',verify:'Xác minh',authorize:'Cấp quyền'};
  try{
    const metrics=await request('/v1/requests');
    const rows=metrics.map(row=>{
      const item=document.createElement('li');
      item.textContent=names[row.provider]+' · '+operations[row.operation]+' · '+row.requests+' lượt'
        +(row.status>=400?' · HTTP '+row.status:row.status===0?' · lỗi kết nối':'');
      return item;
    });
    list.replaceChildren(...rows);
  }catch(error){list.textContent=error.message;}
});
$('itemStatus').onchange=()=>{groupOffset=0;clearGroups();void loadGroups();};
$('reloadItems').onclick=loadGroups;
$('previousGroups').onclick=()=>{groupOffset=Math.max(0,groupOffset-groupPageSize);void loadGroups();};
$('nextGroups').onclick=()=>{groupOffset+=groupPageSize;void loadGroups();};
function renderImports(jobs){
  const items=[];
  for(const [provider,job] of Object.entries(jobs)){
    if(job.state==='idle')continue;
    const box=document.createElement('div'), label=document.createElement('p'), bar=document.createElement('progress');
    box.className='jobbox';
    const phase=job.retry_at ? 'Đang chờ giới hạn yêu cầu · Thử lại từ '+retryTime(job.retry_at) : states[job.state];
    label.textContent=names[provider]+' · '+phase+(job.result?.error?' · '+job.result.error:'');
    bar.setAttribute('aria-label','Tiến độ nhập '+names[provider]);
    const working=job.state==='running'||job.state==='queued';
    showProgress(bar,job.state==='completed'?1:0,working?0:1);
    box.append(label,bar);
    items.push(box);
  }
  $('imports').replaceChildren(...items);
}
async function directAction(path,method,payload){
  if(directBusy)return;
  directBusy=true;
  $('runDirect').disabled=true;
  $('saveSchedule').disabled=true;
  $('directMessage').textContent='Đang gửi yêu cầu…';
  try{
    await request(path,method,payload);
    $('directMessage').textContent=method==='PUT'?'Đã lưu lịch.':'Đã xếp hàng.';
    if(method==='PUT' || path.endsWith('/undo')){ruleDirty=false;ruleApplied='';}
  } catch(e){$('directMessage').textContent=e.message;}
  finally {directBusy=false;await update();}
}
$('direction').onchange=$('schedule').onchange=()=>{ruleDirty=true;};
function direction(){const [source,target]=$('direction').value.split(':');return {source,target};}
$('runDirect').onclick=()=>directAction('/v1/direct-sync/run','POST',direction());
$('saveSchedule').onclick=()=>directAction('/v1/direct-sync/rule','PUT',{...direction(),scheduled:$('schedule').checked});
$('spotifyAuthorize').addEventListener('click',async()=>{
  $('spotifyLink').hidden=true;
  $('spotifyMessage').textContent='';
  try {
    const result=await request('/v1/auth/spotify/start','POST');
    $('spotifyLink').href=result.authorization_url;
    $('spotifyLink').hidden=false;
    $('spotifyLink').focus();
  } catch(e) { $('spotifyMessage').textContent='Lỗi: '+e.message; }
});
$('spotifyLink').addEventListener('click',()=>{$('spotifyLink').hidden=true});
$('appleAuthorize').addEventListener('click',async()=>{
  $('appleLink').hidden=true;
  $('appleMessage').textContent='';
  try {
    const result=await request('/v1/auth/apple/start','POST');
    $('appleLink').href=result.connect_url;
    $('appleLink').hidden=false;
    $('appleLink').focus();
  } catch(e) { $('appleMessage').textContent=e.message; }
});
$('appleLink').addEventListener('click',()=>{$('appleLink').hidden=true});
$('ytmusicAuthorize').addEventListener('click',async()=>{
  $('ytmusicAuthorize').disabled=true;
  try {
    googleGrant=await request('/v1/auth/ytmusic/start','POST');
    $('ytmusicLink').href=googleGrant.verification_url;
    $('ytmusicLink').hidden=false;
    $('ytmusicMessage').textContent='Nhập mã '+googleGrant.user_code+' trên trang Google.';
    googleTimer=setTimeout(checkGoogleGrant,googleGrant.interval*1000);
  } catch(e) {
    $('ytmusicMessage').textContent=e.message;
    $('ytmusicAuthorize').disabled=false;
  }
});
async function checkGoogleGrant(){
  const grant=googleGrant;
  if (!grant || !connected) return;
  try {
    const result=await request('/v1/auth/ytmusic/poll','POST',{handle:grant.handle});
    if (googleGrant!==grant) return;
    if (result.status==='pending') {
      googleTimer=setTimeout(checkGoogleGrant,result.interval*1000);
      return;
    }
    $('ytmusicMessage').textContent=result.status==='connected'
      ? 'YouTube Music đã kết nối. Máy chủ đã xếp hàng nhập dữ liệu.'
      : 'Cấp quyền chưa hoàn tất. Hãy thử lại.';
  } catch(e) {
    if (googleGrant!==grant) return;
    $('ytmusicMessage').textContent=e.message;
  }
  googleGrant=undefined;
  $('ytmusicLink').hidden=true;
  await update();
}
$('findMatch').addEventListener('click',async()=>{
  try {
    const query=new URLSearchParams({provider:$('sourceProvider').value,kind:$('entityKind').value,remote_id:$('sourceId').value.trim()});
    $('matchResults').textContent=JSON.stringify(await request('/v1/matches/candidates?'+query),null,2);
  } catch(e) { $('matchResults').textContent=e.message; }
});
$('approveMatch').addEventListener('click',async()=>{
  try {
    await request('/v1/matches/approve','POST',{source_provider:$('sourceProvider').value,kind:$('entityKind').value,
      source_id:$('sourceId').value.trim(),target_id:$('targetId').value.trim()});
    $('reviewMessage').textContent='Đã duyệt bản ghép cho các lượt phát hiện tiếp theo.';
  } catch(e) { $('reviewMessage').textContent=e.message; }
});
$('audit').addEventListener('click',async()=>{
  try {
    const [events,operations]=await Promise.all([request('/v1/events'),request('/v1/operations')]);
    $('auditResults').textContent=JSON.stringify({events,operations},null,2);
  } catch(e) { $('auditResults').textContent=e.message; }
});
$('baseline').addEventListener('click',async()=>{
  try {
    await request('/v1/baseline/approve','POST');
    $('reviewMessage').textContent='Đã duyệt bản nhập ban đầu.';
    await update();
  } catch(e) { $('reviewMessage').textContent=e.message; }
});
let transferLibrary, transferPlan;
const transferStatus = message => { $('transferStatus').textContent=message; };
function resetTransferPreview() {
  transferPlan=undefined;
  $('transferTracks').replaceChildren();
  $('transferExecute').disabled=true;
}
const loadTransferSource=()=>request('/v1/transfers/export/'+$('transferSource').value);
$('transferSource').onchange=()=>{transferLibrary=undefined;resetTransferPreview();};
$('transferTarget').onchange=resetTransferPreview;
$('transferLoad').onclick=async()=>{
  resetTransferPreview();
  transferLibrary=undefined;
  const source=$('transferSource').value;
  try {
    const library=await loadTransferSource();
    if (source!==$('transferSource').value) return;
    transferLibrary=library;
    transferStatus('Đã đọc '+library.tracks.length+' bài.');
  } catch(e) { transferStatus(e.message); }
};
$('transferExport').onclick=async()=>{
  try {
    const data=await loadTransferSource();
    const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
    const link=document.createElement('a');
    link.href=url;
    link.download=data.provider+'-library.json';
    link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  } catch(e) { transferStatus(e.message); }
};
$('transferFile').onchange=async()=>{
  resetTransferPreview();
  transferLibrary=undefined;
  const file=$('transferFile').files[0];
  if (!file) return;
  try {
    if (file.size>3000000) throw new Error('Tệp vượt quá 3 MB.');
    const data=JSON.parse(await file.text());
    if (file!==$('transferFile').files[0]) return;
    if (data.version!==1 || !Array.isArray(data.tracks)) throw new Error('Định dạng tệp xuất không hợp lệ.');
    transferLibrary=data;
    transferStatus('Đã mở tệp có '+data.tracks.length+' bài.');
  } catch(e) { transferStatus(e.message); }
};
$('transferPreview').onclick=async()=>{
  resetTransferPreview();
  const library=transferLibrary, target=$('transferTarget').value;
  try {
    if (!library) throw new Error('Hãy lấy dữ liệu nguồn hoặc mở tệp xuất trước.');
    const plan=await request('/v1/transfers/preview','POST',{library,target});
    if (library!==transferLibrary || target!==$('transferTarget').value) return;
    transferPlan=plan;
    for (const track of plan.tracks) {
      const item=document.createElement('li');
      item.textContent=[track.title,track.artist,track.target_id ? 'đã ghép ('+track.match+')' : 'chưa ghép'].filter(Boolean).join(' · ');
      $('transferTracks').append(item);
    }
    transferStatus('Đã ghép '+plan.matched+'/'+plan.total+' bài.');
    $('transferExecute').disabled=!plan.matched;
  } catch(e) { transferStatus(e.message); }
};
$('transferExecute').onclick=async()=>{
  const title=$('transferTitle').value.trim();
  if (!title || !transferPlan) {
    transferStatus('Nhập tên playlist sau khi xem trước.');
    return;
  }
  const payload={library:transferLibrary,target:$('transferTarget').value,title,plan:transferPlan.plan};
  $('transferExecute').disabled=true;
  try {
    const result=await request('/v1/transfers/execute','POST',payload);
    transferStatus('Kết quả '+result.status+' · '+result.added+' bài'
      +(result.playlist_id ? ' · ID playlist '+result.playlist_id : ''));
  } catch(e) {
    transferStatus(e.message);
    if (transferPlan?.plan===payload.plan) $('transferExecute').disabled=false;
  }
};
document.querySelectorAll('button[data-provider]').forEach(b=>b.addEventListener('click',async()=>{if(!connected)return;$('message').textContent='Đang xếp hàng: '+b.dataset.provider;try{await request('/v1/sync/'+b.dataset.provider,'POST');$('message').textContent='Yêu cầu đã vào hàng đợi.';await update()}catch(e){$('message').textContent='Lỗi: '+e.message}}));
async function restoreSession(){
  try {await request('/v1/config');showSession(true);await update();}
  catch {showSession(false);}
}
void restoreSession();
let latestDirect;
$('direction').addEventListener('change',()=>{if(latestDirect)renderDirect(...latestDirect);});
let refreshDelay=3500, refreshTimer;
async function refreshVisible(){
  clearTimeout(refreshTimer);
  if(document.hidden)return;
  await update();
  if(!document.hidden)refreshTimer=setTimeout(refreshVisible,refreshDelay);
}
document.addEventListener('visibilitychange',()=>void refreshVisible());
void refreshVisible();
</script></body></html>'''
