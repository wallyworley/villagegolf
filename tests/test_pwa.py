"""Offline regressions for the installable site: manifest, service worker, install card, and guides."""
import hashlib
import html
import importlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
EXPERIENCE_CSS = (STATIC / "css" / "experience.css").read_text(encoding="utf-8")
INSTALL_JS = (STATIC / "js" / "install.js").read_text(encoding="utf-8")

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
CHROME_IOS_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
                 "(KHTML, like Gecko) CriOS/140.0.0.0 Mobile/15E148 Safari/604.1")
FIREFOX_IOS_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) FxiOS/140.0 Mobile/15E148 Safari/605.1.15")
EDGE_IOS_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/18.0 EdgiOS/140.0.0.0 Mobile/15E148 Safari/605.1.15")
MAC_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
ANDROID_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36")
MAC_CHROME_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

EXPECTED_MANIFEST = {
    "id": "/",
    "name": "Village Fairways",
    "short_name": "Fairways",
    "description": "Book Villages tee times, manage reservations, and get golf cart directions.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f3f6f1",
    "theme_color": "#14532d",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

DOM_HARNESS = r"""
function makeDom() {
  const registry = new Map();
  function node(tag) {
    const el = {
      tagName: tag, className: '', id: '', children: [], attrs: {}, listeners: {}, _text: '', parent: null, type: '',
      setAttribute(name, value) { this.attrs[name] = String(value); },
      removeAttribute(name) { delete this.attrs[name]; },
      getAttribute(name) { return this.attrs[name]; },
      appendChild(child) { child.parent = this; this.children.push(child); return child; },
      addEventListener(type, handler) { this.listeners[type] = handler; },
      insertAdjacentElement(where, child) {
        assert.equal(where, 'afterend');
        child.parent = this.parent;
        this.parent.children.splice(this.parent.children.indexOf(this) + 1, 0, child);
        if (child.id) registry.set(child.id, child);
        return child;
      },
      remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); if (this.id) registry.delete(this.id); },
      get textContent() { return this._text + this.children.map((c) => c.textContent || '').join(''); },
      set textContent(value) { this._text = String(value); this.children = []; },
      get hidden() { return 'hidden' in this.attrs; },
    };
    return el;
  }
  const card = node('aside'); card.id = 'install-card'; card.attrs.hidden = '';
  const section = node('section');
  const status = node('p'); status.id = 'install-account-status';
  section.appendChild(status);
  registry.set('install-card', card); registry.set('install-account-status', status);
  const document = {
    getElementById: (id) => registry.get(id) || null,
    createElement: (tag) => node(tag),
    createElementNS: (ns, tag) => node(tag),
    createTextNode: (text) => { const t = node('#text'); t._text = text; return t; },
  };
  return {document, card, status, registry};
}

function boot({userAgent, storage, standalone}) {
  const dom = makeDom();
  const handlers = {};
  const window = {
    isSecureContext: true,
    navigator: {userAgent, platform: 'Linux armv81', maxTouchPoints: 5},
    matchMedia: () => ({matches: !!standalone}),
    addEventListener(type, handler) { handlers[type] = handler; },
    localStorage: storage,
  };
  const context = {window, document: dom.document, console};
  vm.createContext(context);
  vm.runInContext(SOURCE, context);
  return {dom, window, storage, fire: (type, event) => handlers[type](event)};
}

function strongTexts(root) {
  const out = [];
  (function walk(el) { if (el.tagName === 'strong') out.push(el.textContent); el.children.forEach(walk); })(root);
  return out;
}

function findButton(parent, label) {
  const stack = [parent];
  while (stack.length) {
    const el = stack.pop();
    if (el.tagName === 'button' && el.textContent === label) return el;
    stack.push(...el.children);
  }
  return null;
}

function fakePrompt() {
  const event = {prevented: false, prompts: 0,
    preventDefault() { this.prevented = true; },
    prompt() { this.prompts += 1; },
    userChoice: Promise.resolve({outcome: 'accepted'})};
  return event;
}

function memoryStorage(initial) {
  const data = Object.assign({}, initial);
  return {getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, data};
}

"""


def png_size(path):
    """Read the pixel size from the PNG IHDR chunk."""
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    assert data[12:16] == b"IHDR", f"{path} has no IHDR chunk first"
    return struct.unpack(">II", data[16:24])


def visible_text(markup):
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup)
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


class _Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))


def run_node(script):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not installed")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout


class PwaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(os.environ, {
            "SECRET_KEY": "test-only-session-key", "USER_DB_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "DISABLE_REQUEST_WATCHER": "1", "SESSION_COOKIE_SECURE": "0", "TRUST_PROXY": "0",
        })
        cls.environment.start()
        with patch("dotenv.load_dotenv"):
            cls.api = importlib.import_module("app")
        cls.api.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api.user_store._DB_PATH = self.tmp.name + "/users.db"
        self.api.user_store._initialized = False
        self.api.user_store._fernet = Fernet(Fernet.generate_key())
        self.client = self.api.app.test_client()

    def get(self, path):
        response = self.client.get(path)
        self.addCleanup(response.close)
        return response

    def service_worker_body(self):
        response = self.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    # 1
    def test_manifest_has_install_fields(self):
        response = self.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/manifest+json")
        manifest = json.loads(response.get_data(as_text=True))
        self.assertEqual(manifest, EXPECTED_MANIFEST)
        self.assertEqual(len(manifest["icons"]), 3)
        for icon in manifest["icons"]:
            path = ROOT / icon["src"].lstrip("/")
            self.assertTrue(path.is_file(), icon["src"])
            width, height = png_size(path)
            self.assertEqual(f"{width}x{height}", icon["sizes"], icon["src"])

    # 2
    def test_head_icons_exist_with_declared_sizes(self):
        self.assertEqual(png_size(STATIC / "icons" / "apple-touch-icon.png"), (180, 180))
        self.assertEqual(png_size(STATIC / "icons" / "favicon-32.png"), (32, 32))
        for tag in (
            '<meta name="mobile-web-app-capable" content="yes"/>',
            '<meta name="apple-mobile-web-app-title" content="Fairways"/>',
            '<link rel="manifest" href="/manifest.webmanifest"/>',
            '<link rel="icon" type="image/png" sizes="32x32" href="/static/icons/favicon-32.png"/>',
            '<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png"/>',
        ):
            self.assertIn(tag, INDEX)
        interface = INDEX.index('<script src="/static/js/interface.js"></script>')
        install = INDEX.index('<script src="/static/js/install.js"></script>')
        self.assertLess(interface, install)

    # 3
    def test_service_worker_is_served_from_root_with_offline_version(self):
        response = self.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/javascript")
        self.assertEqual(response.headers["Cache-Control"], "no-cache")
        body = response.get_data(as_text=True)
        version = hashlib.sha256((ROOT / "templates" / "offline.html").read_bytes()).hexdigest()[:12]
        self.assertNotIn("__OFFLINE_VERSION__", body)
        self.assertIn(f"vf-offline-{version}", body)

    # 4
    def test_service_worker_kill_switch_unregisters(self):
        with patch.dict(os.environ, {"DISABLE_SERVICE_WORKER": "1"}):
            response = self.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("unregister", body)
        self.assertNotIn("'fetch'", body)
        self.assertNotIn('"fetch"', body)
        self.assertNotIn("vf-offline", body)
        self.assertNotIn("fetch", body)

    # 5
    def test_service_worker_only_answers_same_origin_page_loads(self):
        script = r"""
const vm = require('node:vm');
const BODY = %s;
const log = {putCalls: 0};
const listeners = {};
const cacheStore = {put() { log.putCalls += 1; }, add() { return Promise.resolve(); }};
const self_ = {
  location: {origin: 'https://villagefairways.com'},
  addEventListener(type, handler) { listeners[type] = handler; },
  skipWaiting() { return Promise.resolve(); },
  clients: {claim() { return Promise.resolve(); }},
};
const context = {
  self: self_, URL, Promise, console,
  Request: class { constructor(url, options) { this.url = url; this.cache = options && options.cache; } },
  caches: {
    open() { return Promise.resolve(cacheStore); },
    keys() { return Promise.resolve([]); },
    delete() { return Promise.resolve(true); },
    match(url) { return Promise.resolve({tag: 'offline', url}); },
  },
  fetch: null,
};
vm.createContext(context);
vm.runInContext(BODY, context);
async function dispatch(request, fetchBehavior) {
  context.fetch = fetchBehavior;
  let responded = null;
  listeners.fetch({request, respondWith(promise) { responded = promise; }});
  return {called: responded !== null, value: responded ? await responded : null};
}
const ok = () => Promise.resolve({tag: 'network'});
const fail = () => Promise.reject(new TypeError('offline'));
(async () => {
  const cases = {
    page: await dispatch({method: 'GET', mode: 'navigate', url: 'https://villagefairways.com/'}, ok),
    apiCors: await dispatch({method: 'GET', mode: 'cors', url: 'https://villagefairways.com/api/session'}, ok),
    apiPost: await dispatch({method: 'POST', mode: 'cors', url: 'https://villagefairways.com/api/book'}, ok),
    apiNavigate: await dispatch({method: 'GET', mode: 'navigate', url: 'https://villagefairways.com/api/my-tee-times'}, ok),
    crossOrigin: await dispatch({method: 'GET', mode: 'navigate', url: 'https://gis.thevillages.net/'}, ok),
    script: await dispatch({method: 'GET', mode: 'no-cors', url: 'https://villagefairways.com/static/js/experience.js'}, ok),
    offline: await dispatch({method: 'GET', mode: 'navigate', url: 'https://villagefairways.com/'}, fail),
  };
  console.log(JSON.stringify({cases, putCalls: log.putCalls}));
})().catch((error) => { console.error(error); process.exit(1); });
""" % json.dumps(self.service_worker_body())
        data = json.loads(run_node(script))
        cases = data["cases"]
        self.assertTrue(cases["page"]["called"])
        self.assertEqual(cases["page"]["value"], {"tag": "network"})
        for name in ("apiCors", "apiPost", "apiNavigate", "crossOrigin", "script"):
            self.assertFalse(cases[name]["called"], name)
        self.assertTrue(cases["offline"]["called"])
        self.assertEqual(cases["offline"]["value"], {"tag": "offline", "url": "/offline"})
        self.assertEqual(data["putCalls"], 0)

    # 6
    def test_service_worker_install_caches_only_the_offline_page(self):
        body = self.service_worker_body()
        current = re.search(r"vf-offline-[0-9a-f]{12}", body).group(0)
        script = r"""
const vm = require('node:vm');
const BODY = %s;
const CURRENT = %s;
const log = {added: [], opened: [], deleted: [], skipWaiting: 0, claim: 0, put: 0};
const listeners = {};
const cache = {add(request) { log.added.push({url: request.url, cache: request.cache}); return Promise.resolve(); },
               put() { log.put += 1; }};
const context = {
  self: {location: {origin: 'https://villagefairways.com'},
         addEventListener(type, handler) { listeners[type] = handler; },
         skipWaiting() { log.skipWaiting += 1; return Promise.resolve(); },
         clients: {claim() { log.claim += 1; return Promise.resolve(); }}},
  URL, Promise, console,
  Request: class { constructor(url, options) { this.url = url; this.cache = options && options.cache; } },
  caches: {open(name) { log.opened.push(name); return Promise.resolve(cache); },
           keys() { return Promise.resolve(['vf-offline-old', CURRENT]); },
           delete(name) { log.deleted.push(name); return Promise.resolve(true); },
           match() { return Promise.resolve(null); }},
};
vm.createContext(context);
vm.runInContext(BODY, context);
(async () => {
  let pending = null;
  listeners.install({waitUntil(promise) { pending = promise; }});
  await pending;
  pending = null;
  listeners.activate({waitUntil(promise) { pending = promise; }});
  await pending;
  console.log(JSON.stringify(log));
})().catch((error) => { console.error(error); process.exit(1); });
""" % (json.dumps(body), json.dumps(current))
        log = json.loads(run_node(script))
        self.assertEqual(log["opened"], [current])
        self.assertEqual(log["added"], [{"url": "/offline", "cache": "reload"}])
        self.assertEqual(log["skipWaiting"], 1)
        self.assertEqual(log["deleted"], ["vf-offline-old"])
        self.assertEqual(log["claim"], 1)
        self.assertEqual(log["put"], 0)

    # 7
    def test_offline_page_is_self_contained(self):
        response = self.get("/offline")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIsNone(re.search(r"\bsrc\s*=", page))
        for href in re.findall(r"\bhref\s*=\s*[\"']([^\"']*)[\"']", page):
            self.assertEqual(href, "/")
        self.assertIn("You're offline", page)
        self.assertIn("Try again", page)

    # 8
    def test_app_shell_routes_are_not_cached(self):
        for path in ("/", "/install", "/help", "/offline", "/sw.js", "/manifest.webmanifest"):
            response = self.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.headers["Cache-Control"], "no-cache", path)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff", path)
        self.assertEqual(self.get("/api/session").headers["Cache-Control"], "no-store")

    # 9
    def test_install_platform_detection(self):
        script = r"""
const vm = require('node:vm');
const SOURCE = %s;
const context = {window: {addEventListener() {}, isSecureContext: true, navigator: {}}, document: {getElementById() { return null; }}};
vm.createContext(context);
vm.runInContext(SOURCE, context);
const detect = context.window.VFInstall.detectInstallPlatform;
const browser = context.window.VFInstall.iosBrowser;
const never = () => ({matches: false});
const always = () => ({matches: true});
console.log(JSON.stringify({
  iphone: detect({userAgent: %s, platform: 'iPhone', maxTouchPoints: 5}, never),
  ipados: detect({userAgent: %s, platform: 'MacIntel', maxTouchPoints: 5}, never),
  android: detect({userAgent: %s, platform: 'Linux armv81', maxTouchPoints: 5}, never),
  macChrome: detect({userAgent: %s, platform: 'MacIntel', maxTouchPoints: 0}, never),
  standaloneQuery: detect({userAgent: %s, platform: 'iPhone', maxTouchPoints: 5}, always),
  standaloneFlag: detect({userAgent: %s, platform: 'iPhone', maxTouchPoints: 5, standalone: true}, never),
  chromeIphone: detect({userAgent: %s, platform: 'iPhone', maxTouchPoints: 5}, never),
  browserChrome: browser({userAgent: %s}),
  browserFirefox: browser({userAgent: %s}),
  browserEdge: browser({userAgent: %s}),
  browserSafari: browser({userAgent: %s}),
  browserIpadSafari: browser({userAgent: %s}),
}));
""" % (json.dumps(INSTALL_JS), json.dumps(IPHONE_UA), json.dumps(MAC_UA), json.dumps(ANDROID_UA),
       json.dumps(MAC_CHROME_UA), json.dumps(IPHONE_UA), json.dumps(IPHONE_UA), json.dumps(CHROME_IOS_UA),
       json.dumps(CHROME_IOS_UA), json.dumps(FIREFOX_IOS_UA), json.dumps(EDGE_IOS_UA), json.dumps(IPHONE_UA),
       json.dumps(MAC_UA))
        self.assertEqual(json.loads(run_node(script)), {
            "iphone": "ios", "ipados": "ios", "android": "android", "macChrome": "desktop",
            "standaloneQuery": "standalone", "standaloneFlag": "standalone", "chromeIphone": "ios",
            "browserChrome": "chrome", "browserFirefox": "other", "browserEdge": "other",
            "browserSafari": "safari", "browserIpadSafari": "safari",
        })

    # 10
    def test_install_card_modes(self):
        script = r"""
const vm = require('node:vm');
const SOURCE = %s;
const context = {window: {addEventListener() {}, isSecureContext: true, navigator: {}}, document: {getElementById() { return null; }}};
vm.createContext(context);
vm.runInContext(SOURCE, context);
const mode = context.window.VFInstall.installCardMode;
const out = {};
for (const platform of ['ios', 'android', 'desktop', 'standalone'])
  for (const hasPrompt of [true, false])
    for (const dismissed of [true, false])
      out[[platform, hasPrompt, dismissed].join('|')] = mode(platform, hasPrompt, dismissed);
for (const browser of ['safari', 'chrome', 'other'])
  for (const hasPrompt of [true, false])
    for (const dismissed of [true, false])
      out[['ios', hasPrompt, dismissed, browser].join('|')] = mode('ios', hasPrompt, dismissed, browser);
console.log(JSON.stringify(out));
""" % json.dumps(INSTALL_JS)
        expected = {
            "ios|true|true": "hidden", "ios|true|false": "prompt",
            "ios|false|true": "hidden", "ios|false|false": "ios-steps",
            "android|true|true": "hidden", "android|true|false": "prompt",
            "android|false|true": "hidden", "android|false|false": "android-steps",
            "desktop|true|true": "hidden", "desktop|true|false": "hidden",
            "desktop|false|true": "hidden", "desktop|false|false": "hidden",
            "standalone|true|true": "hidden", "standalone|true|false": "hidden",
            "standalone|false|true": "hidden", "standalone|false|false": "hidden",
            "ios|true|true|safari": "hidden", "ios|true|false|safari": "prompt",
            "ios|false|true|safari": "hidden", "ios|false|false|safari": "ios-steps",
            "ios|true|true|chrome": "hidden", "ios|true|false|chrome": "prompt",
            "ios|false|true|chrome": "hidden", "ios|false|false|chrome": "ios-chrome-steps",
            "ios|true|true|other": "hidden", "ios|true|false|other": "prompt",
            "ios|false|true|other": "hidden", "ios|false|false|other": "ios-other-steps",
        }
        self.assertEqual(json.loads(run_node(script)), expected)

    # 11
    def test_install_card_renders_and_survives_blocked_storage(self):
        script = (r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const SOURCE = %s;
const ANDROID_UA = %s;

"""+DOM_HARNESS+r"""
(async () => {
  // Android with a captured prompt: card shows, Install runs prompt().
  {
    const app = boot({userAgent: ANDROID_UA, storage: memoryStorage({})});
    assert.equal(app.dom.card.hidden, false, 'android steps card shows before the prompt');
    assert.deepEqual(strongTexts(app.dom.card), ['Install app', 'Add to Home screen']);
    const event = fakePrompt();
    app.fire('beforeinstallprompt', event);
    assert.equal(event.prevented, true);
    assert.equal(app.dom.card.hidden, false);
    const install = findButton(app.dom.card, 'Install');
    assert.ok(install, 'card has an Install button');
    assert.ok(findButton(app.dom.card, 'Not now'));
    const accountButton = app.dom.registry.get('install-account-btn');
    assert.ok(accountButton, 'account section has an Install button');
    assert.match(app.dom.status.textContent, /^Add Village Fairways to your Home Screen to open it like an app\.$/);
    await install.listeners.click();
    assert.equal(event.prompts, 1);
    assert.equal(findButton(app.dom.card, 'Install'), null, 'the used prompt is dropped');
    assert.equal(app.dom.registry.get('install-account-btn'), undefined);
    app.fire('appinstalled');
    assert.equal(app.dom.card.hidden, true);
    assert.equal(app.storage.data.vf_install_card_dismissed, '1', 'appinstalled stores the dismissal');
    assert.equal(app.dom.status.textContent, 'Installed. Open Fairways from your Home Screen.');
  }

  // Storage that throws on every access: the card still renders, and dismissing still hides it.
  {
    const blocked = {getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }};
    const app = boot({userAgent: ANDROID_UA, storage: blocked});
    assert.equal(app.dom.card.hidden, false, 'card shows with blocked storage');
    app.fire('beforeinstallprompt', fakePrompt());
    assert.ok(findButton(app.dom.card, 'Install'));
    findButton(app.dom.card, 'Not now').listeners.click();
    assert.equal(app.dom.card.hidden, true, 'dismissal holds for the page view');
  }

  // No localStorage object at all.
  {
    const app = boot({userAgent: ANDROID_UA, storage: undefined});
    assert.equal(app.dom.card.hidden, false);
  }

  // A stored dismissal hides the card, and Not now stores it.
  {
    const app = boot({userAgent: ANDROID_UA, storage: memoryStorage({vf_install_card_dismissed: '1'})});
    assert.equal(app.dom.card.hidden, true);
    const fresh = memoryStorage({});
    const second = boot({userAgent: ANDROID_UA, storage: fresh});
    findButton(second.dom.card, 'Not now').listeners.click();
    assert.equal(fresh.data.vf_install_card_dismissed, '1');
    assert.equal(second.dom.card.hidden, true);
  }

  // iPhone without a prompt shows the steps and a link to the guide section.
  {
    const app = boot({userAgent: %s, storage: memoryStorage({})});
    assert.equal(app.dom.card.hidden, false);
    assert.equal(app.dom.card.children[1].textContent, "In Safari, select Share , then Add to Home Screen. If you don't see Share, select More (three dots) first.");
    const bold = strongTexts(app.dom.card);
    for (const label of ['Share', 'More', 'Add to Home Screen']) assert.ok(bold.includes(label), label + ' is bold');
    assert.deepEqual(strongTexts(app.dom.status), ['Share', 'More', 'Share', 'Add to Home Screen']);
    const link = app.dom.card.children[2].children[0];
    assert.equal(link.getAttribute('href'), '/install#iphone');
    assert.equal(link.textContent, 'Show me how');
  }

  // Standalone hides the card.
  {
    const app = boot({userAgent: ANDROID_UA, storage: memoryStorage({}), standalone: true});
    assert.equal(app.dom.card.hidden, true);
    assert.equal(app.dom.status.textContent, "You're using the Home Screen app.");
  }
  console.log('ok');
})().catch((error) => { console.error(error); process.exit(1); });
""") % (json.dumps(INSTALL_JS), json.dumps(ANDROID_UA), json.dumps(IPHONE_UA))
        self.assertEqual(run_node(script).strip(), "ok")

    # 12
    def test_account_help_section_and_guides_are_linked(self):
        dialog = INDEX[INDEX.index('id="account-overlay"'):]
        heading = dialog.index('id="account-help-heading"')
        self.assertIn('id="install-account-status"', dialog)
        self.assertIn('<a href="/install">Home Screen install guide</a>', dialog)
        self.assertIn('<a href="/help">User guide</a>', dialog)
        logout = dialog.index('class="switch-user switch-user-btn"')
        self.assertLess(heading, logout)
        self.assertIn('id="install-card"', INDEX)
        self.assertIn('id="iphone-chrome"', self.get("/install").get_data(as_text=True))
        for path in ("/install", "/help"):
            response = self.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/"', response.get_data(as_text=True))

    # 13
    def test_guide_images_exist_and_are_small(self):
        total = 0
        for name in ("install.html", "help.html"):
            parser = _Images()
            parser.feed((ROOT / "templates" / name).read_text(encoding="utf-8"))
            self.assertTrue(parser.images, name)
            for image in parser.images:
                src = image.get("src") or ""
                self.assertTrue(src.startswith("/static/"), src)
                path = STATIC / src[len("/static/"):]
                self.assertTrue(path.is_file(), src)
                self.assertLess(path.stat().st_size, 250 * 1024, src)
                self.assertTrue((image.get("alt") or "").strip(), f"{src} needs alt text")
                self.assertTrue((image.get("width") or "").strip(), f"{src} needs width")
                self.assertTrue((image.get("height") or "").strip(), f"{src} needs height")
                total += 1
        self.assertGreaterEqual(total, 8)

    # 14
    def test_guide_prose_follows_house_style(self):
        banned = re.compile(r"\b(simply|just|easily|currently|click|tap|etc|now|new|above|below)\b|\be\.g\.|\bi\.e\.", re.I)

        def check(label, text):
            # On-screen labels the spec fixes are exempt: Not now and Book It!
            text = text.replace("Not now", "")
            self.assertNotIn(chr(0x2014), text, label)
            match = banned.search(text)
            self.assertIsNone(match, f"{label}: {match.group(0) if match else ''}")
            self.assertNotIn("click here", text.lower(), label)
            # The only exclamation marks allowed are in the on-screen label Book It!
            self.assertEqual(text.replace("Book It!", "").count("!"), 0, label)

        for name in ("install.html", "help.html", "offline.html"):
            check(name, visible_text((ROOT / "templates" / name).read_text(encoding="utf-8")))
        account = INDEX[INDEX.index('aria-labelledby="account-help-heading"'):]
        check("account help section", visible_text(account[:account.index("</section>")]))
        script = r"""
