'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const {Capture}=require('../android/assets/phone_audio.js');
function setup(mode='speaker'){
  let now=1000,stopped=0;const calls=[],nodes=new Map();
  const element=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',disabled:false,classList:{toggle(){}},querySelector(){return {textContent:''}}});return nodes.get(id);};
  const app={visit:'visit',busy:false,audioSettings:{value:{mode},meter(){},async microphone(){},route(){}},
    api:async(route,body)=>{calls.push([route,body]);return {};},native:async()=>({ok:true}),notice(){},base64:bytes=>Buffer.from(bytes).toString('base64'),
    setBusy(value){this.busy=value;},typing(){return {remove(){}};},add(){},result(){},delay:async()=>{}};
  const context={PhoneAudio:{Capture},document:{getElementById:element},performance:{now:()=>now},AnimaNative:{stopMicrophone(){stopped++;}},atob:s=>Buffer.from(s,'base64').toString('binary'),Float32Array,setInterval,clearInterval,setTimeout};
  context.window=context;
  vm.createContext(context);vm.runInContext(fs.readFileSync(require.resolve('../android/assets/phone_voice.js'),'utf8')+';globalThis.PhoneVoiceClass=PhoneVoice;',context);
  const voice=new context.PhoneVoiceClass(app);voice.enabled=true;voice.phase='listening';voice.capture=new Capture({},e=>voice.captured(e));voice.draining=true;
  const frame=(value=5000)=>{const raw=Buffer.alloc(640);for(let i=0;i<320;i++)raw.writeInt16LE(value,i*2);voice.frame(raw.toString('base64'));};
  return {voice,app,calls,frame,advance(ms){now+=ms},stopped:()=>stopped};
}
test('speaker playback cannot become a microphone turn, including its acoustic tail',()=>{
  const {voice,frame,advance}=setup();voice.phase='speaking';
  for(let i=0;i<40;i++)frame();assert.equal(voice.capture.active,false);assert.equal(voice.queue.length,0);
  voice.phase='listening';voice.suppressUntil=1450;for(let i=0;i<40;i++)frame();assert.equal(voice.capture.active,false);
  advance(451);for(let i=0;i<18;i++)frame();assert.equal(voice.capture.active,true);
  for(let i=0;i<50;i++)frame(0);assert.equal(voice.queue.length,1);
});
test('headphone sound is captured without interrupting before recognition accepts words',async()=>{
  const {voice,frame,calls}=setup('headphones');voice.phase='speaking';voice.job='job';
  for(let i=0;i<18;i++)frame();await voice.interruptTask;
  assert.equal(voice.capture.active,true);assert.equal(voice.job,'job');
  assert.equal(calls.length,0);
});

test('rejected sustained noise leaves playback and generation unchanged',async()=>{
  const s=setup('headphones');s.voice.draining=false;s.voice.phase='speaking';s.voice.job='old-job';let stopped=0;
  s.voice.players.add({stop(){stopped++;}});const gen=s.voice.generation;
  s.app.api=async(route,body)=>{s.calls.push([route,body]);assert.equal(route,'/api/voice/recognize');return {empty:true};};
  s.voice.queue.push({audio:'noise',life:s.voice.life,visit:s.app.visit});await s.voice.drain();
  assert.equal(s.voice.job,'old-job');assert.equal(s.voice.generation,gen);assert.equal(stopped,0);assert.equal(s.voice.phase,'speaking');
  assert.equal(s.app.busy,false);assert.equal(s.calls.length,1);
});

test('accepted words stop local audio before asking for a reply and preserve the server prompt',async()=>{
  const s=setup('headphones');s.voice.draining=false;s.voice.job='old-job';s.voice.phase='speaking';let stopped=0;const shown=[],played=[];
  s.voice.players.add({stop(){stopped++;}});s.app.add=(who,text)=>shown.push([who,text]);s.voice.play=async job=>played.push(job);
  s.app.api=async(route,body)=>{s.calls.push([route,body]);
    if(route==='/api/voice/recognize'){assert.equal(stopped,0);return {accepted:true,input:'proof',transcript:'Please stop.'};}
    if(route==='/api/voice/turn'){assert.equal(stopped,1);assert.equal(body.input,'proof');assert.equal(body.audio,undefined);return {reply:'Okay.',speech_job:'new-job'};}
    return {};
  };
  s.voice.queue.push({audio:'speech',life:s.voice.life,visit:s.app.visit});await s.voice.drain();
  assert.deepEqual(shown,[['me','Please stop.']]);assert.deepEqual(played,['new-job']);
  assert.ok(s.calls.some(([route])=>route==='/api/voice/delivery'));
  assert.ok(!s.calls.some(([route])=>route==='/api/voice/interrupt'));
});

