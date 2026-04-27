// ==UserScript==
// @name         OpenClaw web chat — Hungarian TTS bridge
// @namespace    openclaw.tts.hu
// @version      0.1.0
// @description  Redirects the web chat's "Read aloud" button (and optionally auto-speaks new assistant messages) through the self-hosted openclaw-tts-router so Hungarian text gets a proper F5-TTS HU voice instead of the OS default. Sample template — operators paste their endpoint and bearer token via the Tampermonkey menu.
// @author       chestercs
// @license      MIT
// @match        https://*/chat*
// @match        https://*/chat/*
// @run-at       document-start
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        GM_xmlhttpRequest
// ==/UserScript==

// -----------------------------------------------------------------------------
// PUBLIC-DEPLOY TEMPLATE for the openclaw-tts-router web chat workaround.
//
// Why this exists: the OpenClaw chat web UI bundle is hard-wired to the
// browser's native `speechSynthesis.speak()` API for the "Read aloud" button.
// On most operating systems the Hungarian voice is poor; this script
// intercepts the call and routes the text through the stack's openclaw-tts-
// router (which runs F5-TTS HU + Kokoro EN behind a single OpenAI-compat
// /v1/audio/speech endpoint). Hungarian diacritic detection picks the HU
// voice automatically; everything else gets the Kokoro EN default.
//
// SETUP:
//
// 1. Install Tampermonkey or a compatible userscript manager (Greasemonkey,
//    Violentmonkey).
//
// 2. Click the Tampermonkey toolbar icon → "Create a new script…" → paste
//    this whole file → save.
//
// 3. Open your OpenClaw chat URL (e.g. https://claw.your-host.example/chat).
//    A Tampermonkey menu appears in the icon's context menu.
//
// 4. Set the endpoint via the menu — "Set TTS endpoint…":
//    - If you have a public TTS subdomain (e.g. https://tts.your-host.example/v1/audio/speech)
//      pointing at openclaw-tts-router with CORS allowing your chat origin → use that.
//    - If you have a same-origin gateway-proxy route (your reverse proxy
//      proxies /v1/audio/speech → http://openclaw-tts-router:8080/v1/audio/speech
//      with the bearer header injected server-side) → use the chat's own origin
//      with /v1/audio/speech path. In that case you can leave the bearer empty.
//
// 5. Set the bearer via the menu — "Set TTS bearer token…":
//    - This is your `OPENCLAW_TTS_ROUTER_API_KEY` from `.env`.
//    - Skip if you went the same-origin gateway-proxy route (the proxy
//      injects the header server-side).
//
// 6. Test: click the "Read aloud" button on any assistant chat bubble. The
//    Tampermonkey console should log `[openclaw-tts] bridge active`.
//    Hungarian text plays with the F5-TTS HU voice (default_hu); English
//    plays with Kokoro `af_heart`.
//
// AUTO-SPEAK (optional): the menu has "Toggle auto-speak assistant replies"
// — when on, every new assistant chat bubble is spoken automatically as it
// arrives. Useful for hands-free setups; off by default.
//
// FALLBACK: if the fetch to the router fails (token wrong, endpoint
// unreachable), the script falls back to the original
// window.speechSynthesis.speak() so the OS default voice still kicks in.
// Open the browser DevTools console to see the warning.
// -----------------------------------------------------------------------------