const vm = require('node:vm');
const SOURCE = %s;
const out = {status: [], cards: []};
const el = () => ({children: [], attrs: {}, className: '', _t: '', setAttribute(n, v) { this.attrs[n] = v; }, removeAttribute() {},
  appendChild(c) { this.children.push(c); return c; }, addEventListener() {}, insertAdjacentElement(w, c) { return c; }, remove() {},
  get textContent() { return this._t + this.children.map((c) => c.textContent).join(''); }, set textContent(v) { this._t = v; this.children = []; }});
for (const [ua, prompt] of [['iPhone', false], ['Android', false], ['Android', true], [%s, false], [%s, false]]) {
  const card = el(); const status = el(); const handlers = {};
  const window = {isSecureContext: true, navigator: {userAgent: ua, platform: 'x', maxTouchPoints: 0}, matchMedia: () => ({matches: false}),
    addEventListener(t, h) { handlers[t] = h; }, localStorage: {getItem: () => null, setItem() {}}};
  const document = {getElementById: (id) => ({'install-card': card, 'install-account-status': status}[id] || null),
    createElement: el, createElementNS: el, createTextNode: (t) => { const n = el(); n._t = t; return n; }};
  vm.runInContext(SOURCE, vm.createContext({window, document}));
  if (prompt) handlers.beforeinstallprompt({preventDefault() {}, prompt() {}, userChoice: Promise.resolve({})});
  out.cards.push(card.textContent);
}
const fn = (() => { const w = {addEventListener() {}, navigator: {}}; vm.runInContext(SOURCE, vm.createContext({window: w, document: {getElementById: () => null}})); return w.VFInstall.accountStatusText; })();
for (const platform of ['ios', 'android', 'desktop', 'standalone'])
  for (const hasPrompt of [true, false])
    for (const installed of [true, false]) out.status.push(fn(platform, hasPrompt, installed));
