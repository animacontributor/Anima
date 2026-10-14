/* Client-only binaural placement. The voice model and its audio remain unchanged. */
((root)=>{
  const defaults={enabled:false,profile:'ku100',azimuth:0,elevation:0,distance:1,room:.06,head_radius:.09};
  const number=(v,fallback,min,max)=>Number.isFinite(Number(v))?Math.max(min,Math.min(max,Number(v))):fallback;
  function clean(s={}){return {enabled:s.enabled===true,profile:s.profile==='sadie'?'sadie':'ku100',azimuth:number(s.azimuth,0,-180,180),elevation:number(s.elevation,0,-60,60),distance:number(s.distance,1,.11,3),room:number(s.room,.06,0,.2),head_radius:number(s.head_radius,.09,.075,.095)};}
  function position(s){s=clean(s);const a=s.azimuth*Math.PI/180,e=s.elevation*Math.PI/180;return {x:Math.sin(a)*Math.cos(e)*s.distance,y:Math.sin(e)*s.distance,z:-Math.cos(a)*Math.cos(e)*s.distance};}
  function route(context,input,s){
    if(!s.enabled){input.connect(context.destination);return null;}
    const p=context.createPanner();p.panningModel='HRTF';p.distanceModel='inverse';p.refDistance=1;p.maxDistance=100;p.rolloffFactor=.15;
    p.coneInnerAngle=360;p.coneOuterAngle=360;p.channelCount=1;p.channelCountMode='explicit';
    const point=position(s);p.setPosition(point.x,point.y,point.z);context.listener.setPosition(0,0,0);context.listener.setOrientation(0,0,-1,0,1,0);
    input.connect(p);p.connect(context.destination);return p;
  }
  class SpatialVoice {
    constructor(getVoice,notice,app=null){
      this.getVoice=getVoice;this.notice=notice;this.app=app;this.context=null;this.input=null;this.panner=null;this.desktopRendered=!!app;this.previewSource=null;
      try{this.state=clean(JSON.parse(localStorage.getItem('voice-position')||'{}'));}catch(_){this.state={...defaults};}
      this.wire();this.draw();
    }
    attach(context,input){this.context=context;this.input=input;this.apply(true);}
    desktop(enabled){if(this.desktopRendered!==enabled){this.desktopRendered=enabled;this.apply(true);}}
    async sync(){
      if(!this.app)return;
      const label=document.getElementById('positionStatus');label.textContent='Preparing the position…';
      try{const result=await this.app.api('/api/spatial/configure',{settings:this.state});this.applied={...this.state};label.textContent=this.state.enabled?'Applied · '+result.nearest_ear_cm+' cm from the nearest ear · next reply':'Normal voice';return result;}
      catch(e){label.textContent=e.message;throw e;}
    }
    apply(reconnect=false){
      const context=this.context;if(!context||context.state==='closed')return;
      try{
        const local=this.state.enabled&&!this.desktopRendered;
        if(reconnect||Boolean(this.panner)!==local){
          this.input.disconnect();if(this.panner)this.panner.disconnect();this.panner=route(context,this.input,{...this.state,enabled:local});
        }else if(this.panner){
          const p=position(this.state),now=context.currentTime;
          for(const axis of ['X','Y','Z'])this.panner['position'+axis].setTargetAtTime(p[axis.toLowerCase()],now,.025);
        }
      }catch(e){this.state.enabled=false;this.input.disconnect();this.input.connect(context.destination);this.panner=null;this.notice('3D positioning is unavailable in this audio engine. Regular voice is ready.');}
    }
    save(){this.state=clean(this.state);localStorage.setItem('voice-position',JSON.stringify(this.state));this.apply();this.draw();if(this.app)document.getElementById('positionStatus').textContent='Not applied — tap Apply position.';}
    open(){this.draw();const d=document.getElementById('spatialDialog');if(!d.open)d.showModal();}
    wire(){
      const $=id=>document.getElementById(id),canvas=$('soundSpace');
      $('positionBefore').onclick=()=>this.open();$('positionDuring').onclick=()=>this.open();$('closePosition').onclick=()=>{this.stopPreview();$('spatialDialog').close();};
      $('spatialEnabled').onchange=()=>{this.state.enabled=$('spatialEnabled').checked;this.save();};
      $('soundDistance').oninput=()=>{this.state.distance=Number($('soundDistance').value);this.save();};
      $('soundHeight').oninput=()=>{this.state.elevation=Number($('soundHeight').value);this.save();};
      $('roomAmount').oninput=()=>{this.state.room=Number($('roomAmount').value);this.save();};
      $('headRadius').oninput=()=>{this.state.head_radius=Number($('headRadius').value);this.save();};
      $('earProfile').onchange=()=>{this.state.profile=$('earProfile').value;this.save();};
      $('applyPosition').onclick=()=>this.sync().catch(e=>this.notice(e.message));
      document.querySelectorAll('[data-position]').forEach(b=>b.onclick=()=>{
        this.state.enabled=true;this.state.elevation=0;
        const presets={left:[-90,this.state.head_radius+.02],front:[0,.4],right:[90,this.state.head_radius+.02],back:[180,.5]};
        [this.state.azimuth,this.state.distance]=presets[b.dataset.position];this.save();
      });
      const move=event=>{
        const r=canvas.getBoundingClientRect(),x=(event.clientX-r.left)*320/r.width-160,y=(event.clientY-r.top)*300/r.height-150;
        this.state.enabled=true;this.state.azimuth=Math.atan2(x,-y)*180/Math.PI;
        const radius=Math.max(32,Math.min(116,Math.hypot(x,y)));
        this.state.distance=.11*Math.pow(3/.11,(radius-32)/84);this.save();
      };
      canvas.onpointerdown=e=>{canvas.setPointerCapture(e.pointerId);move(e);};
      canvas.onpointermove=e=>{if(canvas.hasPointerCapture(e.pointerId))move(e);};
      canvas.onpointerup=e=>{if(canvas.hasPointerCapture(e.pointerId))canvas.releasePointerCapture(e.pointerId);};
      canvas.onkeydown=e=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))return;e.preventDefault();this.state.enabled=true;
        if(e.key==='ArrowLeft')this.state.azimuth-=5;if(e.key==='ArrowRight')this.state.azimuth+=5;
        if(e.key==='ArrowUp')this.state.distance-=.1;if(e.key==='ArrowDown')this.state.distance+=.1;this.save();};
      $('previewPosition').onclick=()=>this.preview();
    }
    draw(){
      const $=id=>document.getElementById(id),s=this.state;
      $('spatialEnabled').checked=s.enabled;$('soundDistance').value=s.distance;$('soundHeight').value=s.elevation;
      $('roomAmount').value=s.room;$('headRadius').value=s.head_radius;$('roomLabel').textContent=s.room===0?'Dry':Math.round(s.room*100)+'%';$('headLabel').textContent=Math.round(s.head_radius*200)+' cm wide';
      $('earProfile').value=s.profile;
      $('distanceLabel').textContent=s.distance<.5?Math.round(s.distance*100)+' cm':s.distance.toFixed(1)+' m';
      $('heightLabel').textContent=s.elevation===0?'Level':Math.abs(s.elevation)+'° '+(s.elevation>0?'above':'below');
      const direction=Math.abs(s.azimuth)<35?'In front':Math.abs(s.azimuth)>145?'Behind':s.azimuth<0?'Left':'Right';
      $('positionSummary').textContent=s.enabled?direction:'Normal';$('positionDuring').classList.toggle('position-on',s.enabled);
      const c=$('soundSpace').getContext('2d');c.clearRect(0,0,320,300);c.save();c.translate(160,150);
      c.strokeStyle='#554633';c.lineWidth=1;for(const r of [55,85,116]){c.beginPath();c.arc(0,0,r,0,Math.PI*2);c.stroke();}
      c.strokeStyle='#42382c';for(const rot of [-.5,.5]){c.save();c.rotate(rot);c.beginPath();c.ellipse(0,0,116,38,0,0,Math.PI*2);c.stroke();c.restore();}
      c.setLineDash([3,7]);c.beginPath();c.moveTo(-116,0);c.lineTo(116,0);c.moveTo(0,-116);c.lineTo(0,116);c.stroke();c.setLineDash([]);
      c.fillStyle='#e9d7b4';c.beginPath();c.arc(0,0,21,0,Math.PI*2);c.fill();c.beginPath();c.moveTo(-5,-19);c.lineTo(0,-28);c.lineTo(5,-19);c.fill();
      c.fillStyle='#a99b85';c.font='11px system-ui';c.textAlign='center';c.fillText('IN FRONT',0,-134);c.fillText('BEHIND',0,140);c.fillText('YOU',0,40);
      c.textAlign='left';c.fillText('L',-150,4);c.textAlign='right';c.fillText('R',150,4);
      const a=s.azimuth*Math.PI/180,r=32+84*Math.log(s.distance/.11)/Math.log(3/.11),x=Math.sin(a)*r,y=-Math.cos(a)*r;
      const glow=c.createRadialGradient(x,y,0,x,y,28);glow.addColorStop(0,'#e4bd8877');glow.addColorStop(1,'#e4bd8800');c.fillStyle=glow;c.beginPath();c.arc(x,y,28,0,Math.PI*2);c.fill();
      c.fillStyle=s.enabled?'#e8bd80':'#8a7b65';c.beginPath();c.arc(x,y,8,0,Math.PI*2);c.fill();c.restore();
    }
    async preview(){
      if(this.app){return this.desktopPreview();}
      let own=false,context;
      try{
        const voice=this.getVoice();context=voice.context&&voice.context.state!=='closed'?voice.context:new(window.AudioContext||window.webkitAudioContext)();own=context!==voice.context;
        await context.resume();const output=own?context.createGain():voice.output;if(own){output.gain.value=1;route(context,output,this.state);}
        const length=Math.round(context.sampleRate*.25),buffer=context.createBuffer(1,length,context.sampleRate),data=buffer.getChannelData(0);
        for(let i=0;i<length;i++){const t=i/context.sampleRate,envelope=Math.sin(Math.PI*i/length)**2;
          data[i]=.07*envelope*(Math.sin(2*Math.PI*440*t)+.45*Math.sin(2*Math.PI*1320*t)+.2*Math.sin(2*Math.PI*2640*t));}
        const source=context.createBufferSource();source.buffer=buffer;source.connect(output);
        source.onended=()=>{if(own)context.close();};source.start();
      }catch(e){if(own&&context)context.close();this.notice('The position preview could not play: '+e.message);}
    }
    async desktopPreview(){
      const $=id=>document.getElementById(id);
      if(this.previewSource){try{this.previewSource.stop();}catch(_){}this.previewSource=null;return;}
      const voice=this.getVoice();let context,own=false;
      try{
        if(this.app.busy){this.notice('Wait for the current reply before previewing.');return;}
        context=voice.context&&voice.context.state!=='closed'?voice.context:new(window.AudioContext||window.webkitAudioContext)();own=context!==voice.context;await context.resume();
        await voice.interrupt('position preview');const generation=voice.generation;voice.previewing=true;voice.capture?.reset();$('previewPosition').disabled=true;
        await this.sync();const result=await this.app.api('/api/spatial/preview',{});
        const raw=atob(result.audio),bytes=Uint8Array.from(raw,c=>c.charCodeAt(0));const buffer=await context.decodeAudioData(bytes.buffer);
        if(generation!==voice.generation){voice.previewing=false;if(own)context.close();return;}
        const gain=context.createGain();gain.gain.value=.7*(this.app.audioSettings?.value.volume??100)/100;gain.connect(context.destination);
        const source=context.createBufferSource();source.buffer=buffer;source.connect(gain);this.previewSource=source;
        if(voice.enabled){voice.players.add(source);voice.phase='speaking';voice.state('Position preview — tap Interrupt');}
        $('previewPosition').textContent='Stop preview';
        this.previewCleanup=()=>{this.previewSource=null;this.previewCleanup=null;voice.players.delete(source);voice.previewing=false;voice.suppressUntil=performance.now()+450;$('previewPosition').textContent='Listen to voice preview';gain.disconnect();if(own&&context.state!=='closed')context.close();if(voice.enabled){voice.phase='listening';voice.state('Listening in a moment…');}};
        source.onended=()=>{if(this.previewCleanup)this.previewCleanup();};
        source.start();
      }catch(e){voice.previewing=false;if(own&&context)context.close();this.notice(e.message);}finally{$('previewPosition').disabled=false;}
    }
    stopPreview(){if(this.previewSource){this.previewSource.onended=null;try{this.previewSource.stop();}catch(_){}if(this.previewCleanup)this.previewCleanup();}}
  }
  const api={clean,position,route,SpatialVoice};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else{root.SpatialVoice=SpatialVoice;root.PhoneSpatial=api;}
})(typeof window!=='undefined'?window:globalThis);
