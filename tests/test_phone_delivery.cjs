'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const DeliveryPreview=require('../android/assets/phone_delivery.js');
function setup(){
  const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,{textContent:''});return elements.get(id);};
  global.document={getElementById:get};let started=0,stopped=0,closed=0;
  class Context{
    constructor(){this.state='running';this.destination={};}
    async resume(){} async close(){closed++;this.state='closed';}
    async decodeAudioData(){return {numberOfChannels:2,duration:2};}
    createGain(){return {gain:{value:1},connect(){},disconnect(){}};}
    createBufferSource(){return {connect(){},start(){started++;},stop(){stopped++;}};}
  }
  global.AudioContext=Context;
  const voice={context:null,enabled:true,starting:false,stopping:false,generation:0,players:new Set(),capture:{reset(){}},state(){},
    async interrupt(reason,keep){this.generation++;if(!keep)preview.stop();}};
  let controls;const app={voice,busy:false,audioSettings:{value:{volume:80},async syncDelivery(){},delivery(){return {speed:.85,whisper_mix:.35,pause_ms:220,guidance:2};}},
    notice(){},async api(route,data){assert.equal(route,'/api/delivery/preview');controls=data.settings;return {audio:Buffer.from('fake audio').toString('base64')};}};
  const preview=new DeliveryPreview(app);
  return {preview,app,voice,get,started:()=>started,stopped:()=>stopped,closed:()=>closed,controls:()=>controls};
}
test('preview uses delivery preferences, gates microphone, and cleans up on interruption',async()=>{
  const s=setup();await s.preview.play();assert.equal(s.started(),1);assert.equal(s.voice.previewing,true);assert.equal(s.voice.players.size,1);
  assert.equal(s.controls().whisper_mix,.35);assert.equal(s.preview.active.source.buffer.numberOfChannels,2);
  s.preview.stop();assert.equal(s.stopped(),1);assert.equal(s.voice.players.size,0);assert.equal(s.voice.previewing,false);assert.equal(s.closed(),1);
});
test('a stopped pending preview cannot play after generation finishes',async()=>{
  const s=setup();let release,entered;
  const waiting=new Promise(resolve=>entered=resolve);
  s.app.api=()=>{entered();return new Promise(resolve=>release=resolve);};
  const pending=s.preview.play();await waiting;s.preview.stop();release({audio:Buffer.from('unused audio').toString('base64')});await pending;
  assert.equal(s.started(),0);assert.equal(s.voice.previewing,false);assert.equal(s.closed(),1);
});
test('closing a preview while audio context resumes cannot resurrect playback',async()=>{
  const s=setup();let release;global.AudioContext.prototype.resume=()=>new Promise(resolve=>release=resolve);
  const pending=s.preview.play();s.preview.stop();release();await pending;assert.equal(s.started(),0);assert.equal(s.preview.active,null);
});
