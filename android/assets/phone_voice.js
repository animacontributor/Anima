/* Native 16 kHz microphone -> accepted chat -> progressive local WAV playback. */
class PhoneVoice {
  constructor(app){this.app=app;this.enabled=false;this.starting=false;this.stopping=false;this.phase='off';this.life=0;this.generation=0;this.players=new Set();this.queue=[];this.draining=false;this.job=null;this.completed=0;this.interruptTask=Promise.resolve();this.context=null;this.suppressUntil=0;this.lastFrame=0;this.lastMeter=0;this.recovering=false;this.recoveries=0;this.watchdog=null;this.initiativeTimer=null;this.initiativeChecking=false;}
  get previewing(){return !!this._previewing;}
  set previewing(value){this._previewing=!!value;if(this.nativeCapture&&window.AnimaNative?.setCapturePaused)AnimaNative.setCapturePaused(this._previewing);}
  state(label){document.getElementById('voiceStatus').textContent=label;const b=document.getElementById('voiceToggle');b.classList.toggle('active',this.enabled);b.querySelector('span').textContent=this.enabled?'Stop voice':this.starting?'Warming…':'Start voice';b.disabled=this.starting||this.stopping;const cancel=document.getElementById('interruptReply');if(cancel)cancel.disabled=!this.enabled||!(this.job||this.draining||this.players.size||this.app.busy);}
  async start(){
    if(this.enabled||this.starting||this.stopping)return;this.starting=true;const life=++this.life;this.state('Getting the voice ready…');
    try{
      if(this.app.stopDeliveryPreview)this.app.stopDeliveryPreview();
      if(this.app.spatial?.stopPreview)this.app.spatial.stopPreview();this.suppressUntil=0;this.previewing=false;this.wasBlocked=false;
      this.context=new(window.AudioContext||window.webkitAudioContext)();await this.context.resume();
      const context=this.context;
      context.onstatechange=()=>{
        if(this.enabled&&this.context===context&&(context.state==='suspended'||context.state==='interrupted')){
          this.resumeOutput().catch(e=>{if(this.enabled&&this.context===context){this.app.notice(e.message);this.interrupt('audio output recovery failed');}});
        }
      };
      this.output=this.context.createGain();this.output.gain.value=this.app.audioSettings?this.app.audioSettings.value.volume/100:1;this.output.connect(this.context.destination);
      if(this.app.spatial)this.app.spatial.attach(this.context,this.output);
      const settings=await this.app.api('/api/voice/start',{restart:true});
      this.serverInitiative=!!settings.server_initiative;this.serverVoiceVersion=settings.voice_version;this.lastEventSequence=settings.event_cursor||0;
      if(life!==this.life)return;
      if(this.app.audioSettings)await this.app.audioSettings.syncDelivery();
      if(this.app.spatial&&this.app.spatial.app)await this.app.spatial.sync();
      const captureSettings=this.app.audioSettings?this.app.audioSettings.capture(settings):settings;
      this.capture=new PhoneAudio.Capture(captureSettings,event=>this.captured(event));
      this.nativeCapture=!!(window.AnimaNative&&AnimaNative.prepareVoice&&settings.server_initiative);
      if(this.app.audioSettings)await this.app.audioSettings.microphone();
      if(window.AnimaNative&&AnimaNative.prepareVoice)await this.app.native('prepareVoice',this.app.visit,settings.voice_version??-1,settings.event_cursor||0,JSON.stringify(captureSettings));
      this.enabled=true;const mic=await this.app.native('startMicrophone');if(this.app.audioSettings)this.app.audioSettings.route(mic);
      if(life!==this.life){AnimaNative.stopMicrophone();return;}
      this.lastFrame=performance.now();this.recoveries=0;if(!this.nativeCapture)this.watchdog=setInterval(()=>{if(this.enabled&&!this.recovering&&performance.now()-this.lastFrame>3000)this.restartMicrophone('Microphone stopped delivering audio',true);},1000);
      this.phase='listening';this.state('Listening');
      this.initiativeTimer=setInterval(()=>this.checkInitiative(),1000);
    }catch(e){await this.stop();this.app.notice(e.message);}
    finally{this.starting=false;this.state(this.enabled?'Listening':'Voice is off');}
  }
  async stop(){
    if(this.stopping)return;this.stopping=true;
    this.enabled=false;this.starting=false;this.life++;this.queue=[];if(this.capture)this.capture.reset();clearInterval(this.watchdog);this.watchdog=null;
    clearInterval(this.initiativeTimer);this.initiativeTimer=null;
    if(this.app.audioSettings)this.app.audioSettings.meter(0,false);
    if(window.AnimaNative)AnimaNative.stopMicrophone();
    if(window.AnimaNative&&AnimaNative.stopVoiceService)AnimaNative.stopVoiceService();
    this.state('Voice is off');
    await this.interrupt('voice stopped');
    try{if(this.app.visit)await this.app.api('/api/voice/stop',{voice_version:this.serverVoiceVersion});}catch(_){}
    if(this.context&&this.context.state!=='closed')await this.context.close();this.context=null;this.output=null;
    this.phase='off';this.stopping=false;this.state('Voice is off');
  }
  frame(encoded){
    if(!this.enabled||!this.capture)return;
    this.lastFrame=performance.now();if(this.recoveries&&this.lastFrame-(this.recoveredAt||this.lastFrame)>15000)this.recoveries=0;
    const raw=atob(encoded),pcm=new Float32Array(raw.length/2);
    for(let i=0;i<pcm.length;i++){let v=raw.charCodeAt(i*2)|(raw.charCodeAt(i*2+1)<<8);if(v&32768)v-=65536;pcm[i]=v/32768;}
    let energy=0;for(const v of pcm)energy+=v*v;const level=Math.sqrt(energy/pcm.length);
    const blocked=this.previewing||this.speakerBlocked();
    if(this.lastFrame-this.lastMeter>100){this.lastMeter=this.lastFrame;if(this.app.audioSettings)this.app.audioSettings.meter(level,blocked);}
    if(blocked){this.capture.reset();this.wasBlocked=true;if(this.phase==='listening')this.state('Listening in a moment…');return;}
    if(this.wasBlocked){this.wasBlocked=false;if(this.phase==='listening')this.state('Listening');}
    if(!this.nativeCapture)this.capture.push(pcm,this.phase==='speaking');
  }
  speakerBlocked(){return this.app.audioSettings?.value.mode==='speaker'&&(this.phase==='speaking'||this.players.size>0||performance.now()<this.suppressUntil);}
  async resumeOutput(){
    const context=this.context;
    if(!context||context.state==='closed')throw Error('Audio output is closed. Stop voice and start it again.');
    if(context.state==='running')return;
    if(this.outputResume&&this.outputResume.context===context)return this.outputResume.promise;
    const attempt={context,promise:context.resume()};this.outputResume=attempt;
    try{await attempt.promise;}finally{if(this.outputResume===attempt)this.outputResume=null;}
  }
  async restartMicrophone(reason,automatic=false){
    if(!this.enabled||this.recovering)return;this.recovering=true;const life=this.life;
    try{
      if(automatic&&++this.recoveries>2)throw Error('The microphone keeps stopping. Stop voice and start it again, or choose another microphone in Audio settings.');
      this.state('Reconnecting microphone…');await this.interrupt('microphone route changed');AnimaNative.stopMicrophone();this.capture.reset();
      if(this.app.audioSettings)await this.app.audioSettings.microphone();
      if(!this.enabled||life!==this.life)return;
      const info=await this.app.native('startMicrophone');
      if(!this.enabled||life!==this.life){AnimaNative.stopMicrophone();return;}
      if(this.app.audioSettings)this.app.audioSettings.route(info);this.lastFrame=performance.now();this.recoveredAt=this.lastFrame;
      if(automatic)this.app.notice(reason+' — reconnected.');this.state(this.phase==='speaking'?'Speaking':'Listening');
    }catch(e){this.app.notice(e.message);await this.stop();}finally{this.recovering=false;}
  }
  async cancelReply(){
    this.queue=[];if(this.capture)this.capture.reset();const task=this.interrupt('interrupted by button');
    this.phase='listening';this.state(this.draining?'Reply muted — listening':'Listening');
    await task;
  }
  async muteUnplayed(job){
    try{await this.app.api('/api/voice/delivery',{job,completed:0,partial:false,reason:'muted before playback'});await this.app.api('/api/voice/interrupt',{job});}catch(_){}
  }
  initiativeState(){
    const now=performance.now(),blocked=[];
    for(const [code,value] of [['checking',this.initiativeChecking],['busy',this.app.busy],['recognizing',this.draining],
      ['recovering',this.recovering],['preview',this.previewing],['capture',this.capture?.active],['candidate',this.capture?.candidate],
      ['queued',this.queue.length],['playback',this.job],['scheduled_audio',this.players.size],['echo_tail',now<this.suppressUntil],
      ['microphone_stale',now-this.lastFrame>3000]])if(value)blocked.push(code);
    return {blocked,phase:this.phase,mic_age_ms:now-this.lastFrame,queued_turns:this.queue.length,
      scheduled_audio:this.players.size,candidate_ms:this.capture?.candidate||0,echo_tail_ms:Math.max(0,this.suppressUntil-now)};
  }
  async reportInitiativeState(state){
    if(this.stateReporting)return;this.stateReporting=true;
    try{await this.app.api('/api/voice/initiative/state',{state});}catch(_){}
    finally{this.stateReporting=false;}
  }
  inputPending(){return this.capture?.active||this.capture?.candidate>0||this.queue.length>0||this.draining||this.app.busy;}
  async checkInitiative(){
    if(!this.enabled)return;
    const state=this.initiativeState();
    if(this.serverInitiative){await this.reportInitiativeState(state);return;}
    if(state.blocked.length){await this.reportInitiativeState(state);return;}
    this.initiativeChecking=true;const life=this.life,visit=this.app.visit,gen=this.generation;
    try{
      await this.interruptTask;const reply=await this.app.api('/api/voice/initiative',{state});
      while(this.enabled&&life===this.life&&visit===this.app.visit&&gen===this.generation&&this.inputPending())await new Promise(resolve=>setTimeout(resolve,25));
      if(!this.enabled||life!==this.life||visit!==this.app.visit||gen!==this.generation){
        if(reply.speech_job&&visit===this.app.visit)await this.muteUnplayed(reply.speech_job);return;
      }
      if(reply.error)this.app.notice(reply.error);
      else if(reply.reply&&reply.speech_job){this.app.result(reply);if(reply.native_playback)this.playNative(reply.speech_job,life);else this.play(reply.speech_job,life,true);}
    }catch(e){if(this.enabled&&life===this.life)this.app.notice(e.message);}
    finally{this.initiativeChecking=false;}
  }
  captured(event){
    if(event.kind==='start'){this.state(this.job?'Speaking — checking sound…':'Checking sound…');}
    else if(event.kind==='utterance'){
      if(this.queue.length<2){this.queue.push({audio:this.app.base64(event.bytes),life:this.life,visit:this.app.visit});this.drain();}
      else this.app.notice('Two turns are queued. Give the current reply a moment.');
    }
  }
  async receivedContinuation(event){
    if(!this.enabled||event.visit!==this.app.visit||event.voice_version!==this.serverVoiceVersion||event.sequence<=this.lastEventSequence)return;
    const generation=this.generation,life=this.life;
    while(this.enabled&&life===this.life&&generation===this.generation&&
          this.inputPending())await new Promise(resolve=>setTimeout(resolve,25));
    if(!this.enabled||life!==this.life||generation!==this.generation||event.visit!==this.app.visit)return;
    this.lastEventSequence=event.sequence;this.app.result(event);
    const status=await this.app.native('playbackStatus',event.speech_job);
    if(this.enabled&&!status.done&&!status.cancelled)this.playNative(event.speech_job);
  }
  receivedInput(event){
    if(!this.enabled||event.visit!==this.app.visit||event.voice_version!==this.serverVoiceVersion)return;
    const result=event.result||{};
    if(event.stage==='checking'){this.state(this.job?'Speaking — checking sound…':'Checking sound…');return;}
    if(event.stage==='accepted'){
      this.generation++;this.job=this.nativeJob=null;this.app.add('me',result.transcript);
      this.nativeInputBusy=true;this.app.setBusy(true);this.phase='thinking';this.state('Thinking…');return;
    }
    if(event.stage==='reply'||event.stage==='error'||event.stage==='cancelled'){
      this.nativeInputBusy=false;this.app.setBusy(false);
      if(event.stage!=='cancelled')this.app.result(result);
      if(result.speech_job)this.playNative(result.speech_job);
      else{this.phase='listening';this.state('Listening');}
    }else if(event.stage==='rejected')this.state(this.job?'Speaking':'No words detected — listening');
  }
  async interrupt(reason,keepDeliveryPreview=false,cancelServer=true){
    if(this.nativeCapture&&this.nativeInputBusy){this.nativeInputBusy=false;this.app.setBusy(false);}
    if(window.AnimaNative&&AnimaNative.stopPlayback)AnimaNative.stopPlayback();
    this.generation++;const job=this.job,count=this.completed,native=this.nativeJob===job,partial=this.players.size>0;
    this.job=null;this.completed=0;
    this.previewing=false;
    if(!keepDeliveryPreview&&this.app.stopDeliveryPreview)this.app.stopDeliveryPreview();
    if(this.app.spatial?.stopPreview)this.app.spatial.stopPreview();
    if(partial)this.suppressUntil=performance.now()+450;
    for(const node of this.players){node.onended=null;try{node.stop();}catch(_){}}this.players.clear();
    this.interruptTask=this.interruptTask.then(async()=>{
      try{if(job&&!native)await this.app.api('/api/voice/delivery',{job,completed:count,partial,reason});if(cancelServer&&this.app.visit)await this.app.api('/api/voice/interrupt',{voice_version:this.serverVoiceVersion});}catch(_){}
    });
    await this.interruptTask;
  }
  async drain(){
    if(this.draining)return;this.draining=true;
    try{
      while(this.enabled&&this.queue.length){
        while(this.app.busy&&this.enabled)await this.app.delay(80);if(!this.enabled)break;
        const item=this.queue.shift();let gen=this.generation,reply,typing,accepted=false;
        this.state(this.job?'Speaking — checking sound…':'Checking sound…');
        try{
          await this.interruptTask;
          const recognition=await this.app.api('/api/voice/recognize',{audio:item.audio});
          if(!this.enabled||item.visit!==this.app.visit||item.life!==this.life||gen!==this.generation)continue;
          if(!recognition.accepted){this.state(this.job?'Speaking':recognition.empty?'No words detected — listening':'Listening');continue;}
          // The server accepted real words and already cancelled its old job.
          // Stop local audio and report delivery without invalidating that prompt.
          const stopping=this.interrupt('accepted microphone',false,false);gen=this.generation;
          this.app.setBusy(true);accepted=true;this.phase='thinking';this.state('Thinking…');typing=this.app.typing();
          this.app.add('me',recognition.transcript);await stopping;
          if(!this.enabled||item.life!==this.life||item.visit!==this.app.visit||gen!==this.generation)continue;
          reply=await this.app.api('/api/voice/turn',{input:recognition.input});
          if(item.visit!==this.app.visit||item.life!==this.life){if(reply.speech_job&&item.visit===this.app.visit)await this.muteUnplayed(reply.speech_job);continue;}
          this.app.result(reply);
        }catch(e){if(item.visit===this.app.visit)this.app.notice(e.message);}
        finally{if(typing)typing.remove();if(accepted)this.app.setBusy(false);}
        if(reply&&reply.speech_job){
          if(gen===this.generation&&item.life===this.life&&this.enabled){if(reply.native_playback)this.playNative(reply.speech_job,item.life);else this.play(reply.speech_job,item.life);}
          else if(item.visit===this.app.visit)await this.muteUnplayed(reply.speech_job);
        }
      }
    }finally{this.draining=false;if(this.enabled&&!this.job){this.phase='listening';this.state('Listening');}}
  }
  async playNative(job,life=this.life){
    if(!this.enabled)return;const gen=this.generation;this.job=job;this.nativeJob=job;this.completed=0;
    try{
      while(this.enabled&&life===this.life&&gen===this.generation){
        const state=await this.app.native('playbackStatus',job);
        if(!this.enabled||life!==this.life||gen!==this.generation)break;
        this.completed=state.completed||0;
        if(state.started){this.phase='speaking';this.state(this.app.audioSettings?.value.mode==='speaker'?'Speaking — tap Interrupt':'Speaking — you can interrupt');}
        if(state.done||state.cancelled)break;
        await this.app.delay(150);
      }
      if(gen===this.generation&&this.job===job){this.job=null;this.nativeJob=null;this.suppressUntil=performance.now()+450;this.phase='listening';this.state('Listening');}
    }catch(e){if(life===this.life){this.app.notice(e.message);await this.interrupt('native playback error');}}
  }
  async play(job,life=this.life,initiative=false){
    if(!this.context||!this.enabled)return;const gen=this.generation;this.job=job;this.completed=0;let next=0,nextAt=0,first=true;
    try{
      while(this.enabled&&life===this.life&&gen===this.generation){
        await this.resumeOutput();
        if(!this.enabled||life!==this.life||gen!==this.generation)break;
        if(nextAt-this.context.currentTime>1.2){await this.app.delay(30);continue;}
        const status=await this.app.api('/api/voice/status',{job});if(status.cancelled)break;
        const chunk=(status.chunks||[]).find(c=>c.index===next);
        if(chunk){
          const data=await this.app.binary('/api/audio/'+job+'/'+next+'?visit='+this.app.visit);
          const buffer=await this.context.decodeAudioData(data);
          while(initiative&&first&&this.enabled&&life===this.life&&gen===this.generation&&this.inputPending())await new Promise(resolve=>setTimeout(resolve,25));
          await this.resumeOutput();
          if(!this.enabled||life!==this.life||gen!==this.generation)break;
          if(this.app.spatial)this.app.spatial.desktop(!!this.app.spatial.app||buffer.numberOfChannels===2&&!!status.spatial);
          const node=this.context.createBufferSource();node.buffer=buffer;node.connect(this.output);
          const at=Math.max(nextAt,this.context.currentTime+(first?.06:.01));
          if(first){node.disconnect();const gain=this.context.createGain();node.connect(gain);gain.connect(this.output);gain.gain.setValueAtTime(0,at);gain.gain.linearRampToValueAtTime(1,at+.005);}
          const index=next++;this.players.add(node);if(this.app.audioSettings?.value.mode==='speaker')this.capture.reset();this.phase='speaking';this.state(this.app.audioSettings?.value.mode==='speaker'?'Speaking — tap Interrupt':'Speaking — you can interrupt');
          node.onended=()=>{this.players.delete(node);this.suppressUntil=performance.now()+450;if(gen!==this.generation)return;this.completed++;this.app.api('/api/voice/ack',{job,index}).catch(()=>{});};
          node.start(at);nextAt=at+buffer.duration;first=false;continue;
        }
        if(status.error)throw Error(status.error);
        if(status.done){if(this.players.size){await this.app.delay(30);continue;}break;}
        if(!this.players.size)this.state('Preparing the voice…');await this.app.delay(65);
      }
      if(gen===this.generation&&this.job===job){await this.app.api('/api/voice/delivery',{job,completed:this.completed,partial:false,reason:'phone playback completed'});this.job=null;}
    }catch(e){if(life===this.life){this.app.notice(e.message);await this.interrupt('playback error');}}
    if(this.enabled&&life===this.life&&gen===this.generation&&!this.job&&!this.capture.active){this.phase='listening';this.state('Listening');}
  }
}
