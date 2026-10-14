/* Audition delivery without a conversation turn; stale requests never play. */
((root)=>{
  class DeliveryPreview {
    constructor(app){this.app=app;this.active=null;}
    stop(){
      const run=this.active;if(!run)return;this.active=null;
      if(run.source){run.source.onended=null;try{run.source.stop();}catch(_){}this.app.voice.players.delete(run.source);}
      if(run.gain)run.gain.disconnect();
      if(run.own&&run.context&&run.context.state!=='closed')run.context.close();
      const voice=this.app.voice;voice.previewing=false;voice.capture?.reset();voice.suppressUntil=performance.now()+450;
      if(voice.enabled){voice.phase='listening';voice.state('Listening in a moment…');}
      document.getElementById('previewDelivery').textContent='Preview delivery';
    }
    async play(){
      if(this.active){this.stop();return;}
      const app=this.app,voice=app.voice,$=id=>document.getElementById(id);
      if(app.busy||voice.starting||voice.stopping){app.notice('Wait for the current reply before previewing.');return;}
      const run={context:voice.context&&voice.context.state!=='closed'?voice.context:new(root.AudioContext||root.webkitAudioContext)()};
      run.own=run.context!==voice.context;this.active=run;
      const current=()=>this.active===run;
      try{
        await run.context.resume();
        if(!current())return;
        await voice.interrupt('delivery preview',true);
        if(!current())return;
        const generation=voice.generation;voice.previewing=true;voice.capture?.reset();
        $('previewDelivery').textContent='Stop preview';$('deliveryStatus').textContent='Creating the preview…';
        await app.audioSettings.syncDelivery();
        if(!current())return;
        const result=await app.api('/api/delivery/preview',{settings:app.audioSettings.delivery()});
        if(!current()||generation!==voice.generation)return;
        const bytes=Uint8Array.from(atob(result.audio),c=>c.charCodeAt(0)),buffer=await run.context.decodeAudioData(bytes.buffer);
        if(!current()||generation!==voice.generation)return;
        run.gain=run.context.createGain();run.gain.gain.value=.7*app.audioSettings.value.volume/100;run.gain.connect(run.context.destination);
        run.source=run.context.createBufferSource();run.source.buffer=buffer;run.source.connect(run.gain);
        if(voice.enabled){voice.players.add(run.source);voice.phase='speaking';voice.state('Voice preview — tap Interrupt');}
        run.source.onended=()=>{if(current())this.stop();};run.source.start();
        $('deliveryStatus').textContent='Preview only — no message or memory is created.';
      }catch(e){if(current()){app.notice(e.message);$('deliveryStatus').textContent=e.message;}}
      finally{
        if(current()&&!run.source)this.stop();
        else if(!current()&&run.own&&run.context.state!=='closed')run.context.close();
      }
    }
  }
  if(typeof module!=='undefined'&&module.exports)module.exports=DeliveryPreview;else root.DeliveryPreview=DeliveryPreview;
})(typeof window!=='undefined'?window:globalThis);