test('a queued noise capture cannot suppress the accepted reply ahead of it',async()=>{
  const s=setup('headphones');s.voice.draining=false;const played=[];let checks=0;s.voice.play=async job=>{played.push(job);s.voice.job=job;};
  s.app.api=async(route,body)=>{s.calls.push([route,body]);
    if(route==='/api/voice/recognize')return ++checks===1?{accepted:true,input:'proof',transcript:'Hello'}:{empty:true};
    if(route==='/api/voice/turn')return {reply:'Hello.',speech_job:'reply'};return {};
  };
  s.voice.queue.push({audio:'speech',life:s.voice.life,visit:s.app.visit},{audio:'noise',life:s.voice.life,visit:s.app.visit});
  await s.voice.drain();assert.deepEqual(played,['reply']);assert.equal(s.voice.job,'reply');
  assert.equal(s.calls.filter(([route])=>route==='/api/voice/turn').length,1);
  assert.ok(!s.calls.some(([route])=>route==='/api/voice/interrupt'));
});

test('manual cancellation while recognition runs prevents later accepted words from starting a turn',async()=>{
  const s=setup('headphones');s.voice.draining=false;let release,entered;const ready=new Promise(resolve=>{entered=resolve;});
  s.app.api=async(route,body)=>{s.calls.push([route,body]);if(route==='/api/voice/recognize'){entered();return new Promise(resolve=>release=resolve);}return {};};
  s.voice.queue.push({audio:'speech',life:s.voice.life,visit:s.app.visit});const running=s.voice.drain();
  await ready;await s.voice.cancelReply();release({accepted:true,input:'stale',transcript:'Late words.'});await running;
  assert.ok(!s.calls.some(([route])=>route==='/api/voice/turn'));
});
test('manual interruption stops every scheduled chunk and records partial delivery',async()=>{
  const {voice,calls}=setup();let stopped=0;voice.job='job';voice.completed=2;voice.phase='speaking';voice.queue=[{}];
  voice.players.add({onended(){},stop(){stopped++;}});voice.players.add({onended(){},stop(){stopped++;}});
  const before=voice.generation;await voice.cancelReply();
  assert.equal(stopped,2);assert.equal(voice.queue.length,0);assert.equal(voice.phase,'listening');assert.equal(voice.generation,before+1);
  const delivery=calls.find(([route])=>route==='/api/voice/delivery')[1];assert.equal(delivery.completed,2);assert.equal(delivery.partial,true);
});
test('microphone errors reconnect capture rather than silently staying listening',async()=>{
  const {voice,stopped}=setup();voice.capture.active=true;
  await voice.restartMicrophone('Android microphone stopped',true);
  assert.equal(stopped(),1);assert.equal(voice.capture.active,false);assert.equal(voice.enabled,true);assert.equal(voice.recovering,false);assert.equal(voice.lastFrame,1000);
});
test('recovery is bounded when the microphone repeatedly fails',async()=>{
  const {voice,app}=setup();let message='';app.notice=text=>message=text;voice.recoveries=2;let closed=false;
  voice.stop=async()=>{closed=true;voice.enabled=false};await voice.restartMicrophone('Android microphone stopped',true);
  assert.equal(closed,true);assert.match(message,/microphone keeps stopping/);
});
test('a reply muted during generation is explicitly marked as unheard',async()=>{
  const {voice,calls}=setup();await voice.muteUnplayed('pending-job');
  const delivery=calls.find(([route])=>route==='/api/voice/delivery')[1];
  assert.equal(delivery.job,'pending-job');assert.equal(delivery.completed,0);assert.equal(delivery.partial,false);
  assert.ok(calls.some(([route])=>route==='/api/voice/interrupt'));
});

