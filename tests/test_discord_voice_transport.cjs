'use strict';
// Run the real helper in a VM with fake Discord networking and a real Opus codec.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const { createRequire } = require('node:module');
const { wav, readWav } = require('../engine/discord_voice_audio.cjs');
const defaultDeps = path.resolve(__dirname, '../.runtime/discord-voice');
const depsPath = process.env.ANIMA_DISCORD_VOICE_DEPS || (fs.existsSync(path.join(defaultDeps, 'node_modules')) ? defaultDeps : null);
const deps = depsPath ? createRequire(path.join(depsPath, 'package.json')) : null;

function harness(options = {}) {
  const messages = [], lines = new EventEmitter(), udp = new EventEmitter(), stream = new PassThrough({ objectMode: true });
  const owner = '100000000000000001', bot = '100000000000000002', channelId = '100000000000000003';
  let clock = 0, tick, sdkClient, player;
  const channel = { id: channelId, type: 2, guild: { id: '100000000000000004', voiceAdapterCreator: {} },
    members: new Map(options.members || [[owner, { id: owner }]]),
    permissionsFor: () => ({ has: () => options.permissions !== false }) };
  class Client extends EventEmitter {
    constructor() { super(); sdkClient = this; this.user = { id: bot }; this.channels = { fetch: async () => channel }; }
    async login() { setImmediate(() => this.emit('clientReady')); }
    destroy() { this.destroyed = true; }
  }
  const receiver = { ssrcMap: new Map([[123, { userId: owner }]]),
    subscribe: id => { assert.equal(id, owner); return stream; } };
  udp.on('message', packet => {
    if (receiver.ssrcMap.get(packet.readUInt32BE(8))?.userId === owner)
      stream.push(packet.subarray(12)); // The pinned receiver pushes into its Readable directly.
  });
  const conn = new EventEmitter();
  conn.receiver = receiver;
  conn.state = { status: 'ready', networking: { state: { udp } } };
  conn.subscribe = () => {};
  conn.destroy = () => { conn.state.status = 'destroyed'; };
  const sdkVoice = {
    joinVoiceChannel: options => { assert.equal(options.selfDeaf, false); assert.equal(options.daveEncryption, true); return conn; },
    entersState: async () => conn, VoiceConnectionStatus: { Ready: 'ready', Destroyed: 'destroyed', Disconnected: 'disconnected' },
    AudioPlayerStatus: { Idle: 'idle' }, NoSubscriberBehavior: { Pause: 'pause' }, StreamType: { Opus: 'opus' },
    EndBehaviorType: { Manual: 'manual' }, createAudioResource: input => ({ input }),
    createAudioPlayer: () => {
      player = new EventEmitter(); player.packets = [];
      player.play = resource => {
        player.resource = resource;
        if (!options.pausePlayback) resource.input.on('data', b => player.packets.push(Buffer.from(b)));
        resource.input.on('end', () => player.emit('idle'));
      };
      player.stop = () => { player.stopped = true; };
      return player;
    },
  };
  const processStub = { argv: [], env: { ANIMA_DISCORD_VOICE_DEPS: depsPath },
    stdin: {}, stdout: { write: line => messages.push(JSON.parse(line)) }, on: () => {}, exit: () => {} };
  const load = name => {
    if (name === 'node:module') return { createRequire: () => n => n === 'discord.js' ? {
      Client, GatewayIntentBits: { Guilds: 1, GuildVoiceStates: 128 }, ChannelType: { GuildVoice: 2 },
      PermissionFlagsBits: { ViewChannel: 1024, Connect: 1048576, Speak: 2097152 },
    } : n === '@discordjs/voice' ? sdkVoice : deps(n) };
    if (name === 'node:readline') return { createInterface: () => lines };
    if (name === 'node:perf_hooks') return { performance: { now: () => clock } };
    if (name === './discord_voice_audio.cjs') return require('../engine/discord_voice_audio.cjs');
    return require(name);
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../engine/discord_voice_transport.cjs'), 'utf8'), {
    require: load, process: processStub, Buffer, console,
    setInterval: fn => { tick = fn; return 1; }, clearInterval: () => { tick = null; },
    setTimeout: () => ({ unref() {} }),
  });
  const send = msg => lines.emit('line', JSON.stringify(msg));
  return { messages, send, owner, bot, channelId, receiver, conn, stream,
    get player() { return player; }, get client() { return sdkClient; },
    tick: now => { clock = now; tick?.(); },
    packet: (opus, timestamp, now, ssrc = 123) => {
      clock = now; const h = Buffer.alloc(12); h[0] = 128; h[1] = 120;
      h.writeUInt32BE(timestamp >>> 0, 4); h.writeUInt32BE(ssrc, 8);
      udp.emit('message', Buffer.concat([h, opus]));
    },
    async init() {
      send({ op: 'init', token: 'fixture-token', owner, channel: channelId });
      for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve));
    },
  };
}

