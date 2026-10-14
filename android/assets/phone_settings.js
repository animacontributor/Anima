/* Phone-local capture preferences and per-phone Kyutai delivery settings. */
((root)=>{
  const defaults={mode:'speaker',sensitivity:60,silence:1000,volume:100,input:0,echo:true,noise:false,speed:90,whisper:0,pause:180,guidance:2};
  function normalize(value={}){
    const number=(key,min,max)=>Math.max(min,Math.min(max,Number.isFinite(Number(value[key]))?Number(value[key]):defaults[key]));
    return {mode:value.mode==='headphones'?'headphones':'speaker',sensitivity:number('sensitivity',0,100),silence:number('silence',500,2000),volume:number('volume',0,100),input:Math.floor(number('input',-1,2147483647)),echo:value.echo!==false,noise:value.noise===true,speed:number('speed',75,110),whisper:number('whisper',0,100),pause:number('pause',0,600),guidance:Math.round(number('guidance',1,4)*2)/2};
  }
  function threshold(sensitivity){return .004*Math.pow(8,(100-sensitivity)/100);}
  class Settings {
    constructor(app){
      this.app=app;let saved={};try{saved=JSON.parse(localStorage.getItem('phoneAudio')||'{}');}catch(_){}
      this.value=normalize(saved);this.dialog=document.getElementById('audioDialog');
      this.dialog.addEventListener('close',()=>this.app.stopDeliveryPreview?.());
      for(const id of ['audioBefore','audioDuring'])document.getElementById(id).onclick=()=>this.open();
      document.getElementById('closeAudio').onclick=()=>this.dialog.close();
      this.dialog.addEventListener('click',e=>{if(e.target===this.dialog)this.dialog.close();});
      for(const key of ['mode','sensitivity','silence','volume','input','echo','noise','speed','whisper','pause','guidance']){
        const el=document.getElementById('audio_'+key);el.value=this.value[key];if(el.type==='checkbox')el.checked=this.value[key];
        el.onchange=()=>this.change(key,el.type==='checkbox'?el.checked:el.value).catch(e=>this.app.notice(e.message));
        if(el.type==='range')el.oninput=el.onchange;
      }
      document.getElementById('refreshMicrophones').onclick=()=>this.devices();document.getElementById('previewDelivery').onclick=()=>this.app.previewDelivery();this.render();
    }
    async change(key,value){
      this.value=normalize({...this.value,[key]:value});localStorage.setItem('phoneAudio',JSON.stringify(this.value));this.render();
      if(key==='input' && (this.value.input===-1 || this.inputs?.some(device=>device.id===this.value.input && device.bluetooth))){this.value.mode='headphones';document.getElementById('audio_mode').value='headphones';localStorage.setItem('phoneAudio',JSON.stringify(this.value));this.render();}
      const voice=this.app.voice;if(voice.capture){voice.capture.s.threshold=threshold(this.value.sensitivity);voice.capture.s.silence_ms=this.value.silence;}
      if(voice.output)voice.output.gain.value=this.value.volume/100;
      if(['volume','mode','sensitivity','silence'].includes(key)&&voice.enabled)await this.microphone();
      if(key==='mode'){voice.capture?.reset();voice.suppressUntil=0;}
      if(['input','echo','noise'].includes(key)&&voice.enabled)await voice.restartMicrophone('Applying microphone settings');
      if(['speed','whisper','pause','guidance'].includes(key)&&this.app.visit){clearTimeout(this.deliveryTimer);this.deliveryTimer=setTimeout(()=>this.syncDelivery().catch(e=>this.app.notice(e.message)),300);}
    }
    capture(settings){return {...settings,min_speech_ms:160,threshold:threshold(this.value.sensitivity),silence_ms:this.value.silence};}
    async microphone(){return this.app.native('configureAudio',JSON.stringify(this.value));}
    delivery(){return {speed:this.value.speed/100,whisper_mix:this.value.whisper/100,pause_ms:this.value.pause,guidance:this.value.guidance};}
    async syncDelivery(){await this.app.api('/api/delivery/configure',{settings:this.delivery()});document.getElementById('deliveryStatus').textContent='Applied to subsequent replies.';}
    async devices(){
      try{
        const info=await this.app.native('audioDevices'),select=document.getElementById('audio_input');select.replaceChildren();
        this.inputs=info.inputs||[];
        for(const device of [{id:0,label:'Android default'},...(info.inputs||[])]){const option=document.createElement('option');option.value=device.id;option.textContent=device.label;select.appendChild(option);}
        select.value=String(this.value.input);if(!select.value){const option=document.createElement('option');option.value=this.value.input;option.textContent='Selected microphone is disconnected; select it again';select.appendChild(option);select.value=String(this.value.input);}
        this.route(info);
      }catch(e){document.getElementById('audioRoute').textContent=e.message;}
    }
    route(info){
      const status=info.connecting?'Connecting microphone. ':info.route?(info.verified?'Verified microphone: ':'Recording from: ')+info.route+'. ':'Start voice to verify the selected microphone. ';
      const bluetooth=info.bluetoothMode?info.bluetoothMode+'. '+(info.bluetoothMode==='LE Audio'?'Playback quality depends on your phone and headset. ':'Hands-free mode can reduce playback quality. '):'';
      document.getElementById('audioRoute').textContent=status+bluetooth+(info.requestedMatched===false?'Selected microphone is not active. ':'')+'Echo cancellation: '+(info.echoEnabled?'active':info.echoAvailable?this.app.voice.enabled?'off':'available when recording':'not provided by this phone')+'.';document.getElementById('audio_echo').disabled=info.echoAvailable===false;document.getElementById('audio_noise').disabled=info.noiseAvailable===false;
    }
    render(){
      document.getElementById('audio_sensitivityLabel').textContent=Math.round(this.value.sensitivity)+'%';
      document.getElementById('audio_silenceLabel').textContent=(this.value.silence/1000).toFixed(1)+' s';
      document.getElementById('audio_volumeLabel').textContent=Math.round(this.value.volume)+'%';
      document.getElementById('audio_speedLabel').textContent=Math.round(this.value.speed)+'%';
      document.getElementById('audio_whisperLabel').textContent=this.value.whisper===0?'Soft speech':this.value.whisper===100?'Whisper':Math.round(this.value.whisper)+'% blend';
      document.getElementById('audio_pauseLabel').textContent=Math.round(this.value.pause)+' ms';
      document.getElementById('audio_guidanceLabel').textContent=this.value.guidance.toFixed(1);
      document.getElementById('audioSummary').textContent=this.value.mode==='speaker'?'Speaker':'Headphones';
      document.getElementById('audioModeHint').textContent=this.value.mode==='speaker'?'Listening pauses while she speaks. Tap Interrupt to stop her, then speak.':'Listening stays on during replies. Accepted words interrupt after recognition; use this with headphones.';
    }
    open(){this.dialog.showModal();this.devices();}
    meter(level,blocked){
      const value=Math.min(100,Math.sqrt(Math.max(0,level))*180);
      for(const id of ['micMeter','audioMeter']){const meter=document.getElementById(id);meter.value=blocked?0:value;meter.title=blocked?'Microphone paused during speaker playback':'Microphone input';}
      document.getElementById('audioMeterHint').textContent=blocked?'Paused during speaker playback':this.app.voice.enabled?'Speak normally. Raise sensitivity if quiet words are missed.':'Start voice to check the microphone.';
    }
  }
  const api={defaults,normalize,threshold};if(typeof module!=='undefined'&&module.exports)module.exports=api;else {root.PhoneSettings=api;root.AudioSettings=Settings;}
})(typeof window!=='undefined'?window:globalThis);