function playbackSetup(){
  const s=setup();let resumed=0,started=0;
  const context={state:'suspended',currentTime:0,
    async resume(){resumed++;this.state='running';},
    async decodeAudioData(){return {numberOfChannels:1,duration:.1};},
    createGain(){return {connect(){},gain:{setValueAtTime(){},linearRampToValueAtTime(){}}};},
    createBufferSource(){const node={connect(){},disconnect(){},stop(){},start(){assert.equal(context.state,'running');started++;}};return node;}};
  s.voice.context=context;s.voice.output={};
  s.app.binary=async()=>new ArrayBuffer(0);
  s.app.api=async(route,body)=>{s.calls.push([route,body]);return route==='/api/voice/status'?{done:true,chunks:[{index:0}]}:{};};
  s.app.delay=async()=>{context.currentTime+=.2;for(const node of [...s.voice.players])if(node.onended)node.onended();};
  return {...s,context,resumed:()=>resumed,started:()=>started};
}
test('a screen-off suspended audio engine resumes before scheduling a reply',async()=>{
  const s=playbackSetup();await s.voice.play('job');
  assert.equal(s.resumed(),1);assert.equal(s.started(),1);assert.equal(s.voice.completed,1);
  assert.ok(s.calls.some(([route])=>route==='/api/voice/ack'));
});
test('suspension while audio is being decoded is recovered before playback',async()=>{
  const s=playbackSetup();s.context.state='running';
  s.context.decodeAudioData=async()=>{s.context.state='suspended';return {numberOfChannels:1,duration:.1};};
  await s.voice.play('job');assert.equal(s.resumed(),1);assert.equal(s.started(),1);
});
test('interrupting while output resumes cannot resurrect an unheard reply',async()=>{
  const s=playbackSetup();let release;
  s.context.resume=()=>new Promise(resolve=>{release=()=>{s.context.state='running';resolve();};});
  const pending=s.voice.play('job');await s.voice.cancelReply();release();await pending;
  assert.equal(s.started(),0);assert.equal(s.voice.job,null);
  assert.ok(!s.calls.some(([route])=>route==='/api/voice/status'));
});
test('concurrent recovery shares a single audio resume request',async()=>{
  const s=playbackSetup();let calls=0,release;
  s.context.resume=()=>{calls++;return new Promise(resolve=>{release=resolve;});};
  const a=s.voice.resumeOutput(),b=s.voice.resumeOutput();assert.equal(calls,1);
  release();await Promise.all([a,b]);assert.equal(s.voice.outputResume,null);
});
test('native reply monitoring needs no audio context and sends no duplicate receipts',async()=>{
  const s=setup();let polls=0;
  s.app.native=async(method,job)=>{assert.equal(method,'playbackStatus');assert.equal(job,'native-job');return ++polls===1?{started:true,done:false,completed:0}:{started:true,done:true,completed:2};};
  s.app.delay=async()=>{};
  await s.voice.playNative('native-job');
  assert.equal(polls,2);assert.equal(s.voice.completed,2);assert.equal(s.voice.job,null);assert.equal(s.voice.phase,'listening');
  assert.ok(!s.calls.some(([route])=>route==='/api/voice/delivery'||route==='/api/voice/status'));
});
test('native interruption leaves playback receipts to the native worker',async()=>{
  const s=setup();s.voice.job='native-job';s.voice.nativeJob='native-job';
  await s.voice.cancelReply();
  assert.ok(!s.calls.some(([route])=>route==='/api/voice/delivery'));
  assert.ok(s.calls.some(([route])=>route==='/api/voice/interrupt'));
});

test('a silent continuation is shown and played without a fabricated user turn',async()=>{
  const s=setup();s.voice.draining=false;const shown=[],played=[];
  s.app.result=reply=>shown.push(reply);s.voice.play=async job=>played.push(job);
  s.app.api=async(route,body)=>{s.calls.push([route,body]);return {reply:'The story continues.',speech_job:'continuation'};};
  await s.voice.checkInitiative();
  assert.equal(shown.length,1);assert.equal(shown[0].reply,'The story continues.');assert.deepEqual(played,['continuation']);
  assert.equal(s.calls.length,1);assert.equal(s.calls[0][0],'/api/voice/initiative');
});