for (const browser of ['chrome', 'other', 'safari']) out.status.push(fn('ios', false, false, browser));
console.log(JSON.stringify(out));
""" % (json.dumps(INSTALL_JS), json.dumps(CHROME_IOS_UA), json.dumps(FIREFOX_IOS_UA))
        copy = json.loads(run_node(script))
        self.assertEqual(len(copy["cards"]), 5)
        self.assertEqual(len(copy["status"]), 19)
        for index, text in enumerate(copy["cards"] + copy["status"]):
            self.assertTrue(text.strip())
            check(f"install.js copy {index}", text)

    # 15 (T1)
    def test_header_leaves_room_for_the_status_bar(self):
        header_rules = re.findall(r"^\s*\.hdr\s*\{[^}]*\}", EXPERIENCE_CSS, re.M)
        self.assertGreaterEqual(len(header_rules), 2)
        for rule in header_rules:
            self.assertIn("env(safe-area-inset-top)", rule)
        wide = EXPERIENCE_CSS[EXPERIENCE_CSS.index("@media (min-width:760px) {\n  body { padding-bottom:44px; }"):]
        nav = re.search(r"\.app-nav\s*\{[^}]*\}", wide).group(0)
        self.assertIn("top:env(safe-area-inset-top)", nav)
        dialog = re.search(r"^\.account-dialog\s*\{[^}]*\}", EXPERIENCE_CSS, re.M).group(0)
        self.assertRegex(dialog, r"max-height:[^;]*env\(safe-area-inset-top\)")
        for selector in (".account-overlay", ".account-dialog", ".centered-overlay", ".course-modal"):
            rule = re.search(r"^" + re.escape(selector) + r"\s*\{[^}]*\}", EXPERIENCE_CSS, re.M)
            self.assertIsNotNone(rule, selector)
            self.assertIn("env(safe-area-inset-", rule.group(0), selector)
        self.assertRegex(EXPERIENCE_CSS, r"body::before\s*\{[^}]*z-index:90[^}]*\}")
        self.assertRegex(EXPERIENCE_CSS, r"body::before\s*\{[^}]*height:env\(safe-area-inset-top\)")

    def test_chrome_on_iphone_gets_chrome_steps(self):
        script = (r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const SOURCE = %s;
const ANDROID_UA = '';
""" + DOM_HARNESS + r"""
(async () => {
  for (const [ua, text, href, bold] of [
    [%s, "In Chrome, select Share  in the address bar, then Add to Home Screen.", '/install#iphone-chrome', ['Share', 'Add to Home Screen']],
    [%s, "Open your browser's Share menu, then select Add to Home Screen. If you don't see it, use Safari or Chrome.", '/install#iphone', ['Share', 'Add to Home Screen']],
  ]) {
    const app = boot({userAgent: ua, storage: memoryStorage({})});
    assert.equal(app.dom.card.hidden, false);
    assert.equal(app.dom.card.children[1].textContent, text);
    assert.deepEqual(strongTexts(app.dom.card), bold);
    const link = app.dom.card.children[2].children[0];
    assert.equal(link.getAttribute('href'), href);
    assert.equal(link.textContent, 'Show me how');
  }
  const chrome = boot({userAgent: %s, storage: memoryStorage({})});
  assert.equal(chrome.dom.status.textContent, "To add it to your Home Screen, select Share in Chrome's address bar, then Add to Home Screen.");
  assert.deepEqual(strongTexts(chrome.dom.status), ['Share', 'Add to Home Screen']);
  console.log('ok');
})().catch((error) => { console.error(error); process.exit(1); });
""") % (json.dumps(INSTALL_JS), json.dumps(CHROME_IOS_UA), json.dumps(FIREFOX_IOS_UA), json.dumps(CHROME_IOS_UA))
        self.assertEqual(run_node(script).strip(), "ok")

    # T3
    def test_install_card_lives_on_the_book_view(self):
        card = INDEX.index('id="install-card"')
        self.assertGreater(card, INDEX.index('id="view-book"'))
        self.assertLess(card, INDEX.index('id="sec-date"'))
        self.assertGreater(card, INDEX.index('<nav class="app-nav"'))

    # T4
    def test_update_check_reloads_only_when_safe(self):
        script = r"""
const vm = require('node:vm');
const w = {addEventListener() {}, navigator: {}};
vm.runInContext(%s, vm.createContext({window: w, document: {getElementById: () => null}}));
const f = w.VFInstall.shouldReloadForUpdate;
console.log(JSON.stringify([
  f('aaaaaaaaaaaa', 'aaaaaaaaaaaa', 600000, false),
  f('aaaaaaaaaaaa', 'bbbbbbbbbbbb', 599999, false),
  f('aaaaaaaaaaaa', 'bbbbbbbbbbbb', 600000, false),
  f('aaaaaaaaaaaa', 'bbbbbbbbbbbb', 900000, false),
  f('', 'bbbbbbbbbbbb', 600000, false),
  f('aaaaaaaaaaaa', '', 600000, false),
  f(null, 'bbbbbbbbbbbb', 600000, false),
  f('aaaaaaaaaaaa', null, 600000, false),
  f('aaaaaaaaaaaa', 'bbbbbbbbbbbb', 600000, true),
]));
""" % json.dumps(INSTALL_JS)
        self.assertEqual(json.loads(run_node(script)), [False, False, True, True, False, False, False, False, False])

    # T5
    def test_asset_version_endpoint(self):
        response = self.get("/api/version")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        version = response.get_json()["version"]
        self.assertRegex(version, r"^[0-9a-f]{12}$")
        page = (ROOT / "templates" / "index.html").read_bytes()
        digest = hashlib.sha256(page)
        markup = page.decode("utf-8")
        found = []
        for tag in re.findall(r"<(?:script|link)\b[^>]*>", markup):
            if tag.startswith("<script"):
                match = re.search(r'\bsrc="([^"]+)"', tag)
            else:
                match = re.search(r'\bhref="([^"]+)"', tag) if 'rel="stylesheet"' in tag else None
            if match and match.group(1).startswith("/static/"):
                found.append(match.group(1).split("?")[0])
        self.assertIn("/static/vendor/leaflet/leaflet.js", found)
        self.assertIn("/static/js/install.js", found)
        self.assertNotIn("/static/css/guide.css", found)
        for reference in found:
            digest.update((ROOT / reference.lstrip("/")).read_bytes())
        self.assertEqual(version, digest.hexdigest()[:12])

    # T7
    def test_raw_worker_source_is_not_public(self):
        self.assertEqual(self.get("/static/sw.js").status_code, 404)
        self.assertTrue((ROOT / "templates" / "sw.js").is_file())
        self.assertFalse((STATIC / "sw.js").exists())

    # T9
    def test_guide_pages_name_the_home_screen_app(self):
        for name in ("install.html", "help.html"):
            page = (ROOT / "templates" / name).read_text(encoding="utf-8")
            self.assertIn('<meta name="apple-mobile-web-app-capable" content="yes">', page, name)
            self.assertIn('<meta name="mobile-web-app-capable" content="yes">', page, name)
            self.assertIn('<meta name="apple-mobile-web-app-title" content="Fairways">', page, name)

    # G4
    def test_guide_pages_have_status_bar_backdrop(self):
        css = (STATIC / "css" / "guide.css").read_text(encoding="utf-8")
        rule = re.search(r"body::before\s*\{[^}]*\}", css)
        self.assertIsNotNone(rule)
        for part in ("position: fixed", "height: env(safe-area-inset-top)", "pointer-events: none", "z-index: 90"):
            self.assertIn(part, rule.group(0))
        self.assertRegex(css, r"@media print\s*\{[^}]*body::before\s*\{\s*display:\s*none;")

    # G3
    def test_update_reload_wiring(self):
        script = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const SOURCE = %s;
