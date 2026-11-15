"""Dashboard whose admin token stays in browser memory."""
HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Music Sync Hub</title>
<style>:root{color-scheme:dark}body{font:15px system-ui,sans-serif;max-width:840px;margin:32px auto;padding:0 18px;background:#10151d;color:#ebeff7}h1{font-size:26px}.muted{color:#9ca9ba}section{background:#1a2330;padding:20px;border-radius:14px;margin:16px 0}button,input{border:1px solid #43516a;border-radius:8px;padding:11px;background:#25364b;color:white;font:inherit}button{cursor:pointer}button:disabled{opacity:.45;cursor:wait}.primary{background:#275a8d}input{width:min(95%,480px)}.flex{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.6}small{color:#a3aec1}</style></head>
<body><h1>🎵 Music Sync Hub</h1><p class="muted">Máy chủ tự nhập dữ liệu theo lịch, kể cả khi bạn đóng dashboard.</p>
<section><label for="token">Admin API key (chỉ lưu trong bộ nhớ của tab)</label><div class="flex"><input id="token" type="password" autocomplete="off" placeholder="APP_API_KEY"><button id="connect">Kết nối</button></div></section>
<section id="providers" hidden><strong>Kết nối dịch vụ âm nhạc</strong>
<p><strong>Spotify</strong> · <span id="spotifyStatus"></span></p>
<p id="spotifyRedirect" hidden>Redirect URI trong Spotify Developer Dashboard <code id="spotifyUri"></code></p>
<button id="spotifyAuthorize" disabled>Kết nối Spotify</button>
<a id="spotifyLink" href="#" target="_blank" rel="noopener noreferrer" hidden>Mở Spotify để cấp quyền</a>
<p id="spotifyMessage" role="status"></p>
<p><strong>YouTube Music</strong> · <span id="ytmusicStatus"></span></p>
<p class="muted">Đặt tệp ytmusicapi tại <code>private/ytmusic_browser.json</code> trên máy chủ.</p>
<p><strong>Apple Music</strong> · <span id="appleStatus"></span></p>
<p class="muted">Đặt khóa MusicKit và Music User Token trong <code>private/</code>, rồi cấu hình <code>APPLE_TEAM_ID</code> và <code>APPLE_KEY_ID</code>.</p>
</section>
<section><strong>Sync Now</strong><p class="muted">Các tác vụ dùng chung hàng đợi với lịch tự động. Không chạy đồng thời để hạn chế RAM.</p><div class="flex"><button data-provider="all" class="primary">Sync tất cả</button><button data-provider="spotify">Spotify</button><button data-provider="ytmusic">YouTube Music</button><button data-provider="apple">Apple Music</button></div><p id="message" role="status">Nhập key và chọn Kết nối.</p></section>
<section><strong>Trạng thái</strong><pre id="status">Chưa kết nối</pre></section>
<script>
let apiKey='';let connected=false;let refreshHandle;
const $=x=>document.getElementById(x);
async function request(path,method='GET') {
  const r=await fetch(path,{method,headers:{'X-API-Key':apiKey},cache:'no-store'});
  let payload;try{payload=await r.json()}catch{payload={detail:'Phản hồi không hợp lệ'}}
  if(!r.ok)throw new Error((payload.detail&&JSON.stringify(payload.detail))||('HTTP '+r.status));return payload;
}
async function update(){if(!connected)return;try{
  const [accounts,jobs,config]=await Promise.all([request('/v1/accounts'),request('/v1/sync/status'),request('/v1/config')]);
  $('providers').hidden=false;
  $('spotifyAuthorize').disabled=!config.spotify_oauth_configured;
  $('spotifyRedirect').hidden=!config.spotify_redirect_uri;
  $('spotifyUri').textContent=config.spotify_redirect_uri;
  for (const provider of ['spotify','ytmusic','apple']) {
    const account=accounts.find(a=>a.provider===provider);
    let status='Thiếu cấu hình trên máy chủ';
    if (config.provider_ready[provider]) {
      status='Đã cấu hình, chờ nhập lần đầu';
      if (account?.last_error) status='Lần nhập gần nhất báo lỗi. Xem trạng thái bên dưới.';
      else if (account?.last_success_at) status='Đã nhập thành công lúc '+account.last_success_at+' UTC';
    } else if (provider==='spotify' && config.spotify_oauth_configured) {
      status='Chờ cấp quyền Spotify';
    }
    $(provider+'Status').textContent=status;
  }
  $('status').textContent=JSON.stringify({mode:config.write_enabled?'REMOTE WRITE':'READ-ONLY',baseline_approved:config.baseline_approved,poll_minutes:config.poll_minutes,accounts,jobs},null,2);
  let busy=Object.values(jobs).some(x=>x.state==='queued'||x.state==='running');
  document.querySelectorAll('button[data-provider]').forEach(b=>b.disabled=busy);
}catch(e){$('status').textContent=e.message}}
$('connect').addEventListener('click',async()=>{apiKey=$('token').value.trim();$('token').value='';try{await request('/v1/config');connected=true;$('message').textContent='Đã kết nối. Bạn có thể chạy Sync Now.';await update();clearInterval(refreshHandle);refreshHandle=setInterval(()=>{if(!document.hidden)update()},5000)}catch(e){connected=false;apiKey='';$('providers').hidden=true;$('spotifyLink').hidden=true;$('message').textContent='Không thể kết nối: '+e.message}});
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
document.querySelectorAll('button[data-provider]').forEach(b=>b.addEventListener('click',async()=>{if(!connected)return;$('message').textContent='Đang xếp hàng: '+b.dataset.provider;try{let p=await request('/v1/sync/'+b.dataset.provider,'POST');$('message').textContent='Đã nhận tác vụ: '+JSON.stringify(p);await update()}catch(e){$('message').textContent='Lỗi: '+e.message}}));
</script></body></html>'''
