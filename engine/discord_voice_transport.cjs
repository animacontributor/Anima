'use strict';
// A guild/voice-only gateway. Python remains the sole handler for text DMs.
const { createRequire } = require('node:module');
const path = require('node:path');
const readline = require('node:readline');
const { PassThrough } = require('node:stream');
const { once } = require('node:events');
const { performance } = require('node:perf_hooks');
const { readWav, SpeechGate, rms } = require('./discord_voice_audio.cjs');
const deps = createRequire(path.join(process.env.ANIMA_DISCORD_VOICE_DEPS, 'package.json'));
const { Client, GatewayIntentBits, ChannelType, PermissionFlagsBits } = deps('discord.js');
const { joinVoiceChannel, entersState, VoiceConnectionStatus, createAudioPlayer,
  createAudioResource, AudioPlayerStatus, NoSubscriberBehavior, StreamType,
  EndBehaviorType, generateDependencyReport } = deps('@discordjs/voice');
const OpusScript = deps('opusscript');

if (process.argv.includes('--check')) {
  const codec = new OpusScript(48000, 2, OpusScript.Application.AUDIO);
  codec.decode(codec.encode(Buffer.alloc(3840), 960)); codec.delete();
  process.stdout.write(JSON.stringify({ ok: true, report: generateDependencyReport() }) + '\n');
  process.exit(0);
}

let client, connection, player, receive, decoder, gate, timer, owner, token = '', current;
let shuttingDown = false, pendingAudio = Promise.resolve();
const taps = new Set(); let packetStamp = null;
const packetTiming = new WeakMap();
const stats = { udp: 0, rtp: 0, ownerPackets: 0, opus: 0, decoded: 0, missingTiming: 0,
  peakRms: 0, utterances: 0 };
let lastStats = 0;
function emit(value) { process.stdout.write(JSON.stringify(value) + '\n'); }
function safeError(e) { return String(e?.message || e).split(token || '\0').join('[token]').slice(0, 500); }
function stopPlayback(reason = 'interrupted') {
  const job = current; current = null;
  if (!job) return;
  job.cancelled = true; player?.stop(true); job.input.destroy(); job.codec?.delete();
  emit({ event: 'playback', job: job.id, partial: true, reason });
}
function close(reason = 'hangup') {
  if (shuttingDown) return;
  shuttingDown = true; clearInterval(timer); stopPlayback(reason);
  gate?.reset(); receive?.destroy(); decoder?.delete();
  if (connection && connection.state.status !== VoiceConnectionStatus.Destroyed) connection.destroy();
  client?.destroy(); emit({ event: 'closed', reason });
  setTimeout(() => process.exit(0), 50).unref();
}

// The pinned receiver omits RTP timestamps from its public stream. A read-only
// UDP tap attaches them as each packet is pushed, before a readable stream
// can buffer or defer the data event. Timing belongs to that packet.
// Fail closed if this internal shape changes; never concatenate away pauses.
function tapUdp() {
  const udp = connection?.state?.networking?.state?.udp;
  if (!udp || taps.has(udp)) return Boolean(udp);
  taps.add(udp);
  udp.prependListener('message', packet => {
    stats.udp++;
    packetStamp = null;
    if (packet.length < 12 || packet[0] >>> 6 !== 2 || (packet[1] & 127) !== 120) return;
    stats.rtp++;
    const user = connection.receiver.ssrcMap.get(packet.readUInt32BE(8));
    if (user?.userId === owner) {
      stats.ownerPackets++;
      packetStamp = packet.readUInt32BE(4);
    }
  });
  return true;
}

