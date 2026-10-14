'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {Capture,encodeWav}=require('../android/assets/phone_audio.js');
const {normalize,threshold}=require('../android/assets/phone_settings.js');
const frame=(v=.1)=>new Float32Array(320).fill(v);
test('Bluetooth auto-selection survives saved settings and invalid inputs are bounded',()=>{
  assert.equal(normalize({input:-1}).input,-1);
  assert.equal(normalize({input:-500}).input,-1);
  assert.equal(normalize({input:19}).input,19);
  assert.equal(normalize({input:Infinity}).input,0);
});
test('native PCM is packaged as mono 16 kHz PCM16 with correct sample count',()=>{
  const bytes=encodeWav([frame(),frame(-.1)]),v=new DataView(bytes.buffer);
  assert.equal(Buffer.from(bytes).toString('ascii',0,4),'RIFF');assert.equal(v.getUint32(24,true),16000);
  assert.equal(v.getUint16(22,true),1);assert.equal(v.getUint32(40,true),1280);
  assert.equal(v.getInt16(44,true),3277);assert.equal(v.getInt16(44+640,true),-3277);
});
test('pre-roll preserves the beginning and silence closes a complete utterance',()=>{
  const events=[],c=new Capture({},e=>events.push(e));
  for(let i=0;i<4;i++)c.push(frame(0));for(let i=0;i<18;i++)c.push(frame());
  assert.equal(events.filter(e=>e.kind==='start').length,1);
  for(let i=0;i<49;i++)c.push(frame(0));assert.equal(events.filter(e=>e.kind==='utterance').length,0);
  c.push(frame(0));const data=events.find(e=>e.kind==='utterance').bytes,v=new DataView(data.buffer);
  assert.equal(v.getUint32(40,true),72*640);assert.equal(v.getInt16(44+4*640,true),3277);
});
test('a transient noise does not submit speech or interrupt playback',()=>{
  const events=[],c=new Capture({},e=>events.push(e));
  for(let i=0;i<7;i++)c.push(frame(),true);for(let i=0;i<60;i++)c.push(frame(0),true);
  assert.deepEqual(events,[]);
});
test('sustained speech interrupts after the configured confirmation',()=>{
  const events=[],c=new Capture({},e=>events.push(e));for(let i=0;i<17;i++)c.push(frame(),true);
  assert.equal(events.length,0);c.push(frame(),true);assert.equal(events[0].kind,'start');
});
test('max capture duration is bounded and reset discards a stopped microphone',()=>{
  const events=[],c=new Capture({max_seconds:1},e=>events.push(e));for(let i=0;i<50;i++)c.push(frame());
  assert.equal(events.filter(e=>e.kind==='utterance').length,1);c.push(frame());c.reset();
  for(let i=0;i<60;i++)c.push(frame(0));assert.equal(events.filter(e=>e.kind==='utterance').length,1);
});
test('natural short consonant gaps still open and submit a complete phrase',()=>{
  const events=[],c=new Capture({},e=>events.push(e));
  for(let word=0;word<3;word++){for(let i=0;i<5;i++)c.push(frame());for(let i=0;i<3;i++)c.push(frame(0));}
  for(let i=0;i<50;i++)c.push(frame(0));
  assert.equal(events.filter(e=>e.kind==='start').length,1);
  assert.equal(events.filter(e=>e.kind==='utterance').length,1);
  const data=events.find(e=>e.kind==='utterance').bytes;assert.equal(new DataView(data.buffer).getInt16(44,true),3277);
});
test('separate short noises with longer gaps do not accumulate into speech',()=>{
  const events=[],c=new Capture({},e=>events.push(e));
  for(let i=0;i<10;i++){for(let j=0;j<5;j++)c.push(frame());for(let j=0;j<10;j++)c.push(frame(0));}
  assert.deepEqual(events,[]);
});
test('default phone sensitivity captures quieter speech and controls are bounded',()=>{
  const s=normalize({}),events=[],c=new Capture({threshold:threshold(s.sensitivity)},e=>events.push(e));
  for(let i=0;i<20;i++)c.push(frame(.012));for(let i=0;i<50;i++)c.push(frame(0));
  assert.equal(events.filter(e=>e.kind==='utterance').length,1);
  assert.equal(s.mode,'speaker');assert.equal(s.noise,false);assert.equal(s.echo,true);
  assert.ok(threshold(100)<threshold(0));assert.equal(normalize({volume:400,silence:-1,sensitivity:NaN}).volume,100);
  assert.equal(normalize({silence:-1}).silence,500);
});
test('phone short answers can finish without requiring a quarter-second vowel',()=>{
  const events=[],c=new Capture({min_speech_ms:160,threshold:threshold(60)},e=>events.push(e));
  for(let i=0;i<9;i++)c.push(frame(.012));for(let i=0;i<50;i++)c.push(frame(0));
  assert.equal(events.filter(e=>e.kind==='utterance').length,1);
});
