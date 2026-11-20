"""Transfer wizard for copying selected music to a private playlist."""

PANEL = '''
<section id="transfer" aria-labelledby="transferHeading">
  <header class="transfer-header">
    <h1 id="transferHeading">Chuyển nhạc</h1>
    <p>Chọn nhạc từ thư viện hoặc tệp JSON để tạo playlist riêng trên Spotify hay YouTube Music.</p>
  </header>
  <ol class="transfer-steps" aria-label="Các bước chuyển nhạc">
    <li aria-current="step">Nguồn</li><li>Nội dung</li><li>Đích</li><li>Kiểm tra</li>
  </ol>
  <div data-transfer-step="0">
    <h2 tabindex="-1">Nhạc của bạn đang ở đâu?</h2>
    <div class="music-services" id="transferSources">
      <button type="button" data-source="spotify" disabled><strong>Spotify</strong><small></small></button>
      <button type="button" data-source="ytmusic" disabled><strong>YouTube Music</strong><small></small></button>
      <button type="button" data-source="apple" disabled><strong>Apple Music</strong><small></small></button>
    </div>
    <label class="transfer-file" for="transferFile">Mở tệp JSON đã xuất
      <input id="transferFile" type="file" accept=".json,application/json">
    </label>
  </div>
  <div data-transfer-step="1" hidden>
    <h2 tabindex="-1">Chọn nhạc cần chuyển</h2>
    <div id="transferCollections" class="music-collections" aria-label="Bộ sưu tập nguồn"></div>
    <div id="transferSelection" hidden>
      <div class="transfer-toolbar">
        <h3 id="transferCollectionTitle"></h3>
        <button type="button" id="transferExport">Tải JSON</button>
      </div>
      <div class="transfer-toolbar">
        <input id="transferTrackSearch" type="search" aria-label="Tìm bài hát hoặc nghệ sĩ" placeholder="Tìm bài hát hoặc nghệ sĩ">
        <button type="button" id="transferSelectAll">Chọn tất cả</button>
        <button type="button" id="transferSelectNone">Bỏ chọn tất cả</button>
      </div>
      <ul id="transferSelectionTracks" class="music-tracks" aria-label="Danh sách bài hát nguồn"></ul>
      <p id="transferTrackCount" class="transfer-count" aria-live="polite"></p>
      <button type="button" class="primary" id="transferChooseTarget">Chọn đích</button>
    </div>
  </div>
  <div data-transfer-step="2" hidden>
    <h2 tabindex="-1">Chuyển đến đâu?</h2>
    <p id="transferSelectionSummary"></p>
    <div class="music-services" id="transferTargets">
      <button type="button" data-target="spotify" disabled><strong>Spotify</strong><small></small></button>
      <button type="button" data-target="ytmusic" disabled><strong>YouTube Music</strong><small></small></button>
    </div>
  </div>
  <div data-transfer-step="3" hidden>
    <h2 id="transferReviewHeading" tabindex="-1">Kiểm tra trước khi chuyển</h2>
    <p id="transferDirection"></p>
    <div id="transferReview" hidden>
      <div class="transfer-totals">
        <p><strong id="transferTotal">0</strong> Bài đã chọn</p>
        <p><strong id="transferMatched">0</strong> Đã ghép</p>
        <p><strong id="transferMissing">0</strong> Chưa ghép</p>
      </div>
      <div class="transfer-toolbar">
        <label class="inline"><input type="checkbox" id="transferMissingOnly"> Chỉ xem bài chưa ghép</label>
        <button type="button" id="transferCsv" hidden>Tải CSV bài chưa ghép</button>
      </div>
      <ul id="transferTracks" class="music-tracks" aria-label="Kết quả ghép bài"></ul>
      <form id="transferForm">
        <label class="choose" for="transferTitle">Tên playlist mới
          <input id="transferTitle" maxlength="100" required>
        </label>
        <p id="transferAvailability"></p>
        <button type="submit" class="primary" id="transferExecute" disabled>Tạo playlist và chuyển bài đã ghép</button>
      </form>
      <div id="transferOutcome" class="jobbox" role="status" hidden></div>
    </div>
  </div>
  <div class="transfer-footer">
    <button id="transferBack" type="button" hidden>Quay lại</button>
    <p id="transferStatus" role="status"></p>
  </div>
</section>
'''