const MIN = 60000;

function boot({standalone = true, versions = [], firstFetchFails = false}) {
  const log = {reload: 0, register: [], fetches: 0};
  const docListeners = {};
  const winListeners = {};
  const present = {overlayShow: false, review: false, notice: null};
  const queue = versions.slice();
  const window = {
    isSecureContext: true,
    navigator: {userAgent: 'Mozilla/5.0 (iPhone)', platform: 'iPhone', maxTouchPoints: 5,
                serviceWorker: {register(url) { log.register.push(url); return Promise.resolve(); }}},
    matchMedia: () => ({matches: standalone}),
    location: {reload() { log.reload += 1; }},
    localStorage: {getItem: () => null, setItem() {}},
    addEventListener(type, handler) { winListeners[type] = handler; },
    fetch() {
      log.fetches += 1;
      if (firstFetchFails && log.fetches === 1) return Promise.reject(new Error('offline'));
      return Promise.resolve({ok: true, json: () => Promise.resolve({version: queue.shift()})});
    },
  };
  let now = 1000000;
  const document = {
    visibilityState: 'visible',
    addEventListener(type, handler) { docListeners[type] = handler; },
    getElementById(id) {
      if (id === 'rq-review') return present.review ? {} : null;
      if (id === 'myTimesNotice') return present.notice;
      return null;
    },
    querySelector(selector) { return selector === '.overlay.show' && present.overlayShow ? {} : null; },
    createElement: () => ({children: [], appendChild() {}, setAttribute() {}, removeAttribute() {}, addEventListener() {}}),
  };
  const context = vm.createContext({window, document, Date: {now: () => now}});
  // BOOKING_IN_FLIGHT and EXPERIENCE live in the same script scope as install.js, never on window.
  vm.runInContext("let BOOKING_IN_FLIGHT = false; const EXPERIENCE = {requestSubmissionInFlight: false, myTimesLoading: false};", context);
  vm.runInContext(SOURCE, context);
  const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
  return {
    log, present, context,
    async load() { winListeners.load(); await tick(); await tick(); },
    async away(minutes) {
      document.visibilityState = 'hidden'; docListeners.visibilitychange();
      now += minutes * MIN;
      document.visibilityState = 'visible'; docListeners.visibilitychange();
      await tick(); await tick(); await tick();
    },
  };
}

