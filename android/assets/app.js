(() => {
  const $=id=>document.getElementById(id),nativePending=new Map();let sequence=0,foreground=true,visible=true,toastTimer;
  const app={visit:'',name:'Friend',busy:false,voice:null,config:{},pairing:false};
  app.delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  app.native=(method,...args)=>new Promise((resolve,reject)=>{
    const id=String(++sequence);nativePending.set(id,{resolve,reject});
    try{if(!window.AnimaNative)throw Error('Open this interface in the Anima Android app.');AnimaNative[method](id,...args);}
    catch(e){nativePending.delete(id);reject(e);}
  });
  window.nativeReply=(id,result)=>{const p=nativePending.get(String(id));if(!p)return;nativePending.delete(String(id));if(result.error){const e=Error(result.error);e.status=result._status;p.reject(e);}else p.resolve(result);};
  app.api=(route,data,method)=>{
    const body=data===undefined?'':JSON.stringify({...data,...(app.visit?{visit:app.visit}:{})});
    return app.native('request',route,method||(data===undefined?'GET':'POST'),body);
  };
  app.base64=bytes=>{let text='';for(let i=0;i<bytes.length;i+=8192)text+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(text);};
  app.binary=async route=>{const r=await app.native('request',route,'BINARY','');const text=atob(r.audio);return Uint8Array.from(text,c=>c.charCodeAt(0)).buffer;};
  app.notice=text=>{$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,7500);};
  app.scroll=()=>{$('messages').scrollTop=$('messages').scrollHeight;};
  app.add=(role,text,preview)=>{const d=document.createElement('div');d.className=role==='system'?'system':'message '+role;if(preview){const im=document.createElement('img');im.src=preview;im.alt='Shared photo';d.appendChild(im);}const span=document.createElement('span');span.textContent=text||'';d.appendChild(span);$('messages').appendChild(d);app.scroll();return d;};
  app.typing=()=>{const d=document.createElement('div');d.className='typing';d.textContent='•••';$('messages').appendChild(d);app.scroll();return d;};
  app.setBusy=value=>{app.busy=value;$('send').disabled=value;$('share').disabled=value;if(app.voice)$('interruptReply').disabled=!app.voice.enabled||!(value||app.voice.draining||app.voice.job||app.voice.players.size);};
  app.result=result=>{
    if(result.error)throw Error(result.error);
    if(result.thinking){const d=document.createElement('details');d.className='thinking';const summary=document.createElement('summary');summary.textContent='Thinking';const content=document.createElement('div');content.textContent=result.thinking;d.append(summary,content);$('messages').appendChild(d);}
    if(result.tools&&result.tools.length){const group=document.createElement('div');group.className='tools';for(const t of result.tools){const chip=document.createElement('span');chip.className='tool';chip.textContent=t.name;chip.title=t.result;group.appendChild(chip);}$('messages').appendChild(group);}
    if(result.reply)app.add('her',result.reply);
    for(const note of result.notes||[])app.add('system',note);
    for(const voice of result.voices||[])app.binary(voice.url).then(buffer=>{const a=document.createElement('audio');a.controls=true;a.src=URL.createObjectURL(new Blob([buffer],{type:voice.url.endsWith('.wav')?'audio/wav':'audio/ogg'}));const d=app.add('her','Voice note');d.appendChild(a);}).catch(e=>app.notice(e.message));
    app.scroll();
  };
  app.voice=new PhoneVoice(app);
  app.audioSettings=new AudioSettings(app);
  app.spatial=new SpatialVoice(()=>app.voice,app.notice,app);
  app.deliveryPreview=new DeliveryPreview(app);
  app.previewDelivery=()=>app.deliveryPreview.play();
  app.stopDeliveryPreview=()=>app.deliveryPreview.stop();
  window.microphoneFrame=data=>app.voice.frame(data);
  window.microphoneError=message=>app.voice.restartMicrophone(message,true);
  window.microphoneRoute=info=>{app.audioSettings.route(info);if(info.requestedMatched===false)app.notice('Android is using a different microphone from the selected one. Check Audio settings.');};
  window.audioDevicesChanged=()=>{if($('audioDialog').open)app.audioSettings.devices();};
  function name(info){app.name=info.name||'Friend';$('friendName').textContent=app.name;$('connectedName').textContent=app.name+' is here';}
  function ready(info){name(info);$('connectionCard').hidden=true;$('readyCard').hidden=false;$('startVisit').disabled=!!info.closing||!!info.occupied;$('startStatus').textContent=info.closing?'Writing the last visit down…':info.occupied?'A visit is open on another phone.':'';}
  function enter(info){
    app.visit=info.visit;name(info);$('setup').hidden=true;$('room').hidden=false;$('messages').replaceChildren();
    $('presenceLabel').textContent=info.physicalize?'Sharing the same room':'A visit on your phone';
    app.add('system',info.physicalize?'A little room together.':'You’re here with '+app.name+'.');
    for(const t of info.history||[])app.add(t.role==='user'?'me':'her',t.content);
  }
  async function connect(){
    if(app.pairing)return;app.pairing=true;$('connect').disabled=true;$('connectionStatus').textContent='Connecting…';
    try{
      let info;
      if(app.config.paired&&$('address').value.trim().replace(/\/$/,'')===app.config.address&&!$('pairCode').value.trim())info=await app.api('/api/info');
      else{await app.native('pair',$('address').value,$('pairCode').value);app.config=JSON.parse(AnimaNative.configuration());info=await app.api('/api/info');}
      ready(info);if(info.visit)enter(info);
    }catch(e){$('connectionStatus').textContent=e.message;}
    finally{app.pairing=false;$('connect').disabled=false;}
  }
  $('connect').onclick=connect;
  $('startVisit').onclick=async()=>{
    $('startVisit').disabled=true;$('startStatus').textContent='Opening the visit…';
    try{const info=await app.api('/api/start',{physicalize:$('physicalize').checked});enter(info);$('startStatus').textContent='';}
    catch(e){$('startStatus').textContent=e.message;}
    finally{$('startVisit').disabled=false;}
  };
  $('physicalize').checked=localStorage.getItem('physicalize')==='true';
  $('physicalize').onchange=()=>localStorage.setItem('physicalize',String($('physicalize').checked));
  $('changeComputer').onclick=()=>{AnimaNative.forget();app.config={};$('readyCard').hidden=true;$('connectionCard').hidden=false;$('connectionStatus').textContent='Enter the computer address and pairing code.';};
  async function leave(){
    const visit=app.visit;if(!visit)return;$('leave').disabled=true;
    try{await app.voice.stop();await app.api('/api/leave',{});}
    catch(e){app.notice(e.message);}
    finally{if(app.visit===visit)app.visit='';$('room').hidden=true;$('setup').hidden=false;$('leave').disabled=false;try{ready(await app.api('/api/info'));}catch(_){} }
  }
  $('leave').onclick=leave;
  $('voiceToggle').onclick=()=>app.voice.enabled||app.voice.starting?app.voice.stop():app.voice.start();
  $('interruptReply').onclick=()=>app.voice.cancelReply();
  $('message').oninput=()=>{$('message').style.height='auto';$('message').style.height=Math.min(135,$('message').scrollHeight)+'px';};
  $('composer').onsubmit=async event=>{
    event.preventDefault();const text=$('message').value.trim();if(!text||app.busy||!app.visit)return;
    const visit=app.visit;$('message').value='';$('message').style.height='auto';app.add('me',text);app.setBusy(true);const typing=app.typing();
    try{await app.voice.interrupt('typed input');const generation=app.voice.generation;const result=await app.api('/api/send',{text,spoken:app.voice.enabled});if(visit===app.visit){app.result(result);if(result.speech_job){if(app.voice.enabled&&generation===app.voice.generation){if(result.native_playback)app.voice.playNative(result.speech_job);else app.voice.play(result.speech_job);}else await app.voice.muteUnplayed(result.speech_job);}}}
    catch(e){if(visit===app.visit)app.notice(e.message);}finally{typing.remove();app.setBusy(false);}
  };
  $('share').onclick=async()=>{if(!app.visit||app.busy)return;if(app.voice.enabled||app.voice.starting)await app.voice.stop();$('uploadStatus').hidden=false;$('uploadStatus').textContent='Choose photos or files…';AnimaNative.chooseFiles(app.visit);};
  window.uploadProgress=(filename,bytes,total)=>{$('uploadStatus').hidden=false;$('uploadStatus').textContent='Sharing '+filename+' · '+Math.round(bytes/total*100)+'%';};
  window.fileUploaded=(visit,result)=>{if(visit!==app.visit)return;if(result.error)app.notice(result.error);else app.add('me file',result.name+' · shared',result.preview);};
  window.filePickerFinished=()=>{$('uploadStatus').hidden=true;foreground=true;};
  window.appPaused=backgroundVoice=>{visible=false;foreground=!!backgroundVoice;if(!backgroundVoice)app.stopDeliveryPreview();if(!backgroundVoice&&(app.voice.enabled||app.voice.starting))app.voice.stop();};
  window.backgroundVoiceStopped=()=>{foreground=visible;if(app.voice.enabled||app.voice.starting)app.voice.stop();};
  window.permissionPaused=()=>{foreground=false;};
  window.appResumed=()=>{visible=true;foreground=true;if(app.voice.enabled)app.voice.resumeOutput().catch(e=>app.notice(e.message));if(app.visit)app.api('/api/ping',{}).catch(e=>app.notice(e.message));};
  window.backPressed=()=>{if($('audioDialog').open){$('audioDialog').close();return;}if($('spatialDialog').open){app.spatial.stopPreview();$('spatialDialog').close();return;}if(app.visit){if(confirm('Leave this visit?'))leave();}else if(window.AnimaNative&&AnimaNative.closeApp)AnimaNative.closeApp();};
  setInterval(async()=>{
    if(!foreground)return;
    if(app.visit){try{await app.api('/api/ping',{});}catch(e){if(e.status===409){await app.voice.stop();app.visit='';$('room').hidden=true;$('setup').hidden=false;app.notice('The visit ended while the app was away. Start a new one when you’re ready.');}}}
    else if(!$('readyCard').hidden){try{ready(await app.api('/api/info'));}catch(_){} }
  },20000);
  try{app.config=JSON.parse(AnimaNative.configuration());$('address').value=app.config.address||'';if(app.config.paired)connect();}catch(_){}
  window.phoneApp=app; // Tests use the same public client state, with a fake native bridge.
  window.nativeVoiceContinuation=event=>app.voice.receivedContinuation(event).catch(e=>app.notice(e.message));
  window.nativeVoiceInput=event=>app.voice.receivedInput(event);
})();
