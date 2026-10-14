'use strict';
// Audio-only helpers: no Discord login, models or filesystem access.
function wav(pcm, rate = 48000, channels = 2) {
  const h = Buffer.alloc(44);
  h.write('RIFF'); h.writeUInt32LE(36 + pcm.length, 4); h.write('WAVE', 8);
  h.write('fmt ', 12); h.writeUInt32LE(16, 16); h.writeUInt16LE(1, 20);
  h.writeUInt16LE(channels, 22); h.writeUInt32LE(rate, 24);
  h.writeUInt32LE(rate * channels * 2, 28); h.writeUInt16LE(channels * 2, 32);
  h.writeUInt16LE(16, 34); h.write('data', 36); h.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([h, pcm]);
}

function readWav(b) {
  if (b.length < 44 || b.toString('ascii', 0, 4) !== 'RIFF' || b.toString('ascii', 8, 12) !== 'WAVE')
    throw Error('Expected PCM WAV audio.');
  let fmt, pcm;
  for (let at = 12; at + 8 <= b.length;) {
    const size = b.readUInt32LE(at + 4), start = at + 8;
    if (start + size > b.length) throw Error('Truncated WAV audio.');
    const name = b.toString('ascii', at, at + 4);
    if (name === 'fmt ' && size >= 16) fmt = {
      type: b.readUInt16LE(start), channels: b.readUInt16LE(start + 2),
      rate: b.readUInt32LE(start + 4), bits: b.readUInt16LE(start + 14),
    };
    if (name === 'data') pcm = b.subarray(start, start + size);
    at = start + size + (size % 2);
  }
  if (!fmt || !pcm || fmt.type !== 1 || fmt.bits !== 16 || ![1, 2].includes(fmt.channels)
      || ![8000, 12000, 16000, 24000, 48000].includes(fmt.rate)
      || pcm.length % (fmt.channels * 2)) throw Error('Unsupported voice WAV format.');
  return { ...fmt, pcm };
}

class Timeline {
  constructor(maxSeconds = 30) {
    this.maxFrames = Math.floor(maxSeconds * 48000);
    this.base = null; this.packets = new Map(); this.frames = 0;
  }
  add(pcm, timestamp) {
    if (pcm.length % 4) throw Error('Expected stereo 48 kHz PCM.');
    timestamp >>>= 0;
    if (this.base === null) this.base = timestamp;
    const offset = (timestamp - this.base) >>> 0;
    if (offset >= this.maxFrames || this.packets.has(offset)) return false;
    const frames = Math.min(pcm.length / 4, this.maxFrames - offset);
    this.packets.set(offset, Buffer.from(pcm.subarray(0, frames * 4)));
    this.frames = Math.max(this.frames, offset + frames);
    return true;
  }
  audio() {
    const pcm = Buffer.alloc(this.frames * 4); // Missing packets and pauses retain their time.
    for (const [offset, data] of this.packets) data.copy(pcm, offset * 4);
    return wav(pcm);
  }
}

function rms(pcm) {
  let total = 0;
  for (let i = 0; i < pcm.length; i += 4) {
    const sample = (pcm.readInt16LE(i) + pcm.readInt16LE(i + 2)) / 65536;
    total += sample * sample;
  }
  return pcm.length ? Math.sqrt(total / (pcm.length / 4)) : 0;
}

class SpeechGate {
  constructor(options, emit) {
    this.options = { silenceMs: 1000, minSpeechMs: 250, interruptMs: 350,
      threshold: 0.018, maxSeconds: 30, ...options };
    this.emit = emit; this.pre = []; this.segment = null; this.lastStamp = null;
  }
  push(pcm, timestamp, now) {
    timestamp >>>= 0;
    if (this.lastStamp !== null) {
      const delta = (timestamp - this.lastStamp) >>> 0;
      if (delta === 0 || delta > 0x80000000) return; // Duplicate/late packet.
      if (delta / 48 > this.options.silenceMs) this.finish();
    }
    this.lastStamp = timestamp;
    this.tick(now);
    const duration = pcm.length / 192;
    const voiced = rms(pcm) >= this.options.threshold;
    this.pre.push({ pcm: Buffer.from(pcm), timestamp, now });
    while (this.pre.length && now - this.pre[0].now > 200) this.pre.shift();
    if (!this.segment && voiced) {
      this.segment = { audio: new Timeline(this.options.maxSeconds), voicedMs: 0,
        continuousMs: 0, lastVoice: now, interrupted: false };
      for (const p of this.pre) this.segment.audio.add(p.pcm, p.timestamp);
    }
    const s = this.segment;
    if (!s) return;
    s.audio.add(pcm, timestamp);
    if (voiced) {
      if (now - s.lastVoice > 80) s.continuousMs = 0;
      s.voicedMs += duration; s.continuousMs += duration; s.lastVoice = now;
      if (!s.interrupted && s.continuousMs >= this.options.interruptMs) {
        s.interrupted = true; this.emit({ event: 'interrupt' });
      }
    } else if (now - s.lastVoice > 80) s.continuousMs = 0;
    if (s.audio.frames >= s.audio.maxFrames) this.finish();
  }
  tick(now) {
    if (this.segment && now - this.segment.lastVoice >= this.options.silenceMs) this.finish();
  }
  finish() {
    const s = this.segment; this.segment = null; this.pre = [];
    if (s && s.voicedMs >= this.options.minSpeechMs)
      this.emit({ event: 'utterance', audio: s.audio.audio().toString('base64') });
  }
  reset() { this.segment = null; this.pre = []; this.lastStamp = null; }
}

module.exports = { wav, readWav, Timeline, SpeechGate, rms };
