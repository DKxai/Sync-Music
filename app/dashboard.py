"""Dashboard whose admin token stays in browser memory."""
HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Music Sync Hub</title>
<style>:root{color-scheme:dark}body{font:15px system-ui,sans-serif;max-width:840px;margin:32px auto;padding:0 18px;background:#10151d;color:#ebeff7}h1{font-size:26px}.muted{color:#9ca9ba}section{background:#1a2330;padding:20px;border-radius:14px;margin:16px 0}button,input,select{border:1px solid #43516a;border-radius:8px;padding:11px;background:#25364b;color:white;font:inherit}button{cursor:pointer}button:disabled{opacity:.45;cursor:wait}.primary{background:#275a8d}input{width:min(95%,480px)}.flex{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.6}small{color:#a3aec1}</style></head>
<body><h1>🎵 Music Sync Hub</h1><p class="muted">Máy chủ tự nhập dữ liệu theo lịch, kể cả khi bạn đóng dashboard.</p>
<section><label for="token">Admin API key (chỉ lưu trong bộ nhớ của tab)</label><div class="flex"><input id="token" type="password" autocomplete="off" placeholder="APP_API_KEY"><button id="connect">Kết nối</button></div></section>
<section id="providers" hidden><strong>Kết nối dịch vụ âm nhạc</strong>
<p><strong>Spotify</strong> · <span id="spotifyStatus"></span></p>
<p id="spotifyRedirect" hidden>Redirect URI trong Spotify Developer Dashboard <code id="spotifyUri"></code></p>
<button id="spotifyAuthorize" disabled>Kết nối Spotify</button>
<a id="spotifyLink" href="#" target="_blank" rel="noopener noreferrer" hidden>Mở Spotify để cấp quyền</a>
<p id="spotifyMessage" role="status"></p>
<p><strong>YouTube Music</strong> · <span id="ytmusicStatus"></span></p>
<button id="ytmusicAuthorize" disabled>Kết nối YouTube Music</button>
<a id="ytmusicLink" target="_blank" rel="noopener noreferrer" hidden>Mở Google để nhập mã</a>
<p id="ytmusicMessage" role="status"></p>
<p id="ytmusicHelp" class="muted">Đặt tệp ytmusicapi tại <code>private/ytmusic_browser.json</code> trên máy chủ.</p>
<p><strong>Apple Music</strong> · <span id="appleStatus"></span></p>
<button id="appleAuthorize" disabled>Kết nối Apple Music</button>
<a id="appleLink" target="_blank" rel="noopener noreferrer" hidden>Mở MusicKit để cấp quyền</a>
<p id="appleMessage" role="status"></p>
<p id="appleHelp" class="muted">Đặt khóa MusicKit và Music User Token trong <code>private/</code>, rồi cấu hình <code>APPLE_TEAM_ID</code> và <code>APPLE_KEY_ID</code>.</p>
</section>
<section><strong>Sync Now</strong><p class="muted">Các tác vụ dùng chung hàng đợi với lịch tự động. Không chạy đồng thời để hạn chế RAM.</p><div class="flex"><button data-provider="all" class="primary">Sync tất cả</button><button data-provider="spotify">Spotify</button><button data-provider="ytmusic">YouTube Music</button><button data-provider="apple">Apple Music</button></div><p id="message" role="status">Nhập key và chọn Kết nối.</p></section>
<section id="review" hidden>
<strong>Duyệt ghép Spotify và YouTube Music</strong>
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
</section>
<section id="transfer" hidden>
<strong>Nhập và xuất thư viện</strong>
<p><label>Nguồn <select id="transferSource"><option value="ytmusic">YouTube Music Liked Music</option><option value="spotify">Spotify Liked Songs</option><option value="apple">Thư viện Apple Music</option></select></label></p>
<div class="flex"><button id="transferExport">Tải JSON</button><button id="transferLoad">Lấy dữ liệu nguồn</button></div>
<p><label>Mở tệp đã xuất <input id="transferFile" type="file" accept=".json,application/json"></label></p>
<p><label>Đích <select id="transferTarget"><option value="spotify">Spotify</option><option value="ytmusic">YouTube Music</option></select></label></p>
<button id="transferPreview">Xem trước bản ghép</button>
<ol id="transferTracks"></ol>
<p><label>Tên playlist riêng <input id="transferTitle" maxlength="100"></label></p>
<button id="transferExecute" disabled>Tạo playlist và nhập bài đã ghép</button>
<p id="transferStatus" role="status"></p>
</section>
<section><strong>Trạng thái</strong><pre id="status">Chưa kết nối</pre></section>
<script>
let apiKey='';let connected=false;let refreshHandle;let googleGrant;let googleTimer;
const $=x=>document.getElementById(x);
async function request(path,method='GET',body) {
  const options={method,headers:{'X-API-Key':apiKey},cache:'no-store'};
  if (body!==undefined) {
    options.headers['Content-Type']='application/json';
    options.body=JSON.stringify(body);
  }
  const r=await fetch(path,options);
  let payload;try{payload=await r.json()}catch{payload={detail:'Phản hồi không hợp lệ'}}
  if(!r.ok)throw new Error((payload.detail&&JSON.stringify(payload.detail))||('HTTP '+r.status));return payload;
}
async function update(){if(!connected)return;try{
  const [accounts,jobs,config]=await Promise.all([request('/v1/accounts'),request('/v1/sync/status'),request('/v1/config')]);
  $('providers').hidden=false;
  $('review').hidden=false;
  $('transfer').hidden=false;
  $('baseline').disabled=config.baseline_approved || !accounts.every(account=>account.last_success_at && account.status==='connected');
  $('spotifyAuthorize').disabled=!config.spotify_oauth_configured;
  $('ytmusicAuthorize').disabled=!config.ytmusic_oauth_configured || Boolean(googleGrant);
  $('appleAuthorize').disabled=!config.apple_oauth_configured;
  $('ytmusicHelp').hidden=config.ytmusic_oauth_configured;
  $('appleHelp').hidden=config.apple_oauth_configured;
  $('spotifyRedirect').hidden=!config.spotify_redirect_uri;
  $('spotifyUri').textContent=config.spotify_redirect_uri;
  for (const provider of ['spotify','ytmusic','apple']) {
    const account=accounts.find(a=>a.provider===provider);
    let status='Thiếu cấu hình trên máy chủ';
    if (config.provider_ready[provider]) {
      status='Đã cấu hình, chờ nhập lần đầu';
      if (account?.last_error) status='Lần nhập gần nhất báo lỗi. Xem trạng thái bên dưới.';
      else if (account?.last_success_at) status='Đã nhập thành công lúc '+account.last_success_at+' UTC';
    } else if (config[provider+'_oauth_configured']) {
      status='Chờ cấp quyền';
    }
    $(provider+'Status').textContent=status;
  }
  $('status').textContent=JSON.stringify({mode:config.write_enabled?'REVIEWED ADDITIONS':'READ-ONLY',baseline_approved:config.baseline_approved,poll_minutes:config.poll_minutes,accounts,jobs},null,2);
  let busy=Object.values(jobs).some(x=>x.state==='queued'||x.state==='running');
  document.querySelectorAll('button[data-provider]').forEach(b=>b.disabled=busy);
}catch(e){$('status').textContent=e.message}}
$('connect').addEventListener('click',async()=>{clearTimeout(googleTimer);googleGrant=undefined;$('ytmusicLink').hidden=true;$('ytmusicMessage').textContent='';$('appleLink').hidden=true;apiKey=$('token').value.trim();$('token').value='';try{await request('/v1/config');connected=true;$('message').textContent='Đã kết nối. Bạn có thể chạy Sync Now.';await update();clearInterval(refreshHandle);refreshHandle=setInterval(()=>{if(!document.hidden)update()},5000)}catch(e){connected=false;apiKey='';$('providers').hidden=true;$('review').hidden=true;$('transfer').hidden=true;$('spotifyLink').hidden=true;$('message').textContent='Không thể kết nối: '+e.message}});
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
document.querySelectorAll('button[data-provider]').forEach(b=>b.addEventListener('click',async()=>{if(!connected)return;$('message').textContent='Đang xếp hàng: '+b.dataset.provider;try{let p=await request('/v1/sync/'+b.dataset.provider,'POST');$('message').textContent='Đã nhận tác vụ: '+JSON.stringify(p);await update()}catch(e){$('message').textContent='Lỗi: '+e.message}}));
</script></body></html>'''