(async () => {
  const results = {};
  const run = async (name, options, prepare, minutes) => {
    const app = boot(options);
    await app.load();
    if (prepare) prepare(app);
    await app.away(minutes);
    results[name] = app.log.reload;
    return app;
  };
  const differ = {versions: ['aaaaaaaaaaaa', 'bbbbbbbbbbbb']};

  const first = boot(differ);
  await first.load();
  results.registerCalls = first.log.register.slice();

  await run('idle11', differ, null, 11);
  await run('idle9', differ, null, 9);
  await run('same11', {versions: ['aaaaaaaaaaaa', 'aaaaaaaaaaaa']}, null, 11);
  await run('booking', differ, (a) => vm.runInContext('BOOKING_IN_FLIGHT = true', a.context), 11);
  await run('submitting', differ, (a) => vm.runInContext('EXPERIENCE.requestSubmissionInFlight = true', a.context), 11);
  await run('myTimesLoading', differ, (a) => vm.runInContext('EXPERIENCE.myTimesLoading = true', a.context), 11);
  await run('overlay', differ, (a) => { a.present.overlayShow = true; }, 11);
  await run('review', differ, (a) => { a.present.review = true; }, 11);
  await run('noticeVisible', differ, (a) => { a.present.notice = {hasAttribute: () => false}; }, 11);
  await run('noticeHidden', differ, (a) => { a.present.notice = {hasAttribute: (n) => n === 'hidden'}; }, 11);
  await run('browserTab', {standalone: false, versions: ['aaaaaaaaaaaa', 'bbbbbbbbbbbb']}, null, 11);

  // First load fetch rejected: the first return only sets the baseline, the next return can reload.
  const flaky = boot({firstFetchFails: true, versions: ['aaaaaaaaaaaa', 'bbbbbbbbbbbb']});
  await flaky.load();
  await flaky.away(11);
  results.flakyFirstReturn = flaky.log.reload;
  await flaky.away(11);
  results.flakySecondReturn = flaky.log.reload;

  console.log(JSON.stringify(results));
})().catch((error) => { console.error(error); process.exit(1); });
""" % json.dumps(INSTALL_JS)
        self.assertEqual(json.loads(run_node(script)), {
            "registerCalls": ["/sw.js"],
            "idle11": 1, "idle9": 0, "same11": 0,
            "booking": 0, "submitting": 0, "myTimesLoading": 0,
            "overlay": 0, "review": 0, "noticeVisible": 0, "noticeHidden": 1,
            "browserTab": 0,
            "flakyFirstReturn": 0, "flakySecondReturn": 1,
        })

if __name__ == "__main__":
    unittest.main()
