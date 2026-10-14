'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { wav, readWav, Timeline, SpeechGate } = require('../engine/discord_voice_audio.cjs');
function pcm(value = 7000, frames = 960) {
  const b = Buffer.alloc(frames * 4);
  for (let at = 0; at < b.length; at += 2) b.writeInt16LE(value, at);
  return b;
}
test('RTP pauses remain silence and timestamp wrap is handled', () => {
  const line = new Timeline();
  line.add(pcm(1000), 0xfffffc40);
  line.add(pcm(2000), (0xfffffc40 + 4800) >>> 0);
  const audio = readWav(line.audio());
  assert.equal(audio.pcm.length, 5760 * 4);
  assert.equal(audio.pcm.readInt16LE(0), 1000);
  assert.equal(audio.pcm.readInt16LE(2000 * 4), 0);
  assert.equal(audio.pcm.readInt16LE(4800 * 4), 2000);
  assert.equal(line.add(pcm(3000), 0xfffffc40), false);
  assert.equal(line.add(pcm(), (0xfffffc40 - 960) >>> 0), false);
});
test('capture is bounded even with malformed or distant timestamps', () => {
  const line = new Timeline(.1);
  line.add(pcm(1000), 0); line.add(pcm(2000, 1920), 4320);
  assert.equal(line.frames, 4800);
  assert.equal(line.add(pcm(), 100000000), false);
  assert.throws(() => line.add(Buffer.alloc(3), 100));
});
test('short noise does not submit a turn or interrupt', () => {
  const events = [], gate = new SpeechGate({}, e => events.push(e));
  gate.push(pcm(), 0, 0); gate.push(pcm(0), 960, 20); gate.tick(1020);
  assert.deepEqual(events, []);
});
test('speech begins with its first packet and submits only after the pause', () => {
  const events = [], gate = new SpeechGate({}, e => events.push(e));
  for (let i = 0; i < 20; i++) gate.push(pcm(7000 + i), i * 960, i * 20);
  assert.equal(events.filter(e => e.event === 'interrupt').length, 1);
  gate.tick(1379); assert.equal(events.filter(e => e.event === 'utterance').length, 0);
  gate.tick(1380);
  const audio = readWav(Buffer.from(events.find(e => e.event === 'utterance').audio, 'base64'));
  assert.equal(audio.pcm.readInt16LE(0), 7000);
  assert.equal(audio.pcm.length, 20 * 3840);
});
test('pauses inside a phrase retain their duration rather than joining words', () => {
  const events = [], gate = new SpeechGate({}, e => events.push(e));
  for (let i = 0; i < 15; i++) gate.push(pcm(), i * 960, i * 20);
  for (let i = 30; i < 45; i++) gate.push(pcm(), i * 960, i * 20);
  gate.tick(1880);
  const audio = readWav(Buffer.from(events.find(e => e.event === 'utterance').audio, 'base64'));
  assert.equal(audio.pcm.length, 45 * 3840);
  assert.equal(audio.pcm.readInt16LE(20 * 3840), 0);
});
test('interruption requires sustained speech rather than isolated sounds', () => {
  const events = [], gate = new SpeechGate({}, e => events.push(e));
  for (let i = 0; i < 40; i++) gate.push(pcm(i % 10 < 4 ? 7000 : 0), i * 960, i * 20);
  assert.equal(events.filter(e => e.event === 'interrupt').length, 0);
});
test('a long monologue is capped and reset discards a disconnect fragment', () => {
  const events = [], gate = new SpeechGate({ maxSeconds: .4 }, e => events.push(e));
  for (let i = 0; i < 20; i++) gate.push(pcm(), i * 960, i * 20);
  assert.equal(events.filter(e => e.event === 'utterance').length, 1);
  gate.push(pcm(), 20 * 960, 400); gate.reset(); gate.tick(2000);
  assert.equal(events.filter(e => e.event === 'utterance').length, 1);
});
test('WAV parser rejects truncation and unsupported formats', () => {
  assert.throws(() => readWav(Buffer.from('bad')));
  assert.throws(() => readWav(wav(pcm()).subarray(0, 50)));
  const b = wav(pcm()); b.writeUInt16LE(3, 20);
  assert.throws(() => readWav(b));
});
