(function initializeNotify(){
  'use strict';

  const SYNC_KEY = 'vf_push_synced_at';
  const SYNC_INTERVAL_MS = 600000;
  const ENDPOINT_WAIT_MS = 2000;
  const READY_WAIT_MS = 10000;
  const BOLD_LABELS = ['Settings'];

  const TEXT = {
    unsupported: "This browser can't show notifications.",
    installFirst: 'Add Village Fairways to your Home Screen, then open it from the Home Screen icon to turn on notifications.',
    blocked: "Notifications are blocked for Village Fairways. Turn them on in your phone's Settings.",
    off: 'Get a notification when a tee time is booked, and a reminder about 12 hours before it.',
    on: 'Notifications are on for this device.',
  };
  const LABELS = {turnOn: 'Turn on notifications', turnOff: 'Turn off notifications', test: 'Send a test notification', guide: 'Home Screen install guide'};
  const MESSAGES = {
    notGranted: 'Notifications were not turned on.',
    turnOnFailed: 'Notifications could not be turned on. Try again.',
    turnedOff: 'Notifications are off for this device.',
    turnOffFailed: 'Notifications could not be turned off. Try again.',
    testSent: 'Test sent. It should arrive in a moment.',
    testFailed: 'The test did not reach this device.',
    testUnavailable: 'The test could not be sent. Try again.',
  };

  // Pure helper, exposed for tests.
  function notifyState(info){
    info = info || {};
    if (!info.configured) return 'hidden';
    if (info.platform === 'ios') return 'install-first';
    if (!info.supported) return 'unsupported';
    if (info.permission === 'denied') return 'blocked';
    if (info.subscribed && info.permission === 'granted') return 'on';
    return 'off';
  }

  // ── Browser capability guards ──────────────────────────────────────────────
  function nav(){ return window.navigator || {}; }

  function browserSupportsPush(){
    try {
      return typeof nav() === 'object' && 'serviceWorker' in nav() && !!nav().serviceWorker
        && typeof window.Notification !== 'undefined' && typeof window.PushManager !== 'undefined';
    } catch (error) { return false; }
  }

  function permission(){
    try { return typeof window.Notification !== 'undefined' ? String(window.Notification.permission) : 'default'; } catch (error) { return 'default'; }
  }

  function platform(){
    try {
      if (window.VFInstall && typeof window.VFInstall.detectInstallPlatform === 'function') {
        const match = typeof window.matchMedia === 'function' ? window.matchMedia.bind(window) : null;
        return window.VFInstall.detectInstallPlatform(window.navigator, match);
      }
    } catch (error) { /* fall through */ }
    return 'desktop';
  }

  function timeout(ms, value){
    return new Promise((resolve) => {
      const schedule = typeof window.setTimeout === 'function' ? window.setTimeout.bind(window) : (typeof setTimeout === 'function' ? setTimeout : null);
      if (schedule) schedule(() => resolve(value), ms);
    });
  }

  // The current registration's subscription, or null. Never waits on a worker that is not there.
  function currentSubscription(){
    if (!browserSupportsPush()) return Promise.resolve(null);
    try {
      return Promise.resolve(nav().serviceWorker.getRegistration())
        .then((registration) => (registration && registration.pushManager ? registration.pushManager.getSubscription() : null))
        .then((subscription) => subscription || null)
        .catch(() => null);
    } catch (error) { return Promise.resolve(null); }
  }

  function currentEndpoint(){
    const lookup = currentSubscription().then((subscription) => (subscription && typeof subscription.endpoint === 'string' ? subscription.endpoint : null));
    return Promise.race([lookup, timeout(ENDPOINT_WAIT_MS, null)]).catch(() => null);
  }

  // ── Server calls ───────────────────────────────────────────────────────────
  function postJson(path, body){
    if (typeof window.fetch !== 'function') return Promise.reject(new Error('fetch unavailable'));
    return window.fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  }

  function readJson(response){
    return response && typeof response.json === 'function' ? response.json().catch(() => ({})) : Promise.resolve({});
  }

  let configPromise = null;
  function getConfig(){
    if (configPromise) return configPromise;
    const attempt = typeof window.fetch !== 'function' ? Promise.resolve(null)
      : window.fetch('/api/push/config', {cache: 'no-store'})
        .then((response) => (response && response.ok ? response.json() : null))
        .catch(() => null);
    configPromise = attempt.then((data) => {
      if (!data) configPromise = null;  // a failed lookup is retried next time
      return data && data.enabled === true && typeof data.public_key === 'string' ? data : {enabled: false, public_key: null};
    });
    return configPromise;
  }

  function keyBytes(base64url){
    const padded = String(base64url).replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (String(base64url).length % 4)) % 4);
    const raw = window.atob(padded);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  // ── Sync: a device belongs to whoever is signed in on it ───────────────────
  let lastSync = 0;
  let inflight = null;

  function readLastSync(){
    let stored = 0;
    try { stored = Number(window.localStorage.getItem(SYNC_KEY)) || 0; } catch (error) { stored = 0; }
    return Math.max(lastSync, stored);
  }

  function writeLastSync(){
    lastSync = Date.now();
    try { window.localStorage.setItem(SYNC_KEY, String(lastSync)); } catch (error) { /* storage blocked: the in-memory value still limits this page view */ }
  }

  // Forget the sync stamp, so the next account to sign in on this device syncs at once.
  function clearSyncStamp(){
    lastSync = 0;
    renderSyncTried = false;
    try { window.localStorage.removeItem(SYNC_KEY); } catch (error) { /* nothing stored to clear */ }
  }

  function postSubscription(){
    return currentSubscription().then((subscription) => {
      if (!subscription || typeof subscription.toJSON !== 'function') return false;
      return postJson('/api/push/subscribe', {subscription: subscription.toJSON()}).then((response) => {
        // A 401 means nobody is signed in on this device yet: ignore it silently and try again later.
        if (response && response.ok) { writeLastSync(); return true; }
        return false;
      });
    }).catch(() => false);
  }

  // Post this device's subscription to the signed-in account. Without {force: true} it runs at most
  // once per ten minutes; sign-in forces it so the device moves to the account that just signed in.
  function syncSubscription(options){
    const force = !!(options && options.force);
    if (!browserSupportsPush() || permission() !== 'granted') return Promise.resolve(false);
    if (!force && (inflight || Date.now() - readLastSync() < SYNC_INTERVAL_MS)) return Promise.resolve(false);
    const previous = inflight || Promise.resolve();
    const mine = previous.then(postSubscription, postSubscription);
    inflight = mine;
    mine.then(() => { if (inflight === mine) inflight = null; });
    return mine;
  }

  // Whether the server holds this device's endpoint for the signed-in account.
  function deviceRegistered(endpoint){
    if (typeof window.fetch !== 'function' || !endpoint) return Promise.resolve(false);
    return window.fetch('/api/push/config', {cache: 'no-store', headers: {'X-Push-Endpoint': endpoint}})
      .then((response) => (response && response.ok ? response.json() : null))
      .then((data) => !!(data && data.device_registered === true))
      .catch(() => false);
  }

  // ── Rendering ──────────────────────────────────────────────────────────────
  let message = '';
  let renderToken = 0;

  function make(tag, className, text){
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function appendLabelled(parent, text){
    if (window.VFInstall && typeof window.VFInstall.appendWithLabels === 'function') {
      window.VFInstall.appendWithLabels(parent, text, BOLD_LABELS);
    } else {
      parent.appendChild(document.createTextNode(text));
    }
  }

  function button(label, handler){
    const node = make('button', 'secondary-btn', label);
    node.type = 'button';
    node.addEventListener('click', handler);
    return node;
  }

  function show(state){
    const section = document.getElementById('notify-section');
    const status = document.getElementById('notify-status');
    const actions = document.getElementById('notify-actions');
    if (!section || !status || !actions) return;
    status.textContent = '';
    actions.textContent = '';
    if (state === 'hidden') { section.setAttribute('hidden', ''); return; }
    const copy = {unsupported: TEXT.unsupported, 'install-first': TEXT.installFirst, blocked: TEXT.blocked, off: TEXT.off, on: TEXT.on}[state];
    appendLabelled(status, copy);
    if (state === 'install-first') {
      const link = make('a', 'install-link', LABELS.guide);
      link.setAttribute('href', '/install');
      actions.appendChild(link);
    } else if (state === 'off') {
      actions.appendChild(button(LABELS.turnOn, turnOn));
    } else if (state === 'on') {
      actions.appendChild(button(LABELS.test, sendTest));
      actions.appendChild(button(LABELS.turnOff, turnOff));
    }
    if (message) {
      const note = make('p', 'notify-message', message);
      note.setAttribute('role', 'status');
      actions.appendChild(note);
    }
    section.removeAttribute('hidden');
  }

  let renderSyncTried = false;

  // True only when the browser holds a subscription AND the server confirms it for this account.
  function confirmedSubscription(subscription){
    const endpoint = subscription && typeof subscription.endpoint === 'string' ? subscription.endpoint : '';
    if (!endpoint || permission() !== 'granted') return Promise.resolve(false);
    return deviceRegistered(endpoint).then((registered) => {
      if (registered || renderSyncTried) return registered;
      renderSyncTried = true;
      return (inflight || syncSubscription({force: true})).then((synced) => (synced ? deviceRegistered(endpoint) : false));
    });
  }

  function render(){
    const token = renderToken += 1;
    try {
      if (!document.getElementById('notify-section')) return Promise.resolve();
      return getConfig()
        .then((config) => currentSubscription().then((subscription) => ({config, subscription})))
        .then(({config, subscription}) => {
          const enabled = !!(config && config.enabled);
          return (enabled && subscription ? confirmedSubscription(subscription) : Promise.resolve(false))
            .then((confirmed) => ({enabled, confirmed}));
        })
        .then(({enabled, confirmed}) => {
          if (token !== renderToken) return;
          show(notifyState({
            configured: enabled, supported: browserSupportsPush(), platform: platform(),
            permission: permission(), subscribed: confirmed,
          }));
        })
        .catch(() => {});
    } catch (error) { return Promise.resolve(); }
  }

  function setMessage(text){
    message = text;
    return render();
  }

  // ── Actions ────────────────────────────────────────────────────────────────
  function readyRegistration(){
    return Promise.race([nav().serviceWorker.ready, timeout(READY_WAIT_MS, null)]).then((registration) => {
      if (!registration) throw new Error('worker not ready');
      return registration;
    });
  }

  function subscribeBrowser(registration, key){
    const options = {userVisibleOnly: true, applicationServerKey: key};
    return registration.pushManager.subscribe(options).catch((error) => {
      // A subscription made with a different server key blocks a new one: drop it and retry once.
      return registration.pushManager.getSubscription().then((existing) => {
        if (!existing) throw error;
        return existing.unsubscribe().then(() => registration.pushManager.subscribe(options));
      });
    });
  }

  async function turnOn(){
    try {
      // The permission prompt must start inside the click handler, before any other await.
      const result = await window.Notification.requestPermission();
      if (result !== 'granted') { await setMessage(result === 'denied' ? '' : MESSAGES.notGranted); return; }
      const config = await getConfig();
      if (!config || !config.enabled) throw new Error('not configured');
      const registration = await readyRegistration();
      const subscription = await subscribeBrowser(registration, keyBytes(config.public_key));
      const response = await postJson('/api/push/subscribe', {subscription: subscription.toJSON()});
      if (!response || !response.ok) {
        try { await subscription.unsubscribe(); } catch (error) { /* the page shows the error below */ }
        const data = await readJson(response);
        await setMessage(typeof data.error === 'string' && data.error ? data.error : MESSAGES.turnOnFailed);
        return;
      }
      writeLastSync();
      await setMessage('');
    } catch (error) {
      await setMessage(MESSAGES.turnOnFailed);
    }
  }

  async function turnOff(){
    try {
      const subscription = await currentSubscription();
      const endpoint = subscription && typeof subscription.endpoint === 'string' ? subscription.endpoint : null;
      if (subscription) await subscription.unsubscribe();
      if (endpoint) {
        // The browser no longer receives anything, so a failed server update is not shown:
        // the server drops the stale entry the next time the push service reports it gone.
        try { await postJson('/api/push/unsubscribe', {endpoint}); } catch (error) { /* see above */ }
      }
      await setMessage(MESSAGES.turnedOff);
    } catch (error) {
      await setMessage(MESSAGES.turnOffFailed);
    }
  }

  async function sendTest(){
    try {
      const response = await postJson('/api/push/test', {});
      const data = await readJson(response);
      if (response && response.ok && data.success === true) await setMessage(MESSAGES.testSent);
      else await setMessage(typeof data.error === 'string' && data.error ? data.error : MESSAGES.testFailed);
    } catch (error) {
      await setMessage(MESSAGES.testUnavailable);
    }
  }

  window.VFNotify = {notifyState, currentEndpoint, render, syncSubscription, clearSyncStamp, copy: {text: TEXT, labels: LABELS, messages: MESSAGES}};

  // ── Wiring: every step guarded so this script never throws at load ─────────
  try {
    if (typeof window.openAccountDialog === 'function') {
      const original = window.openAccountDialog;
      window.openAccountDialog = function openAccountDialogWithNotify(){
        const result = original.apply(this, arguments);
        try { message = ''; syncSubscription(); render(); } catch (error) { /* the dialog still opened */ }
        return result;
      };
    }
    if (document && typeof document.addEventListener === 'function') {
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') syncSubscription();
      });
    }
    syncSubscription();
    render();
  } catch (error) { /* notifications are optional: the rest of the page keeps working */ }
})();