async function init(msg) {
  if (client) throw Error('A call is already starting.');
  token = String(msg.token || ''); owner = String(msg.owner || '');
  if (!token || !/^\d{15,22}$/.test(owner) || !/^\d{15,22}$/.test(String(msg.channel)))
    throw Error('Missing call credentials or channel ID.');
  client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates] });
  client.on('error', e => { emit({ event: 'error', error: safeError(e) }); close('connection error'); });
  const ready = once(client, 'clientReady');
  await client.login(token); await ready;
  const channel = await client.channels.fetch(String(msg.channel));
  if (!channel || channel.type !== ChannelType.GuildVoice) throw Error('Choose a server voice channel, not a DM or stage.');
  const members = [...channel.members.values()];
  if (!members.some(m => m.id === owner)) throw Error('Join the voice channel yourself before inviting the bot.');
  if (members.some(m => m.id !== owner && m.id !== client.user.id))
    throw Error('Use an empty private channel with only you and the bot for this first version.');
  const permissions = channel.permissionsFor(client.user);
  if (!permissions?.has([PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect, PermissionFlagsBits.Speak]))
    throw Error('The bot needs View Channel, Connect and Speak permissions in this channel.');
  player = createAudioPlayer({ behaviors: { noSubscriber: NoSubscriberBehavior.Pause } });
  player.on('error', e => { emit({ event: 'error', error: safeError(e) }); stopPlayback('playback error'); });
  player.on(AudioPlayerStatus.Idle, () => {
    const job = current;
    if (job && job.ended && !job.cancelled) {
      current = null; job.codec?.delete();
      emit({ event: 'playback', job: job.id, partial: false, reason: 'sent to Discord' });
    }
  });
  connection = joinVoiceChannel({ channelId: channel.id, guildId: channel.guild.id,
    adapterCreator: channel.guild.voiceAdapterCreator, selfDeaf: false, selfMute: false,
    daveEncryption: true });
  connection.subscribe(player);
  connection.on('error', e => { emit({ event: 'error', error: safeError(e) }); close('voice connection error'); });
  connection.on('stateChange', (_, state) => {
    if (state.status === VoiceConnectionStatus.Ready) tapUdp();
    if (state.status === VoiceConnectionStatus.Disconnected) {
      stopPlayback('reconnecting'); gate?.reset(); packetStamp = null;
      entersState(connection, VoiceConnectionStatus.Ready, 15000)
        .then(() => emit({ event: 'reconnected' })).catch(() => close('voice disconnected'));
    }
  });
  client.on('voiceStateUpdate', (oldState, state) => {
    if (state.id === owner && state.channelId !== channel.id) close('you left the channel');
    else if (state.channelId === channel.id && state.id !== owner && state.id !== client.user.id)
      close('another participant joined');
    else if (state.id === client.user.id && state.channelId && state.channelId !== channel.id)
      close('bot moved to another channel');
  });
  await entersState(connection, VoiceConnectionStatus.Ready, 30000);
  if (!tapUdp()) throw Error('Receiver timing API changed; reinstall the pinned voice packages.');
  decoder = new OpusScript(48000, 2, OpusScript.Application.AUDIO);
  gate = new SpeechGate(msg.speech || {}, event => {
    // Acoustic activity only starts a candidate capture. Python stops playback
    // after transcription accepts actual words from this capture.
    if (event.event === 'utterance') stats.utterances++;
    emit(event);
  });
  receive = connection.receiver.subscribe(owner, { end: { behavior: EndBehaviorType.Manual } });
  const push = receive.push;
  receive.push = function(packet, ...args) {
    if (packet && packetStamp !== null) packetTiming.set(packet, packetStamp);
    return push.call(this, packet, ...args);
  };
  receive.on('data', opus => {
    try {
      stats.opus++;
      const timestamp = packetTiming.get(opus);
      if (timestamp === undefined) { stats.missingTiming++; return; }
      packetTiming.delete(opus);
      const pcm = decoder.decode(opus);
      stats.decoded++;
      stats.peakRms = Math.max(stats.peakRms, rms(pcm));
      gate.push(pcm, timestamp, performance.now());
    } catch (e) { emit({ event: 'error', error: safeError(e) }); close('audio receive error'); }
  });
  receive.on('error', e => { emit({ event: 'error', error: safeError(e) }); close('audio receive error'); });
  timer = setInterval(() => {
    const now = performance.now(); gate.tick(now);
    if (now - lastStats >= 5000) {
      lastStats = now;
      // Packet counts and levels only: no audio, text, IDs, tokens or keys.
      process.stderr?.write(JSON.stringify({ event: 'receive_stats', ...stats,
        ownerMapped: Boolean(connection.receiver.ssrcMap.get(owner)),
        connection: connection.state.status, dave: Boolean(connection.state.networking?.state?.dave),
        threshold: gate.options.threshold }) + '\n');
    }
  }, 20);
  emit({ event: 'ready', channel: channel.id });
}

