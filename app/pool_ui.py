"""Controls on the main dashboard for all configured account slots."""
import json


def panel(profiles):
    return PANEL.replace('__SLOTS__', json.dumps(list(profiles)))


PANEL = """
<section id="pool"><h2>Đồng bộ theo tài khoản</h2>
<label>Nguồn <select id="poolFrom"></select></label>
<label>Đích <select id="poolTo"></select></label>
<div class="flex"><button data-route="run">Đồng bộ một chiều</button>
<button data-route="schedule">Bật tự đồng bộ</button></div>
<p id="poolMessage" role="status"></p><div id="poolRoutes"></div>
<details id="poolSettings"><summary>Pool tài khoản</summary>
<div class="flex"><label>Tên nhóm tài khoản mới <input id="poolName" placeholder="gia-dinh"></label><button id="poolAdd">Thêm nhóm tài khoản</button></div>
<p id="poolAccountMessage" role="status"></p><div id="poolAccounts" class="providers-grid"></div></details></section>
<script>
document.addEventListener('DOMContentLoaded',()=>{
 const slots=__SLOTS__, prefix=s=>s==='main'?'':'/profiles/'+s;
 const label=(s,p)=>(p==='spotify'?'Spotify':'YouTube Music')+' · '+(s==='main'?'Chính':s);
 const node=(tag,text)=>{const n=document.createElement(tag);n.textContent=text;return n};
 const cards=[], routeNames=Object.fromEntries(slots.flatMap(slot=>['spotify','ytmusic'].map(provider=>
   [slot+'/'+provider,label(slot,provider)])));
 const services={spotify:'Spotify',ytmusic:'YouTube Music'};
 const routeStates={queued:'Đã xếp hàng',running:'Đang đồng bộ',idle:'Chưa chạy',completed:'Hoàn tất',
   partial:'Hoàn tất một phần',failed:'Gặp lỗi',interrupted:'Bị gián đoạn',account_changed:'Tài khoản đã thay đổi'};
 $('poolAdd').onclick=async()=>{try{
   await request('/v1/pool/accounts?'+new URLSearchParams({name:$('poolName').value.trim()}),'POST');
   location.reload();
 }catch(e){$('poolAccountMessage').textContent=e.message}};
 function action(parent,text,fn,errorId='poolAccountMessage'){
   const btn=node('button',text);parent.append(btn);
   btn.onclick=async()=>{btn.disabled=true;try{await fn()}catch(e){$(errorId).textContent=e.message}finally{btn.disabled=false}};
   return btn;
 }
 async function command(s,p,kind){return request(prefix(s)+'/v1/'+kind,'POST')}
 for(const s of slots)for(const p of ['spotify','ytmusic']){
   const card=node('article','');card.className='provider';$('poolAccounts').append(card);
   card.append(node('h3',label(s,p)));const status=node('p','');card.append(status);
   const result=node('p','');result.setAttribute('role','status');
   const link=node('a','');link.target='_blank';link.rel='noopener noreferrer';link.hidden=true;
   let grantVersion=0;
   const login=action(card,p==='spotify'?'Kết nối Spotify':'Kết nối bằng Google',async()=>{
     const version=++grantVersion;link.hidden=true;result.textContent='';
     const grant=await command(s,p,'auth/'+p+'/start');
     link.href=grant.authorization_url||grant.verification_url;
     link.textContent=p==='spotify'?'Mở Spotify':'Mở Google để nhập mã';link.hidden=false;
     if(p==='ytmusic'){result.textContent='Mã Google: '+grant.user_code;
       const poll=async()=>{if(version!==grantVersion||!connected)return;
         try{const reply=await request(prefix(s)+'/v1/auth/ytmusic/poll','POST',{handle:grant.handle});
           if(reply.status==='pending'){setTimeout(poll,reply.interval*1000);return}
           link.hidden=true;result.textContent=reply.status==='connected'?'Đã cấp quyền. Đang nhập thư viện.':'Mã đã hết hạn hoặc bị từ chối.';
           await refreshPool();
         }catch(e){result.textContent=e.message}
       };setTimeout(poll,grant.interval*1000)}
   });
   action(card,'Nhập thư viện',async()=>{await command(s,p,'sync/'+p);result.textContent='Đã xếp hàng nhập.'});
   action(card,'Gỡ kết nối',async()=>{
     if(!confirm('Gỡ '+label(s,p)+' và dừng lịch của hồ sơ? Tệp phiên YouTube của hồ sơ cũng bị xóa khi gỡ YouTube Music.'))return;
     await request(prefix(s)+'/v1/auth/'+p+'/connection','DELETE');grantVersion++;link.hidden=true;result.textContent='Đã gỡ kết nối.';await refreshPool()
   });
   if(p==='ytmusic'){
     const path='private/'+(s==='main'?'':s+'/')+'ytmusic_browser.json';
     const mode=node('p','');mode.className='muted';card.append(mode);
     card._mode=mode;
     const selectMode=async mode=>{
       await request(prefix(s)+'/v1/auth/ytmusic/mode','PUT',{mode});
       result.textContent='Đã kiểm tra Liked Music và đổi cách kết nối. Lịch đồng bộ đã tạm dừng.';
       await refreshPool();
     };
     action(card,'Dùng phiên trình duyệt',async()=>selectMode('browser'));
     action(card,'Dùng quyền Google đã lưu',async()=>selectMode('oauth'));
     card._path=path;
   }
   if(p==='spotify'){const uri=node('small','');card.append(uri);card._uri=uri}
   card.append(link,result);cards.push({s,p,status,login,card});
   for(const id of ['poolFrom','poolTo'])$(id).add(new Option(label(s,p),s+'/'+p));
 }
 $('poolTo').selectedIndex=1;
 let loading=false, renderedRoutes='';
 async function refreshPool(){if(!connected||document.hidden||loading)return;loading=true;
 try{
   for(const s of slots){const view=await request(prefix(s)+'/v1/overview');
     for(const c of cards.filter(c=>c.s===s)){
       const a=view.accounts.find(a=>a.provider===c.p);
       const googleMissing=c.p==='ytmusic'&&!view.config.ytmusic_oauth_configured;
       c.status.textContent=googleMissing?'Thiếu YTM_CLIENT_ID hoặc YTM_CLIENT_SECRET trong runtime.':
         (view.config.provider_ready[c.p]?(a?.last_error||'Đã cấu hình'):'Chưa kết nối');
       if(c.card._uri)c.card._uri.textContent='Redirect URI: '+view.config.spotify_redirect_uri;
       if(c.card._mode){
         const mode=view.config.ytmusic_auth_mode;
         c.card._mode.textContent=(mode==='browser'?'Đang dùng phiên trình duyệt. ':
           mode==='oauth'?'Đang dùng quyền Google. ':'Đang ưu tiên quyền Google nếu có. ')+
           (view.config.ytmusic_browser_available?'Đã có tệp phiên.':'Chưa có tệp '+c.card._path+'.');
       }
       c.login.disabled=googleMissing;
     }}
   const rows=await request('/v1/pool/routes'), key=JSON.stringify(rows);
   if(key===renderedRoutes)return;
   renderedRoutes=key;
   $('poolRoutes').replaceChildren();
   for(const row of rows){
       const line=node('article','');line.className='jobbox';$('poolRoutes').append(line);
       line.append(node('h3',routeNames[row.source]+' → '+routeNames[row.target]));
       const state=row.retry_at?'Đang chờ '+(services[row.retry_provider]||'dịch vụ'):
         row.state==='running'?(stages[row.stage]||routeStates.running):routeStates[row.state];
       line.append(node('p',state+(row.scheduled?' · Có lịch':'')));
       if(row.covered_by_primary)line.append(node('small','Lịch đồng bộ thư viện đã bao gồm hướng này.'));
       if(row.retry_at)line.append(node('p','Tự tiếp tục từ '+retryTime(row.retry_at)));
       else if(row.state==='running' && row.total){
         const bar=node('progress','');
         bar.setAttribute('aria-label','Tiến độ đồng bộ');
         showProgress(bar,row.done,row.total);
         line.append(bar,node('p',row.done+' / '+row.total+' mục'));
       }
       if(row.error)line.append(node('p',row.error));
       const actions=node('div','');actions.className='flex';line.append(actions);
       if(row.scheduled)action(actions,'Dừng lịch',async()=>{await sendRoute(row.source,row.target,'stop')},'poolMessage');
       const running=['queued','running'].includes(row.state);
       for(const entry of row.undo||[])if(entry.confirmed && !running)action(actions,'Hoàn tác',async()=>{
         if(confirm('Hoàn tác lượt đã chọn và dừng lịch này?')){
           await request('/v1/pool/undo?'+new URLSearchParams({source:row.source,target:row.target,transfer_id:entry.job_id}),'POST');await refreshPool()}
       },'poolMessage');
   }
 }catch(e){$('poolMessage').textContent=e.message}finally{loading=false}}
 async function sendRoute(source,target,action){
   const result=await request('/v1/pool/route?'+new URLSearchParams({source,target,action}),'POST');
   $('poolMessage').textContent=action==='stop'?'Đã dừng lịch.':result.covered_by_primary
     ?'Đã lưu lịch. Lịch đồng bộ thư viện đang xử lý hướng này.':'Đã nhận lượt đồng bộ.';
   await refreshPool();
 }
 const routeButtons=[...document.querySelectorAll('[data-route]')];
 const updateRouteChoices=()=>{
   const source=$('poolFrom').value.split('/')[1];
   for(const option of $('poolTo').options)option.disabled=option.value.split('/')[1]===source;
   if($('poolTo').selectedOptions[0]?.disabled)$('poolTo').value=[...$('poolTo').options].find(option=>!option.disabled).value;
 };
 $('poolFrom').onchange=updateRouteChoices;
 updateRouteChoices();
 routeButtons.forEach(btn=>btn.onclick=async()=>{
   routeButtons.forEach(button=>{button.disabled=true;});
   try{await sendRoute($('poolFrom').value,$('poolTo').value,btn.dataset.route);}
   catch(error){$('poolMessage').textContent=error.message;}
   finally{routeButtons.forEach(button=>{button.disabled=false;});}
 });
 new MutationObserver(refreshPool).observe($('workspace'),{attributes:true,attributeFilter:['hidden']});
 refreshPool();setInterval(refreshPool,20000);
});
</script>
"""
