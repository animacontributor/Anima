/* Pure microphone boundaries and PCM/WAV packaging, shared with offline tests. */
((root) => {
  function encodeWav(blocks) {
    const count = blocks.reduce((n, b) => n + b.length, 0);
    const bytes = new Uint8Array(44 + count * 2), view = new DataView(bytes.buffer);
    function text(at, s) { for (let i=0;i<s.length;i++) bytes[at+i]=s.charCodeAt(i); }
    text(0,'RIFF');view.setUint32(4,36+count*2,true);text(8,'WAVE');text(12,'fmt ');
    view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);
    view.setUint32(24,16000,true);view.setUint32(28,32000,true);view.setUint16(32,2,true);view.setUint16(34,16,true);
    text(36,'data');view.setUint32(40,count*2,true);let at=44;
    for(const block of blocks)for(const sample of block){view.setInt16(at,Math.round(Math.max(-1,Math.min(1,sample))*(sample<0?32768:32767)),true);at+=2;}
    return bytes;
  }
  class Capture {
    constructor(settings, emit) { this.s={silence_ms:1000,min_speech_ms:250,interrupt_ms:350,threshold:.018,max_seconds:30,...settings};this.emit=emit;this.noise=.002;this.reset(); }
    reset(){this.pre=[];this.preSize=0;this.blocks=[];this.size=0;this.candidate=0;this.candidateGap=0;this.candidateAge=0;this.voiced=0;this.silence=0;this.active=false;}
    push(pcm,speaking=false){
      let energy=0;for(const v of pcm)energy+=v*v;const level=Math.sqrt(energy/pcm.length),ms=pcm.length/16;
      if(!this.active&&!speaking&&level<this.s.threshold)this.noise=this.noise*.98+level*.02;
      const voiced=level>Math.max(this.s.threshold,this.noise*3);
      if(!this.active){
        this.pre.push(pcm);this.preSize+=pcm.length;
        const lead=Math.max(1000,Math.max(this.s.min_speech_ms,this.s.interrupt_ms)+300)*16;
        while(this.preSize>lead&&this.pre.length>1)this.preSize-=this.pre.shift().length;
        // Natural words contain quiet consonants and brief gaps. Count voiced time
        // across these gaps instead of requiring one uninterrupted loud vowel.
        if(this.candidate)this.candidateAge+=ms;
        if(this.candidateAge>900){this.candidate=0;this.candidateAge=0;}
        if(voiced){this.candidate+=ms;this.candidateGap=0;}
        else {this.candidateGap+=ms;if(this.candidateGap>140){this.candidate=0;this.candidateAge=0;}}
        const confirm=Math.max(this.s.min_speech_ms,speaking?this.s.interrupt_ms:this.s.min_speech_ms);
        if(!voiced||this.candidate<confirm)return;
        this.active=true;this.blocks=this.pre.slice();this.size=this.preSize;this.voiced=this.candidate;this.silence=0;
        this.emit({kind:'start'});return;
      }
      this.blocks.push(pcm);this.size+=pcm.length;
      if(voiced){this.voiced+=ms;this.silence=0;}else this.silence+=ms;
      if(this.silence>=this.s.silence_ms||this.size>=this.s.max_seconds*16000){
        if(this.voiced>=this.s.min_speech_ms)this.emit({kind:'utterance',bytes:encodeWav(this.blocks)});
        this.reset();
      }
    }
  }
  const api={Capture,encodeWav};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.PhoneAudio=api;
})(typeof window!=='undefined'?window:globalThis);
