/*
 * live_client.js — Phase 0 browser-audio client for the TARS full-duplex test.
 *
 * Mic  : getUserMedia({echoCancellation}) -> 16 kHz PCM16 -> socket 'live_audio'
 * Play : socket 'live_audio_out' (24 kHz PCM16) -> AudioContext playback
 *        (this playback is what the browser's AEC uses as its reference signal)
 *
 * getUserMedia only works on a secure context: use http://localhost:5012/live
 * on the server itself, or serve over https for remote devices.
 */

(() => {
  const socket = io();

  const startBtn = document.getElementById('startBtn');
  const stopBtn = document.getElementById('stopBtn');
  const aecChk = document.getElementById('aecChk');
  const nsChk = document.getElementById('nsChk');
  const agcChk = document.getElementById('agcChk');
  const statusEl = document.getElementById('status');
  const logEl = document.getElementById('log');

  const INPUT_RATE = 16000;   // sent to Gemini
  const OUTPUT_RATE = 24000;  // received from Gemini
  const CAPTURE_FRAMES = 2048; // ~128 ms per chunk at 16 kHz

  let stream = null;
  let inputCtx = null;
  let outputCtx = null;
  let sourceNode = null;
  let processorNode = null;
  let muteNode = null;

  // playback scheduling
  let playHead = 0;
  let activeSources = [];

  function log(msg) {
    const time = new Date().toLocaleTimeString();
    logEl.textContent += `[${time}] ${msg}\n`;
    logEl.scrollTop = logEl.scrollHeight;
  }

  function setStatus(s) {
    statusEl.textContent = s;
  }

  function floatTo16BitPCM(input) {
    const out = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
      const s = Math.max(-1, Math.min(1, input[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }

  function schedulePlayback(arrayBuffer) {
    if (!outputCtx) return;
    const pcm16 = new Int16Array(arrayBuffer);
    if (pcm16.length === 0) return;
    const float32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) float32[i] = pcm16[i] / 32768;

    const buffer = outputCtx.createBuffer(1, float32.length, OUTPUT_RATE);
    buffer.getChannelData(0).set(float32);
    const src = outputCtx.createBufferSource();
    src.buffer = buffer;
    src.connect(outputCtx.destination);

    const now = outputCtx.currentTime;
    if (playHead < now) playHead = now + 0.02; // small cushion against underrun
    src.start(playHead);
    playHead += buffer.duration;

    activeSources.push(src);
    src.onended = () => {
      activeSources = activeSources.filter((s) => s !== src);
    };
  }

  function flushPlayback() {
    for (const s of activeSources) {
      try { s.stop(); } catch (e) { /* already stopped */ }
    }
    activeSources = [];
    playHead = 0;
  }

  socket.on('live_audio_out', (data) => {
    // socket.io delivers binary as ArrayBuffer
    if (data instanceof ArrayBuffer) schedulePlayback(data);
    else if (data && data.buffer) schedulePlayback(data.buffer);
  });

  socket.on('live_event', (ev) => {
    if (!ev) return;
    if (ev.kind === 'interrupted') {
      flushPlayback();
      log('[barge-in] playback flushed');
    } else if (ev.kind === 'input_transcript') {
      log('YOU : ' + ev.text);
    } else if (ev.kind === 'output_transcript') {
      log('TARS: ' + ev.text);
    } else if (ev.kind === 'error') {
      setStatus('error');
      log('ERROR: ' + ev.text);
    } else {
      setStatus(ev.text || ev.kind);
      log('[' + ev.kind + '] ' + (ev.text || ''));
    }
  });

  async function start() {
    startBtn.disabled = true;
    try {
      inputCtx = new AudioContext({ sampleRate: INPUT_RATE });
      outputCtx = new AudioContext({ sampleRate: OUTPUT_RATE });
      await inputCtx.resume();
      await outputCtx.resume();

      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: aecChk.checked,
          noiseSuppression: nsChk.checked,
          autoGainControl: agcChk.checked,
          channelCount: 1,
        },
      });

      const track = stream.getAudioTracks()[0];
      // Log what the browser actually applied — confirms AEC is on.
      log('mic: ' + JSON.stringify(track.getSettings()));

      sourceNode = inputCtx.createMediaStreamSource(stream);
      processorNode = inputCtx.createScriptProcessor(CAPTURE_FRAMES, 1, 1);
      processorNode.onaudioprocess = (e) => {
        const input = e.inputBuffer.getChannelData(0);
        const pcm16 = floatTo16BitPCM(input);
        socket.emit('live_audio', pcm16.buffer);
      };
      // ScriptProcessor only fires while connected to a destination, so route it
      // through a muted gain node — the mic must NOT reach the speaker directly.
      muteNode = inputCtx.createGain();
      muteNode.gain.value = 0;
      sourceNode.connect(processorNode);
      processorNode.connect(muteNode);
      muteNode.connect(inputCtx.destination);

      socket.emit('live_start');
      setStatus('listening');
      stopBtn.disabled = false;
      log('started (mic ' + INPUT_RATE + 'Hz, play ' + OUTPUT_RATE + 'Hz)');
    } catch (err) {
      setStatus('mic error');
      log('getUserMedia failed: ' + err);
      cleanup();
      startBtn.disabled = false;
    }
  }

  function cleanup() {
    if (processorNode) { try { processorNode.disconnect(); } catch (e) {} processorNode = null; }
    if (sourceNode) { try { sourceNode.disconnect(); } catch (e) {} sourceNode = null; }
    if (muteNode) { try { muteNode.disconnect(); } catch (e) {} muteNode = null; }
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    flushPlayback();
    if (inputCtx) { try { inputCtx.close(); } catch (e) {} inputCtx = null; }
    if (outputCtx) { try { outputCtx.close(); } catch (e) {} outputCtx = null; }
  }

  function stop() {
    stopBtn.disabled = true;
    socket.emit('live_stop');
    cleanup();
    setStatus('idle');
    startBtn.disabled = false;
    log('stopped');
  }

  startBtn.addEventListener('click', start);
  stopBtn.addEventListener('click', stop);

  socket.on('connect', () => log('socket connected'));
  socket.on('disconnect', () => { setStatus('disconnected'); log('socket disconnected'); });
})();