test('native continuation uses the native monitor instead of WebView playback',async()=>{
  const s=setup();s.voice.draining=false;let played='';
  s.app.result=()=>{};s.app.api=async()=>({reply:'Another passage.',speech_job:'native-continuation',native_playback:true});
  s.voice.play=()=>assert.fail('WebView playback must not also play native speech');
  s.voice.playNative=async job=>{played=job;};
  await s.voice.checkInitiative();assert.equal(played,'native-continuation');
});

test('blocked silence checks report state without starting a model request',async()=>{
  for(const block of [v=>v.capture.active=true,v=>v.capture.candidate=20,v=>v.queue=[{}],v=>v.job='playing',v=>v.previewing=true,v=>v.recovering=true,v=>v.lastFrame=-5000]){
    const s=setup();s.voice.draining=false;block(s.voice);await s.voice.checkInitiative();
    assert.equal(s.calls.length,1);assert.equal(s.calls[0][0],'/api/voice/initiative/state');
    assert.ok(s.calls[0][1].state.blocked.length>0);
  }
});

test('an interrupted pending continuation is discarded and cancellation targets its own job',async()=>{
  const s=setup();s.voice.draining=false;let release,entered;
  const ready=new Promise(resolve=>{entered=resolve;});
  s.app.result=()=>assert.fail('a stale continuation must not be displayed');
  s.voice.play=()=>assert.fail('a stale continuation must not play');
  s.app.api=async(route,body)=>{s.calls.push([route,body]);if(route==='/api/voice/initiative'){entered();return new Promise(resolve=>{release=resolve;});}return {};};
  const pending=s.voice.checkInitiative();await ready;await s.voice.cancelReply();
  release({reply:'Too late.',speech_job:'old-continuation'});await pending;
  const targeted=s.calls.find(([route,body])=>route==='/api/voice/interrupt'&&body.job==='old-continuation');assert.ok(targeted);
  assert.ok(s.calls.some(([route,body])=>route==='/api/voice/delivery'&&body.job==='old-continuation'&&body.completed===0));
});

test('only one silence check can be pending and choosing quiet adds no bubble',async()=>{
  const s=setup();s.voice.draining=false;let release,entered;
  const ready=new Promise(resolve=>{entered=resolve;});
  s.app.result=()=>assert.fail('waiting has no visible message');
  s.app.api=async(route,body)=>{s.calls.push([route,body]);if(route==='/api/voice/initiative/state')return {};entered();return new Promise(resolve=>{release=resolve;});};
  const pending=s.voice.checkInitiative();await ready;await s.voice.checkInitiative();
  assert.equal(s.calls.filter(([route])=>route==='/api/voice/initiative').length,1);
  assert.ok(s.calls.some(([route,body])=>route==='/api/voice/initiative/state'&&body.state.blocked.includes('checking')));
  release({quiet:true});await pending;assert.equal(s.voice.initiativeChecking,false);
});

test('unverified microphone activity cannot discard a pending autonomous reply',async()=>{
  const s=setup('headphones');s.voice.draining=false;let release,entered;const played=[];
  const ready=new Promise(resolve=>entered=resolve);s.voice.play=async job=>played.push(job);
  s.app.api=async()=>{entered();return new Promise(resolve=>release=resolve);};
  const pending=s.voice.checkInitiative();await ready;
  s.voice.captured({kind:'start'});s.voice.capture.active=true;s.voice.draining=true;s.voice.queue=[{}];
  release({reply:'Continuing the story.',speech_job:'continuation'});
  await new Promise(resolve=>setTimeout(resolve,40));assert.deepEqual(played,[]);
  // Rejected audio releases the same continuation without an interruption.
  s.voice.capture.reset();s.voice.draining=false;s.voice.queue=[];await pending;
  assert.deepEqual(played,['continuation']);assert.equal(s.voice.generation,0);
});