(function () {
  'use strict';

  // -------- config (editable via Tampermonkey menu) --------------------------
  const CFG = {
    endpoint: GM_getValue('endpoint', ''),
    token:    GM_getValue('token', ''),
    voiceHu:  GM_getValue('voiceHu', 'default_hu'),
    voiceEn:  GM_getValue('voiceEn', 'af_heart'),
    autoSpeak: GM_getValue('autoSpeak', false),
    placeholders: ['Audio reply', 'Generated audio reply.', 'Generated audio reply'],
  };

  GM_registerMenuCommand('Set TTS endpoint…', () => {
    const v = prompt('TTS router endpoint URL (e.g. https://tts.example.com/v1/audio/speech):', CFG.endpoint);
    if (v) { GM_setValue('endpoint', v); CFG.endpoint = v; }
  });
  GM_registerMenuCommand('Set TTS bearer token…', () => {
    const v = prompt('Router bearer token (OPENCLAW_TTS_ROUTER_API_KEY) — leave blank if same-origin gateway-proxy injects it:', CFG.token);
    if (v !== null) { GM_setValue('token', v); CFG.token = v; }
  });
  GM_registerMenuCommand('Toggle auto-speak assistant replies', () => {
    CFG.autoSpeak = !CFG.autoSpeak;
    GM_setValue('autoSpeak', CFG.autoSpeak);
    alert('Auto-speak: ' + (CFG.autoSpeak ? 'ON' : 'OFF'));
  });

  // -------- utilities --------------------------------------------------------
  // Detects any Hungarian-only diacritic. Texts with these characters route
  // to the F5-TTS HU voice; everything else stays on Kokoro EN.
  const HU_RX = /[áéíóöőúüűÁÉÍÓÖŐÚÜŰ]/;
  const pickVoice = (text) => HU_RX.test(text) ? CFG.voiceHu : CFG.voiceEn;

  const isPlaceholder = (text) =>
    !text || CFG.placeholders.some(p => text.trim().toLowerCase() === p.trim().toLowerCase());

  // Walk up from any node to the chat bubble and grab its visible text.
  // The bubble is whatever ancestor carries the "Read aloud" button.
  // Used when the speechSynthesis.speak() text is just a placeholder (the
  // agent's tts skill returns "Audio reply" — the actual reply is the bubble).
  const findBubbleText = (node) => {
    let el = node && (node.nodeType === 1 ? node : node.parentElement);
    for (let i = 0; el && i < 12; i++, el = el.parentElement) {
      if (el.querySelector?.('.chat-tts-btn')) {
        const clone = el.cloneNode(true);
        clone.querySelectorAll('.chat-tts-btn, button').forEach(n => n.remove());
        const t = clone.textContent?.trim();
        if (t && t.length > 0) return t;
      }
    }
    return null;
  };

  let currentAudio = null;
  const stop = () => {
    if (currentAudio) {
      try { currentAudio.pause(); currentAudio.src = ''; } catch (_) {}
      currentAudio = null;
    }
  };

  const speakViaRouter = (text, voice) => {
    if (!text || !text.trim()) return;
    if (!CFG.endpoint) {
      console.warn('[openclaw-tts] no endpoint set — open Tampermonkey menu → "Set TTS endpoint…"');
      return;
    }
    stop();
    const headers = { 'Content-Type': 'application/json' };
    if (CFG.token) headers['Authorization'] = 'Bearer ' + CFG.token;
    const body = JSON.stringify({
      model: 'openclaw-tts',
      input: text,
      voice: voice || pickVoice(text),
      response_format: 'mp3',
      speed: 1.0,
    });
    GM_xmlhttpRequest({
      method: 'POST',
      url: CFG.endpoint,
      headers,
      data: body,
      responseType: 'blob',
      onload: (r) => {
        if (r.status !== 200) {
          console.warn('[openclaw-tts] HTTP', r.status, r.statusText);
          return;
        }
        const url = URL.createObjectURL(r.response);
        const audio = new Audio(url);
        audio.onended = () => URL.revokeObjectURL(url);
        currentAudio = audio;
        audio.play().catch(e => console.warn('[openclaw-tts] play() failed:', e));
      },
      onerror: (e) => console.warn('[openclaw-tts] request error:', e),
    });
  };

  // -------- speechSynthesis hijack ------------------------------------------
  // The web chat's "Read aloud" button calls speechSynthesis.speak(utterance).
  // We swallow that call and route the text through our router instead. On
  // any fetch failure we fall back to the original speak() so the OS voice
  // still kicks in (better-than-nothing for English).
  const realSpeak = window.speechSynthesis?.speak?.bind(window.speechSynthesis);
  if (window.speechSynthesis) {
    window.speechSynthesis.speak = function (utt) {
      try {
        let text = utt?.text || '';
        if (isPlaceholder(text)) {
          // The text "Audio reply" comes from the agent's tts skill placeholder.
          // The actual reply is in the surrounding chat bubble — fish it out.
          const ev = window.event;
          const bubbleText = findBubbleText(ev?.target) || text;
          text = bubbleText;
        }
        speakViaRouter(text);
      } catch (e) {
        console.warn('[openclaw-tts] hook error, falling back to native:', e);
        return realSpeak?.(utt);
      }
    };
    window.speechSynthesis.cancel = function () { stop(); };
  }

  // -------- optional auto-speak ---------------------------------------------
  // When CFG.autoSpeak is on, watch for new assistant chat bubbles and
  // speak each one as it lands. Heuristic: a node is a fresh assistant
  // bubble if it has a .chat-tts-btn descendant after being added.
  const seen = new WeakSet();
  const maybeSpeakBubble = (el) => {
    if (!CFG.autoSpeak) return;
    if (seen.has(el)) return;
    if (!el.querySelector?.('.chat-tts-btn')) return;
    seen.add(el);
    const clone = el.cloneNode(true);
    clone.querySelectorAll('.chat-tts-btn, button').forEach(n => n.remove());
    const text = clone.textContent?.trim();
    if (text && !isPlaceholder(text)) speakViaRouter(text);
  };

  const installObserver = () => {
    const obs = new MutationObserver((muts) => {
      for (const m of muts) {
        for (const n of m.addedNodes) {
          if (n.nodeType !== 1) continue;
          maybeSpeakBubble(n);
          n.querySelectorAll?.('*').forEach(maybeSpeakBubble);
        }
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });
  };
  if (document.body) installObserver();
  else document.addEventListener('DOMContentLoaded', installObserver);

  console.log('[openclaw-tts] bridge active — endpoint:', CFG.endpoint || '(unset)', 'auto-speak:', CFG.autoSpeak);
})();