STYLE = '''
#transfer{padding:clamp(18px,4vw,38px);--transfer-accent:#d9f57e;background:#171c28}
#transfer .transfer-header{background:none;border:0}
#transfer .transfer-header h1{font-size:clamp(34px,6vw,58px);letter-spacing:-.055em}
.transfer-header p,#transfer [data-transfer-step]>p,#transferStatus,#transferAvailability{color:var(--muted)}
.transfer-steps{list-style:none;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:28px 0;padding:0}
.transfer-steps li{border-top:3px solid #41485a;padding:10px 0 0;color:var(--muted);font-size:.9rem;font-weight:650}
.transfer-steps li[data-complete="true"]{border-color:#8ca04f}
.transfer-steps [aria-current="step"]{border-color:var(--transfer-accent);color:var(--transfer-accent)}
.music-services{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,178px),1fr));gap:10px;max-width:820px}
#transfer .music-services button,.music-collections button{text-align:left;background:#222939;border-color:#3e485f}
#transfer .music-services button{display:grid;gap:4px;padding:16px}
#transfer .music-services button:hover:not(:disabled),.music-collections button:hover:not(:disabled){border-color:var(--transfer-accent)}
.music-services strong{font-size:1.07rem}
.music-services small{display:block}
.transfer-file{display:grid;gap:6px;margin-top:22px;font-weight:650}
.music-collections{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr));gap:8px;margin:12px 0 20px}
.music-collections button{display:grid;gap:3px;overflow-wrap:anywhere}
.music-collections button[aria-pressed="true"]{border-color:var(--transfer-accent)}
.transfer-toolbar,.transfer-footer{display:flex;align-items:center;flex-wrap:wrap;gap:10px}
.transfer-toolbar{margin:12px 0}
.transfer-toolbar h3,.transfer-footer p{margin:0}
.transfer-footer{margin-top:18px}
.transfer-footer p:empty{display:none}
.music-tracks{list-style:none;padding:0;border:1px solid var(--border);border-radius:12px;margin:10px 0 14px;max-height:260px;overflow:auto;background:#191f2b}
.music-tracks li{padding:8px 12px;overflow-wrap:anywhere}
.music-tracks li+li{border-top:1px solid var(--border)}
.music-tracks label{display:flex;align-items:center;gap:10px}
.music-tracks input{flex:none}
.music-tracks li>small{display:block}
.music-tracks li.empty{color:var(--muted)}
.transfer-count{font-variant-numeric:tabular-nums}
.transfer-totals{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:14px 0}
.transfer-totals p{margin:0;background:#222939;border:1px solid #3e485f;border-radius:12px;padding:10px;font-size:.87rem}
.transfer-totals strong{display:block;font-size:clamp(1.4rem,5vw,2rem);line-height:1.25;color:var(--transfer-accent)}
#transfer button.primary{background:var(--transfer-accent);color:#1a2712}
#transfer button.primary:hover:not(:disabled){background:#ecffb8}
#transferAvailability:empty{display:none}
#transferOutcome[data-state="needs_review"]{border-left:4px solid #ffc486}
'''

