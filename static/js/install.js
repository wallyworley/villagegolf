(function initializeInstall(){
  'use strict';

  const DISMISS_KEY = 'vf_install_card_dismissed';
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const HEADING = 'Add Village Fairways to your Home Screen';

  // Pure helpers, exposed for tests.
  function detectInstallPlatform(nav, matchMedia){
    nav = nav || {};
    let standalone = false;
    try { standalone = !!(typeof matchMedia === 'function' && matchMedia('(display-mode: standalone)').matches); } catch (error) { standalone = false; }
    if (standalone || nav.standalone === true) return 'standalone';
    const agent = String(nav.userAgent || '');
    if (/iPhone|iPad|iPod/.test(agent)) return 'ios';
    if (nav.platform === 'MacIntel' && nav.maxTouchPoints > 1) return 'ios';
    if (/Android/.test(agent)) return 'android';
    return 'desktop';
  }

  // Which browser an iPhone or iPad runs. Every iOS browser reports the platform 'ios'.
  function iosBrowser(nav){
    const agent = String((nav && nav.userAgent) || '');
    if (/CriOS/.test(agent)) return 'chrome';
    if (/FxiOS|EdgiOS|OPiOS/.test(agent)) return 'other';
    return 'safari';
  }

  function installCardMode(platform, hasPrompt, dismissed, browser){
    if (dismissed || platform === 'standalone' || platform === 'desktop') return 'hidden';
    if (platform === 'ios') {
      if (hasPrompt) return 'prompt';
      if (browser === 'chrome') return 'ios-chrome-steps';
      if (browser === 'other') return 'ios-other-steps';
      return 'ios-steps';
    }
    if (platform === 'android') return hasPrompt ? 'prompt' : 'android-steps';
    return 'hidden';
  }

  function accountStatusText(platform, hasPrompt, installed, browser){
    if (installed) return 'Installed. Open Fairways from your Home Screen.';
    if (platform === 'standalone') return "You're using the Home Screen app.";
    if (hasPrompt) return 'Add Village Fairways to your Home Screen to open it like an app.';
    if (platform === 'ios' && browser === 'chrome') return "To add it to your Home Screen, select Share in Chrome's address bar, then Add to Home Screen.";
    if (platform === 'ios' && browser === 'other') return "To add it to your Home Screen, open your browser's Share menu, then select Add to Home Screen.";
    if (platform === 'ios') return 'To add it to your Home Screen, select Share in Safari (or More, then Share), then Add to Home Screen.';
    if (platform === 'android') return 'To add it to your Home Screen, open the browser menu and select Install app or Add to Home screen.';
    return 'On a computer, Chrome and Edge show an install button in the address bar.';
  }

  const RELOAD_AFTER_HIDDEN_MS = 600000;
  const BOLD_LABELS = ['Share', 'More', 'Add to Home Screen', 'Install app', 'Add to Home screen'];

  // True only when a deploy changed the page, the app was away long enough, and no booking is running.
  function shouldReloadForUpdate(loaded, current, hiddenMs, bookingInFlight){
    return typeof loaded === 'string' && loaded !== '' && typeof current === 'string' && current !== ''
      && loaded !== current && hiddenMs >= RELOAD_AFTER_HIDDEN_MS && bookingInFlight === false;
  }

  // Append text to parent, wrapping each exact label occurrence in <strong>. Never uses innerHTML.
  function appendWithLabels(parent, text, labels){
    const names = (labels || []).slice().sort((a, b) => b.length - a.length)
      .map((label) => label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    const parts = names.length ? String(text).split(new RegExp('(' + names.join('|') + ')')) : [String(text)];
    parts.forEach((part) => {
      if (part === '') return;
      if ((labels || []).indexOf(part) !== -1) {
        const strong = document.createElement('strong');
        strong.textContent = part;
        parent.appendChild(strong);
      } else {
        parent.appendChild(document.createTextNode(part));
      }
    });
  }

  window.VFInstall = {detectInstallPlatform, installCardMode, accountStatusText, shouldReloadForUpdate, appendWithLabels, appIsIdle, iosBrowser};

  // Runtime state.
  let deferredPrompt = null;
  let installed = false;
  let dismissedInMemory = false;

  function readDismissed(){
    if (dismissedInMemory) return true;
    try { return window.localStorage.getItem(DISMISS_KEY) === '1'; } catch (error) { return false; }
  }

  function writeDismissed(){
    dismissedInMemory = true;
    try { window.localStorage.setItem(DISMISS_KEY, '1'); } catch (error) { /* storage blocked: stay dismissed for this page view only */ }
  }

  function currentPlatform(){
    const match = typeof window.matchMedia === 'function' ? window.matchMedia.bind(window) : null;
    return detectInstallPlatform(window.navigator, match);
  }

  function make(tag, className, text){
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function shareIcon(){
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'install-share-icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', 'M12 15V3M8 7l4-4 4 4M6 11H5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-8a1 1 0 0 0-1-1h-1');
    svg.appendChild(path);
    return svg;
  }

  function dismissButton(){
    const button = make('button', 'install-dismiss', 'Not now');
    button.type = 'button';
    button.addEventListener('click', () => { writeDismissed(); render(); });
    return button;
  }

  function installButton(id){
    const button = make('button', id ? 'secondary-btn install-account-btn' : 'install-primary', 'Install');
    button.type = 'button';
    if (id) button.id = id;
    button.addEventListener('click', runInstallPrompt);
    return button;
  }

  async function runInstallPrompt(){
    const event = deferredPrompt;
    if (!event) return;
    try {
      event.prompt();
      await event.userChoice;
    } catch (error) { /* the browser declined or the prompt was already used */ }
    deferredPrompt = null;
    render();
  }

  function renderCard(platform){
    const card = document.getElementById('install-card');
    if (!card) return;
    const mode = installCardMode(platform, !!deferredPrompt, readDismissed() || installed, iosBrowser(window.navigator));
    card.textContent = '';
    if (mode === 'hidden') { card.setAttribute('hidden', ''); return; }

    const heading = make('h2', 'install-card-title', HEADING);
    heading.id = 'install-card-title';
    card.appendChild(heading);

    const text = make('p', 'install-card-text');
    const actions = make('div', 'install-card-actions');
    if (mode === 'prompt') {
      text.textContent = 'Open it like an app, full screen, from an icon on your phone.';
      actions.appendChild(installButton(null));
    } else {
      let guide = '/install#android';
      if (mode === 'ios-steps') {
        guide = '/install#iphone';
        appendWithLabels(text, 'In Safari, select Share', BOLD_LABELS);
        text.appendChild(document.createTextNode(' '));
        text.appendChild(shareIcon());
        appendWithLabels(text, ", then Add to Home Screen. If you don't see Share, select More (three dots) first.", BOLD_LABELS);
      } else if (mode === 'ios-chrome-steps') {
        guide = '/install#iphone-chrome';
        appendWithLabels(text, 'In Chrome, select Share', BOLD_LABELS);
        text.appendChild(document.createTextNode(' '));
        text.appendChild(shareIcon());
        appendWithLabels(text, ' in the address bar, then Add to Home Screen.', BOLD_LABELS);
      } else if (mode === 'ios-other-steps') {
        guide = '/install#iphone';
        appendWithLabels(text, "Open your browser's Share menu, then select Add to Home Screen. If you don't see it, use Safari or Chrome.", BOLD_LABELS);
      } else {
        appendWithLabels(text, 'Open your browser menu, then select Install app or Add to Home screen.', BOLD_LABELS);
      }
      const link = make('a', 'install-link', 'Show me how');
      link.setAttribute('href', guide);
      actions.appendChild(link);
    }
    actions.appendChild(dismissButton());
    card.appendChild(text);
    card.appendChild(actions);
    card.removeAttribute('hidden');
  }

  function renderAccount(platform){
    const status = document.getElementById('install-account-status');
    if (!status) return;
    const hasPrompt = !!deferredPrompt;
    status.textContent = '';
    appendWithLabels(status, accountStatusText(platform, hasPrompt, installed, iosBrowser(window.navigator)), BOLD_LABELS);
    const old = document.getElementById('install-account-btn');
    if (old && typeof old.remove === 'function') old.remove();
    if (hasPrompt && !installed && platform !== 'standalone') {
      status.insertAdjacentElement('afterend', installButton('install-account-btn'));
    }
  }

  function render(){
    const platform = currentPlatform();
    renderCard(platform);
    renderAccount(platform);
  }

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredPrompt = event;
    render();
  });

  window.addEventListener('appinstalled', () => {
    deferredPrompt = null;
    installed = true;
    writeDismissed();
    render();
  });

  // Update check: an installed app resumes without reloading, so it asks the server whether a deploy
  // changed the page after it has been away for a while. A failed check never reloads.
  let loadedVersion = '';
  let hiddenAt = null;

  function fetchVersion(){
    if (typeof window.fetch !== 'function') return Promise.resolve('');
    return window.fetch('/api/version', {cache: 'no-store'})
      .then((response) => (response && response.ok ? response.json() : null))
      .then((data) => (data && typeof data.version === 'string' ? data.version : ''))
      .catch(() => '');
  }

  function bookingInFlight(){
    return typeof BOOKING_IN_FLIGHT !== 'undefined' && BOOKING_IN_FLIGHT === true;
  }

  // True only when nothing the user started would be lost or hidden by a reload. Reads live globals:
  // BOOKING_IN_FLIGHT and EXPERIENCE are top-level declarations, not properties of window.
  function appIsIdle(){
    if (bookingInFlight()) return false;
    if (typeof EXPERIENCE !== 'undefined' && EXPERIENCE) {
      if (EXPERIENCE.requestSubmissionInFlight === true || EXPERIENCE.myTimesLoading === true) return false;
    }
    if (document.querySelector('.overlay.show')) return false;
    if (document.getElementById('rq-review')) return false;
    const notice = document.getElementById('myTimesNotice');
    if (notice && !notice.hasAttribute('hidden')) return false;
    return true;
  }

  window.addEventListener('load', () => {
    if ('serviceWorker' in window.navigator && window.isSecureContext) {
      window.navigator.serviceWorker.register('/sw.js').catch(() => {});
    }
    fetchVersion().then((version) => { loadedVersion = version; });
  });

  if (typeof document.addEventListener === 'function') {
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') { hiddenAt = Date.now(); return; }
      if (hiddenAt === null) return;
      const hiddenMs = Date.now() - hiddenAt;
      hiddenAt = null;
      // A short absence needs no check, unless the first fetch failed and there is no baseline yet.
      if (hiddenMs < RELOAD_AFTER_HIDDEN_MS && loadedVersion !== '') return;
      fetchVersion().then((current) => {
        if (current === '') return;
        if (loadedVersion === '') { loadedVersion = current; return; }
        // Evaluate every condition now, after the response arrived, not before the fetch started.
        if (currentPlatform() !== 'standalone') return;
        if (!shouldReloadForUpdate(loadedVersion, current, hiddenMs, bookingInFlight())) return;
        if (!appIsIdle()) return;
        window.location.reload();
      });
    });
  }

  render();
})();