test('actual helper receives the authorized speaker with real Opus and timed pauses', { skip: !deps }, async () => {
  const h = harness(); await h.init();
  assert.ok(h.messages.some(m => m.event === 'ready'));
  const Opus = deps('opusscript'), codec = new Opus(48000, 2, Opus.Application.AUDIO);
  const pcm = Buffer.alloc(3840);
  for (let at = 0; at < pcm.length; at += 2) pcm.writeInt16LE(Math.sin(at / 4 * Math.PI / 48) * 8000, at);
  const opus = Buffer.from(codec.encode(pcm, 960));
  h.receiver.ssrcMap.set(999, { userId: '100000000000000999' });
  h.packet(opus, 0, 0, 999);
  for (let i = 0; i < 15; i++) h.packet(opus, i * 960, i * 20);
  for (let i = 30; i < 45; i++) h.packet(opus, i * 960, i * 20);
  h.tick(1880);
  const event = h.messages.find(m => m.event === 'utterance');
  assert.ok(event);
  assert.equal(readWav(Buffer.from(event.audio, 'base64')).pcm.length, 45 * 3840);
  h.send({ op: 'hangup' }); codec.delete();
});
test('actual helper encodes progressive speech and reports transmission, not hearing', { skip: !deps }, async () => {
  const h = harness(); await h.init();
  const first = wav(Buffer.alloc(240 * 2), 24000, 1); // half a frame: retained across chunks
  h.send({ op: 'begin', job: 'j' });
  h.send({ op: 'audio', job: 'j', index: 0, audio: first.toString('base64') });
  h.send({ op: 'audio', job: 'j', index: 1, audio: first.toString('base64') });
  h.send({ op: 'end', job: 'j' });
  for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.player.packets.length, 1);
  assert.equal(h.messages.filter(m => m.event === 'audio_ack').length, 2);
  assert.ok(h.messages.some(m => m.event === 'playback' && !m.partial && m.reason === 'sent to Discord'));
  h.send({ op: 'hangup' });
});

test('acoustic capture activity never stops playback before accepted-word confirmation', { skip: !deps }, async () => {
  const h=harness();await h.init();h.send({op:'begin',job:'still-speaking'});
  const Opus=deps('opusscript'),codec=new Opus(48000,2,Opus.Application.AUDIO),pcm=Buffer.alloc(3840);
  for(let at=0;at<pcm.length;at+=2)pcm.writeInt16LE(Math.sin(at/4*Math.PI/48)*8000,at);
  const opus=Buffer.from(codec.encode(pcm,960));
  for(let i=0;i<25;i++)h.packet(opus,i*960,i*20);
  assert.ok(h.messages.some(item=>item.event==='interrupt'));
  assert.ok(!h.messages.some(item=>item.event==='playback'&&item.partial));
  h.send({op:'stop'});assert.ok(h.messages.some(item=>item.event==='playback'&&item.partial));
  h.send({op:'hangup'});codec.delete();
});
test('buffered receive events retain their own timestamps after unrelated UDP packets', { skip: !deps }, async () => {
  const h = harness(); await h.init();
  const Opus = deps('opusscript'), codec = new Opus(48000, 2, Opus.Application.AUDIO);
  const pcm = Buffer.alloc(3840);
  for (let at = 0; at < pcm.length; at += 2) pcm.writeInt16LE(Math.sin(at / 4 * Math.PI / 48) * 8000, at);
  const opus = Buffer.from(codec.encode(pcm, 960));
  h.stream.pause();
  for (let i = 0; i < 20; i++) h.packet(opus, i * 960, i * 20);
  h.receiver.ssrcMap.set(999, { userId: '100000000000000999' });
  h.packet(opus, 0, 400, 999); // Clears the tap's current timestamp.
  h.stream.resume();
  await new Promise(resolve => setImmediate(resolve));
  h.tick(1400);
  const event = h.messages.find(m => m.event === 'utterance');
  assert.ok(event);
  assert.equal(readWav(Buffer.from(event.audio, 'base64')).pcm.length, 20 * 3840);
  h.send({ op: 'hangup' }); codec.delete();
});
test('actual helper cancels queued speech and hangs up when another person joins', { skip: !deps }, async () => {
  const h = harness(); await h.init();
  h.send({ op: 'begin', job: 'j' }); h.send({ op: 'stop' });
  assert.ok(h.messages.some(m => m.event === 'playback' && m.partial));
  h.client.emit('voiceStateUpdate', {}, { id: '100000000000000999', channelId: h.channelId });
  assert.ok(h.messages.some(m => m.event === 'closed' && m.reason === 'another participant joined'));
  assert.equal(h.conn.state.status, 'destroyed');
});
test('actual helper rejects permissions and extra participants before receiving', { skip: !deps }, async () => {
  for (const options of [{ permissions: false }, { members: [['100000000000000001', { id: '100000000000000001' }],
    ['100000000000000999', { id: '100000000000000999' }]] }]) {
    const h = harness(options); await h.init();
    assert.ok(h.messages.some(m => m.event === 'error'));
    assert.ok(!h.messages.some(m => m.event === 'ready'));
  }
});
test('cancellation releases a backpressured audio producer without leaking listeners', { skip: !deps }, async () => {
  const h = harness({ pausePlayback: true }); await h.init();
  h.send({ op: 'begin', job: 'j' });
  h.send({ op: 'audio', job: 'j', index: 0, audio: wav(Buffer.alloc(48000), 24000, 1).toString('base64') });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.player.resource.input.listenerCount('close'), 1);
  h.send({ op: 'stop' });
  for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.player.resource.input.listenerCount('close'), 0);
  assert.ok(h.messages.some(m => m.event === 'audio_ack'));
  h.send({ op: 'hangup' });
});