test('accepted microphone words supersede a continuation that arrived during capture',async()=>{
  const s=setup('headphones');s.voice.draining=false;let release,entered;
  const ready=new Promise(resolve=>entered=resolve);
  s.app.result=()=>assert.fail('stale initiative displayed');s.voice.play=()=>assert.fail('stale initiative played');
  s.app.api=async(route,body)=>{s.calls.push([route,body]);if(route==='/api/voice/initiative'){entered();return new Promise(resolve=>release=resolve);}return {};};
  const pending=s.voice.checkInitiative();await ready;s.voice.capture.active=true;
  release({reply:'Too late.',speech_job:'initiative'});
  await new Promise(resolve=>setTimeout(resolve,40));
  await s.voice.interrupt('accepted microphone',false,false);s.voice.capture.reset();await pending;
  assert.ok(s.calls.some(([route,body])=>route==='/api/voice/interrupt'&&body.job==='initiative'));
  assert.ok(!s.calls.some(([route,body])=>route==='/api/voice/interrupt'&&!body.job));
});

test('initiative decoding cannot start output over capture that began during the audio fetch',async()=>{
  const s=playbackSetup();s.voice.draining=false;let decoded;
  const ready=new Promise(resolve=>decoded=resolve);
  s.context.decodeAudioData=async()=>{s.voice.capture.active=true;decoded();return {numberOfChannels:1,duration:.1};};
  const pending=s.voice.play('initiative',s.voice.life,true);await ready;
  await new Promise(resolve=>setTimeout(resolve,40));assert.equal(s.started(),0);
  await s.voice.interrupt('accepted microphone',false,false);s.voice.capture.reset();await pending;
  assert.equal(s.started(),0);
});

test('server initiative mode never uses UI readiness to request model decisions',async()=>{
  const s=setup();s.voice.serverInitiative=true;s.voice.capture.active=true;s.voice.job='old-ui-job';
  await s.voice.checkInitiative();
  assert.equal(s.calls.length,1);assert.equal(s.calls[0][0],'/api/voice/initiative/state');
  assert.ok(!s.calls.some(([route])=>route==='/api/voice/initiative'));
});

test('native microphone mode keeps JS frames observational rather than submitting duplicate prompts',()=>{
  const s=setup('headphones');s.voice.nativeCapture=true;s.voice.phase='listening';
  for(let i=0;i<25;i++)s.frame();for(let i=0;i<55;i++)s.frame(0);
  assert.equal(s.voice.capture.active,false);assert.equal(s.voice.queue.length,0);assert.equal(s.calls.length,0);
});

test('native continuation observations are deduplicated and never start a second audio player',async()=>{
  const s=setup();s.voice.draining=false;s.voice.serverVoiceVersion=3;s.voice.lastEventSequence=0;const shown=[];
  s.app.result=event=>shown.push(event);s.app.native=async()=>({done:true,cancelled:false});
  s.voice.play=()=>assert.fail('native audio must not replay in WebView');s.voice.playNative=()=>assert.fail('completed native speech needs no monitor');
  const event={visit:s.app.visit,voice_version:3,sequence:1,reply:'A passage already played while locked.',speech_job:'job'};
  await s.voice.receivedContinuation(event);await s.voice.receivedContinuation(event);
  assert.equal(shown.length,1);assert.equal(s.voice.lastEventSequence,1);
});

test('native accepted-input observations update the view without sending microphone or interruption requests',()=>{
  const s=setup('headphones');s.voice.nativeCapture=true;s.voice.serverVoiceVersion=4;const shown=[];
  s.app.add=(who,text)=>shown.push([who,text]);s.app.result=reply=>shown.push(['her',reply.reply]);
  s.voice.receivedInput({visit:s.app.visit,voice_version:4,stage:'accepted',result:{transcript:'Keep talking.'}});
  assert.equal(s.app.busy,true);
  s.voice.receivedInput({visit:s.app.visit,voice_version:4,stage:'reply',result:{reply:'Of course.'}});
  assert.equal(s.app.busy,false);assert.equal(s.calls.length,0);assert.deepEqual(shown,[['me','Keep talking.'],['her','Of course.']]);
});