function begin(msg) {
  if (!connection || connection.state.status !== VoiceConnectionStatus.Ready) throw Error('Voice connection is not ready.');
  stopPlayback('replaced');
  current = { id: msg.job, input: new PassThrough({ objectMode: true, highWaterMark: 8 }),
    tail: Buffer.alloc(0), cancelled: false, ended: false, codec: null, format: null };
  const job = current;
  job.input.on('error', () => {});
  player.play(createAudioResource(job.input, { inputType: StreamType.Opus, metadata: { job: msg.job } }));
}
async function writeFrames(job, final = false) {
  const bytes = job.format.rate / 50 * job.format.channels * 2;
  if (final && job.tail.length % bytes) job.tail = Buffer.concat([job.tail, Buffer.alloc(bytes - job.tail.length % bytes)]);
  while (job.tail.length >= bytes && current === job && !job.cancelled) {
    const pcm = job.tail.subarray(0, bytes); job.tail = job.tail.subarray(bytes);
    const packet = Buffer.from(job.codec.encode(pcm, job.format.rate / 50));
    if (!job.input.write(packet)) {
      await new Promise(resolve => {
        const done = () => {
          job.input.off('drain', done); job.input.off('close', done); job.input.off('error', done);
          resolve();
        };
        if (job.input.destroyed) return resolve();
        job.input.once('drain', done); job.input.once('close', done); job.input.once('error', done);
      });
    }
  }
}
async function audio(msg) {
  const job = current;
  if (!job || job.id !== msg.job) { emit({ event: 'audio_ack', job: msg.job, index: msg.index }); return; }
  const parsed = readWav(Buffer.from(msg.audio, 'base64'));
  if (!job.codec) {
    job.format = parsed;
    job.codec = new OpusScript(parsed.rate, parsed.channels, OpusScript.Application.AUDIO);
    job.codec.encoderCTL(4002, 128000);
  } else if (parsed.rate !== job.format.rate || parsed.channels !== job.format.channels)
    throw Error('Voice stream changed format mid-reply.');
  job.tail = Buffer.concat([job.tail, parsed.pcm]);
  await writeFrames(job);
  emit({ event: 'audio_ack', job: msg.job, index: msg.index });
}
async function end(msg) {
  const job = current;
  if (!job || job.id !== msg.job) return;
  if (job.codec) await writeFrames(job, true);
  job.ended = true; job.input.end();
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on('line', line => {
  try {
    if (line.length > 12 * 1024 * 1024) throw Error('Oversized transport message.');
    const msg = JSON.parse(line);
    if (msg.op === 'init') init(msg).catch(e => { emit({ event: 'error', error: safeError(e) }); close('call setup failed'); });
    else if (msg.op === 'hangup') close();
    else if (msg.op === 'stop') stopPlayback();
    else if (msg.op === 'begin') begin(msg);
    else if (msg.op === 'audio' || msg.op === 'end') {
      pendingAudio = pendingAudio.then(() => msg.op === 'audio' ? audio(msg) : end(msg))
        .catch(e => { emit({ event: 'error', error: safeError(e) }); stopPlayback('audio error'); });
    }
  } catch (e) { emit({ event: 'error', error: safeError(e) }); }
});
input.on('close', () => close('bridge closed'));
process.on('SIGTERM', () => close('bridge closed'));
process.on('uncaughtException', e => { emit({ event: 'error', error: safeError(e) }); close('transport error'); });