SCRIPT = r'''
const transfer = {step:0, source:null, collection:null, library:null, selected:new Set(), target:null,
  plan:null, config:null, busy:false, outcome:null};
const transferSteps=[...document.querySelectorAll('[data-transfer-step]')];
const transferStatus=message=>{$('transferStatus').textContent=message;};

function setTransferStep(step){
  transfer.step=step;
  transferSteps.forEach((panel,index)=>{panel.hidden=index!==step;});
  document.querySelectorAll('.transfer-steps li').forEach((item,index)=>{
    if(index===step)item.setAttribute('aria-current','step');else item.removeAttribute('aria-current');
    item.dataset.complete=String(index<step);
  });
  $('transferBack').hidden=step===0;
  transferSteps[step].querySelector('h2').focus();
}

function syncTransferConfig(config){
  transfer.config=config;
  document.querySelectorAll('[data-source], [data-target]').forEach(button=>{
    const ready=Boolean(config?.provider_ready[button.dataset.source||button.dataset.target]);
    button.disabled=transfer.busy || !ready;
    button.querySelector('small').textContent=ready?'Đã kết nối':'Chưa kết nối';
  });
  const retryAt=config?.[transfer.target+'_retry_at'];
  $('transferAvailability').textContent=config && !config.playlist_transfer_enabled
    ? 'Máy chủ chưa bật chuyển playlist. Vẫn có thể xem trước kết quả ghép và tải báo cáo.'
    : retryAt ? names[transfer.target]+' đang giới hạn yêu cầu. Có thể thử lại từ '+retryTime(retryAt)+'.'
    : '';
  $('transferExecute').disabled=transfer.busy || !transfer.plan?.matched || Boolean(transfer.outcome)
    || !config?.playlist_transfer_enabled || !config.provider_ready[transfer.target] || Boolean(retryAt);
}

function transferBusy(busy){
  transfer.busy=busy;
  $('transferBack').disabled=busy;
  $('transferFile').disabled=busy;
  $('transferChooseTarget').disabled=busy || !transfer.selected.size;
  $('transferTitle').disabled=busy || Boolean(transfer.outcome);
  $('transferCollections').querySelectorAll('button').forEach(button=>{
    button.disabled=busy || button.dataset.available!=='true';
  });
  syncTransferConfig(transfer.config);
}

function textElement(tag,text){
  const node=document.createElement(tag);
  node.textContent=text;
  return node;
}

function downloadTransfer(data,type,filename){
  const url=URL.createObjectURL(new Blob([data],{type}));
  const link=document.createElement('a');
  link.href=url;
  link.download=filename;
  link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}

function showTransferTracks(){
  const query=$('transferTrackSearch').value.trim().toLocaleLowerCase('vi');
  const rows=transfer.library.tracks.flatMap((track,index)=>{
    const title=[track.title||'Bài chưa có tên',track.artist].filter(Boolean).join(' · ');
    if(!title.toLocaleLowerCase('vi').includes(query))return [];
    const row=document.createElement('li'), label=document.createElement('label'), box=document.createElement('input');
    box.type='checkbox';
    box.checked=transfer.selected.has(index);
    box.onchange=()=>{
      if(box.checked)transfer.selected.add(index);else transfer.selected.delete(index);
      updateTransferSelectionCount();
    };
    label.append(box,textElement('span',title));
    row.append(label);
    return [row];
  });
  $('transferSelectionTracks').replaceChildren(...(rows.length ? rows : [textElement('li','Không tìm thấy bài hát.')]));
  updateTransferSelectionCount();
}

function updateTransferSelectionCount(){
  $('transferTrackCount').textContent=transfer.selected.size+' / '+transfer.library.tracks.length+' bài đã chọn';
  $('transferChooseTarget').disabled=transfer.busy || !transfer.selected.size;
}

function selectTransferLibrary(collection,library){
  transfer.collection=collection;
  transfer.library=library;
  transfer.selected=new Set(library.tracks.map((_,index)=>index));
  $('transferCollectionTitle').textContent=collection.title;
  $('transferTrackSearch').value='';
  $('transferSelection').hidden=false;
  showTransferTracks();
}

async function selectTransferCollection(button,collection){
  if(transfer.busy)return;
  transferBusy(true);
  transferStatus('Đang tải danh sách bài hát…');
  try{
    const query=collection.id ? '?'+new URLSearchParams({playlist_id:collection.id}) : '';
    const library=await request('/v1/transfers/export/'+transfer.source+query);
    $('transferCollections').querySelectorAll('button').forEach(option=>{
      option.setAttribute('aria-pressed',String(option===button));
    });
    selectTransferLibrary(collection,library);
    transferStatus('');
  }catch(error){transferStatus(error.message);}
  finally{transferBusy(false);}
}

async function openTransferSource(source){
  transfer.source=source;
  transfer.library=null;
  transfer.selected.clear();
  $('transferFile').value='';
  $('transferCollections').replaceChildren();
  $('transferSelection').hidden=true;
  setTransferStep(1);
  transferBusy(true);
  transferStatus('Đang tải thư viện '+names[source]+'…');
  try{
    const {collections,max_tracks}=await request('/v1/transfers/collections/'+source);
    for(const collection of collections){
      const button=document.createElement('button');
      button.type='button';
      button.dataset.available=String(collection.count>0 && collection.count<=max_tracks);
      button.setAttribute('aria-pressed','false');
      const detail=collection.count+' bài'+(collection.count>max_tracks?' · Vượt giới hạn '+max_tracks+' bài':'');
      button.append(textElement('strong',collection.title),textElement('small',detail));
      button.onclick=()=>void selectTransferCollection(button,collection);
      $('transferCollections').append(button);
    }
    transferStatus(collections.length?'':'Thư viện đã nhập chưa có playlist hoặc bài hát.');
  }catch(error){transferStatus(error.message);}
  finally{transferBusy(false);}
}

function showTransferMatches(){
  const rows=transfer.plan.tracks.filter(track=>!$('transferMissingOnly').checked || !track.target_id).map(track=>{
    const row=document.createElement('li');
    row.append(textElement('strong',[track.title||'Bài chưa có tên',track.artist].filter(Boolean).join(' · ')),
      textElement('small',track.target_id?'Đã ghép':track.match==='ambiguous'?'Có nhiều bản phù hợp':
        track.match==='unavailable'?'Bài không khả dụng ở nguồn':'Chưa có bản ghép đã xác minh'));
    return row;
  });
  $('transferTracks').replaceChildren(...(rows.length?rows:[textElement('li','Không có bài trong danh sách này.')]));
}

async function reviewTransfer(target){
  transfer.target=target;
  transfer.plan=null;
  transfer.outcome=null;
  $('transferReview').hidden=true;
  $('transferOutcome').hidden=true;
  $('transferReviewHeading').textContent='Kiểm tra trước khi chuyển';
  $('transferDirection').textContent=names[transfer.source]+' → '+names[target]+' · '+transfer.collection.title;
  setTransferStep(3);
  transferBusy(true);
  transferStatus('Đang ghép bài từ thư viện và kết quả tra cứu đã lưu của '+names[target]+'…');
  try{
    transfer.payload={library:{...transfer.library,tracks:transfer.library.tracks.filter((_,index)=>transfer.selected.has(index))},target};
    const plan=transfer.plan=await request('/v1/transfers/preview','POST',transfer.payload);
    $('transferTotal').textContent=plan.total;
    $('transferMatched').textContent=plan.matched;
    $('transferMissing').textContent=plan.total-plan.matched;
    $('transferMissingOnly').checked=false;
    $('transferCsv').hidden=plan.matched===plan.total;
    $('transferTitle').value=transfer.collection.title.slice(0,100);
    $('transferReview').hidden=false;
    showTransferMatches();
    transferStatus('');
  }catch(error){transferStatus(error.message);}
  finally{transferBusy(false);}
}

document.querySelectorAll('[data-source]').forEach(button=>{
  button.onclick=()=>void openTransferSource(button.dataset.source);
});
document.querySelectorAll('[data-target]').forEach(button=>{
  button.onclick=()=>void reviewTransfer(button.dataset.target);
});
$('transferFile').onchange=async()=>{
  const file=$('transferFile').files[0];
  if(!file)return;
  transferBusy(true);
  try{
    if(file.size>3000000)throw new Error('Tệp vượt quá 3 MB.');
    const library=JSON.parse(await file.text());
    if(library?.version!==1 || !Object.hasOwn(names,library.provider) || !Array.isArray(library.tracks)
      || !library.tracks.every(track=>track && typeof track.title==='string')){
      throw new Error('Chọn tệp JSON do Sync-Music xuất.');
    }
    transfer.source=library.provider;
    $('transferCollections').replaceChildren();
    selectTransferLibrary({title:file.name.replace(/\.json$/i,'')||'Playlist đã nhập'},library);
    setTransferStep(1);
    transferStatus('');
  }catch(error){transferStatus(error.message);}
  finally{$('transferFile').value='';transferBusy(false);}
};
$('transferTrackSearch').oninput=showTransferTracks;
$('transferSelectAll').onclick=()=>{
  transfer.selected=new Set(transfer.library.tracks.map((_,index)=>index));
  showTransferTracks();
};
$('transferSelectNone').onclick=()=>{transfer.selected.clear();showTransferTracks();};
$('transferChooseTarget').onclick=()=>{
  $('transferSelectionSummary').textContent=transfer.collection.title+' · '+transfer.selected.size+' bài';
  setTransferStep(2);
  transferStatus('');
};
$('transferBack').onclick=()=>{
  if(transfer.busy)return;
  setTransferStep(transfer.step-1);
  transferStatus('');
};
$('transferExport').onclick=()=>downloadTransfer(JSON.stringify(transfer.library,null,2),'application/json',transfer.source+'-library.json');
$('transferMissingOnly').onchange=showTransferMatches;
$('transferCsv').onclick=()=>{
  const csv=value=>{
    const text=String(value??'');
    return '"'+(/^[\s]*[=+\-@]|^[\t\r\n＝＋－＠]/u.test(text)?"'":'')+text.replaceAll('"','""')+'"';
  };
  const rows=transfer.plan.tracks.filter(track=>!track.target_id).map(track=>
    [track.position,track.title,track.artist,track.match].map(csv).join(','));
  downloadTransfer('\uFEFF'+['Vị trí,Bài hát,Nghệ sĩ,Kết quả ghép',...rows].join('\r\n'),'text/csv;charset=utf-8','bai-chua-ghep.csv');
};
$('transferForm').onsubmit=async event=>{
  event.preventDefault();
  const title=$('transferTitle').value.trim();
  if(!title || $('transferExecute').disabled)return;
  transferBusy(true);
  transferStatus('Đang chờ máy chủ tạo playlist và xác minh các bài đã chuyển…');
  try{
    const result=transfer.outcome=await request('/v1/transfers/execute','POST',{...transfer.payload,title,plan:transfer.plan.plan});
    const status=result.status==='completed'?'Đã chuyển và xác minh '+result.added+' bài.'
      : result.status==='running'?'Máy chủ đang xử lý playlist này.'
      : 'Playlist cần kiểm tra lại. Máy chủ đã xác nhận ghi '+result.added+' bài.';
    const outcome=$('transferOutcome');
    outcome.dataset.state=result.status;
    outcome.replaceChildren(textElement('p',status));
    if(result.playlist_id && /^[A-Za-z0-9_-]+$/.test(result.playlist_id)){
      const link=textElement('a','Mở playlist trên '+names[transfer.target]);
      link.href=transfer.target==='spotify'?'https://open.spotify.com/playlist/'+result.playlist_id
        :'https://music.youtube.com/playlist?list='+result.playlist_id;
      link.target='_blank';
      link.rel='noopener noreferrer';
      outcome.append(link);
    }
    outcome.hidden=false;
    $('transferReviewHeading').textContent='Kết quả chuyển nhạc';
    transferStatus('');
  }catch(error){transferStatus(error.message);}
  finally{transferBusy(false);}
};
'''
