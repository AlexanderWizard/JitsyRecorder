// capture.js — injected into the Jitsi page.
// Mixes every remote audio track into one stream and records it with
// MediaRecorder. Encoded chunks are handed to Python via the exposed
// binding `window.__sendChunk` (base64) so the recorder never touches disk
// from the page side.
(() => {
  if (window.__recorderStarted) return;
  window.__recorderStarted = true;

  const log = (...a) => console.log('[capture]', ...a);

  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const destination = ctx.createMediaStreamDestination();
  // All sources feed a gain bus; the bus fans out to the recorder
  // destination and to an analyser used only for level metering.
  const bus = ctx.createGain();
  bus.connect(destination);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  bus.connect(analyser);
  const buf = new Float32Array(analyser.fftSize);
  const connected = new WeakSet();

  // Report RMS level (0..100) a few times a second.
  setInterval(() => {
    if (!window.__sendLevel) return;
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    const rms = Math.sqrt(sum / buf.length);
    // Perceptual-ish scaling; clamp to 0..100.
    const level = Math.min(100, Math.round(rms * 300));
    window.__sendLevel(level);
  }, 250);

  // Attach any <audio>/<video> element that carries a live stream.
  function attach(el) {
    if (!el || connected.has(el)) return;
    let stream = null;
    try {
      if (el.srcObject instanceof MediaStream) {
        stream = el.srcObject;
      } else if (typeof el.captureStream === 'function') {
        stream = el.captureStream();
      }
    } catch (e) { /* not ready yet */ }
    if (!stream || stream.getAudioTracks().length === 0) return;
    try {
      const src = ctx.createMediaStreamSource(stream);
      src.connect(bus);
      connected.add(el);
      log('attached media element, tracks=', stream.getAudioTracks().length);
    } catch (e) { log('attach failed', e.message); }
  }

  function scan() {
    document.querySelectorAll('audio,video').forEach(attach);
  }

  // Elements are added dynamically as participants join.
  new MutationObserver(scan).observe(document.documentElement, {
    childList: true, subtree: true,
  });
  const scanTimer = setInterval(scan, 1000);
  scan();

  // Resume context after autoplay gesture policy.
  const resume = () => ctx.state === 'suspended' && ctx.resume();
  resume();

  const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus' : 'audio/webm';
  const rec = new MediaRecorder(destination.stream, {
    mimeType: mime, audioBitsPerSecond: 128000,
  });

  rec.ondataavailable = async (ev) => {
    if (!ev.data || ev.data.size === 0) return;
    const buf = await ev.data.arrayBuffer();
    // base64 encode
    let bin = '';
    const bytes = new Uint8Array(buf);
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    window.__sendChunk(btoa(bin));
  };
  rec.onstop = () => { window.__recorderStopped && window.__recorderStopped(); };

  rec.start(2000); // flush a chunk every 2s
  window.__stopRecording = () => { try { rec.stop(); } catch (e) {} clearInterval(scanTimer); };
  log('recording started, mime=', mime);
})();
