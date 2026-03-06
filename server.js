const express = require('express');
const { chromium } = require('playwright');
const bodyParser = require('body-parser');
const cors = require('cors');
const fetch = require('node-fetch');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 8080;
const DEBUG_GOLF = process.env.DEBUG_GOLF === 'true';
const PERF_METRICS = process.env.PERF_METRICS !== 'false';
const USER_CONTEXT_TTL_MS = Number(process.env.USER_CONTEXT_TTL_MS || 10 * 60 * 1000);
const SESSION_CACHE_TTL_MS = Number(process.env.SESSION_CACHE_TTL_MS || 15 * 60 * 1000);
const TEE_SHEET_CACHE_TTL_MS = Number(process.env.TEE_SHEET_CACHE_TTL_MS || 2 * 60 * 1000);
const BUDDY_CACHE_TTL_MS = Number(process.env.BUDDY_CACHE_TTL_MS || SESSION_CACHE_TTL_MS);
const DEFAULT_USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36';

app.use(cors());
app.use(bodyParser.json());

// --- Global Error Handlers ---
process.on('unhandledRejection', (reason, promise) => {
    console.error('Unhandled Rejection at:', promise, 'reason:', reason);
});

process.on('uncaughtException', (err, origin) => {
    console.error(`Uncaught Exception: ${err.message}\nOrigin: ${origin}`);
});

// --- Global Browser & Session Management ---
let globalBrowser = null;
const sessionCache = new Map(); // username -> { cookies: [], pin: string, timestamp: Date }
const userContextCache = new Map(); // username -> { context, lastUsedMs }
const teeSheetCache = new Map(); // username -> { page, createdAt, requestedDateCompact, golferCount, courseType }
const buddyCache = new Map(); // username -> { buddies, courseTypes, timestamp }

function debugLog(...args) {
    if (DEBUG_GOLF) console.log(...args);
}

function createPerfTracker(label) {
    const enabled = PERF_METRICS;
    const startedAt = Date.now();
    let lastMark = startedAt;
    return {
        mark(step) {
            if (!enabled) return;
            const now = Date.now();
            console.log(`[PERF] ${label} ${step}: +${now - lastMark}ms (total ${now - startedAt}ms)`);
            lastMark = now;
        },
        done(status = 'ok') {
            if (!enabled) return;
            const now = Date.now();
            console.log(`[PERF] ${label} completed (${status}) in ${now - startedAt}ms`);
        }
    };
}

async function isContextUsable(context) {
    try {
        await context.cookies();
        return true;
    } catch {
        return false;
    }
}

async function getOrCreateUserContext(browser, username) {
    const now = Date.now();
    const cached = userContextCache.get(username);
    if (cached) {
        const isExpired = (now - cached.lastUsedMs) > USER_CONTEXT_TTL_MS;
        if (!isExpired && await isContextUsable(cached.context)) {
            cached.lastUsedMs = now;
            if (PERF_METRICS) {
                console.log(`[PERF] context:${username} reused`);
            }
            return cached.context;
        }
        try {
            await cached.context.close();
        } catch { }
        userContextCache.delete(username);
    }

    const context = await browser.newContext({ userAgent: DEFAULT_USER_AGENT });
    userContextCache.set(username, { context, lastUsedMs: now });
    if (PERF_METRICS) {
        console.log(`[PERF] context:${username} created`);
    }
    return context;
}

function touchUserContext(username) {
    const cached = userContextCache.get(username);
    if (cached) cached.lastUsedMs = Date.now();
}

function normalizeCourseTypeForCache(courseType) {
    return String(courseType || '').trim();
}

async function setCachedTeeSheet(username, entry) {
    const existing = teeSheetCache.get(username);
    if (existing && existing.page && !existing.page.isClosed() && existing.page !== entry.page) {
        await closePageResources(existing.page, 'tee-sheet-replace', { closeContext: false });
    }
    teeSheetCache.set(username, entry);
}

async function takeCachedTeeSheet(username, expected) {
    const entry = teeSheetCache.get(username);
    if (!entry) return null;
    teeSheetCache.delete(username);

    const isExpired = (Date.now() - entry.createdAt) > TEE_SHEET_CACHE_TTL_MS;
    const matchesDate = entry.requestedDateCompact === expected.requestedDateCompact;
    const matchesGolfers = entry.golferCount === expected.golferCount;
    const matchesCourseType = entry.courseType === expected.courseType;
    const isUsable = entry.page && !entry.page.isClosed();

    if (!isExpired && matchesDate && matchesGolfers && matchesCourseType && isUsable) {
        return entry.page;
    }

    if (PERF_METRICS) {
        const reasons = [
            isExpired && 'expired',
            !matchesDate && `date mismatch (cached=${entry.requestedDateCompact} req=${expected.requestedDateCompact})`,
            !matchesGolfers && `golfer count mismatch (cached=${entry.golferCount} req=${expected.golferCount})`,
            !matchesCourseType && `courseType mismatch (cached="${entry.courseType}" req="${expected.courseType}")`,
            !isUsable && 'page closed'
        ].filter(Boolean);
        console.log(`[PERF] tee-sheet cache miss: ${reasons.join(', ')}`);
    }

    if (entry.page && !entry.page.isClosed()) {
        await closePageResources(entry.page, 'tee-sheet-expired-or-mismatch', { closeContext: false });
    }
    return null;
}

// Initialize browser on startup
async function getBrowser() {
    if (globalBrowser && globalBrowser.isConnected()) {
        return globalBrowser;
    }
    console.log("Launching new global browser instance...");
    globalBrowser = await chromium.launch({
        headless: true,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage'
        ]
    });

    // Handle crashes
    globalBrowser.on('disconnected', () => {
        console.log("Global browser disconnected. Will relaunch on next request.");
        globalBrowser = null;
        userContextCache.clear();
        teeSheetCache.clear();
        buddyCache.clear();
    });

    return globalBrowser;
}


// Serve static files from the React app
app.use(express.static(path.join(__dirname, 'villages-frontend/dist')));

// Health check endpoint
app.get('/health', (req, res) => res.json({ status: 'ok' }));

// --- ENDPOINT: Weather ---
app.get('/weather', async (req, res) => {
    try {
        const { date } = req.query; // Expect YYYY-MM-DD
        const lat = 28.92;
        const lon = -81.97;
        const url = `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&daily=weathercode,temperature_2m_max,temperature_2m_min&temperature_unit=fahrenheit&timezone=auto`;

        const response = await fetch(url);
        const data = await response.json();

        if (!data.daily) throw new Error("Weather data not available");

        const dateIndex = data.daily.time.indexOf(date);
        const idx = dateIndex !== -1 ? dateIndex : 1; // Default to tomorrow if not found

        res.json({
            success: true,
            weather: {
                tempMax: Math.round(data.daily.temperature_2m_max[idx]),
                tempMin: Math.round(data.daily.temperature_2m_min[idx]),
                code: data.daily.weathercode[idx]
            }
        });
    } catch (error) {
        console.error("Weather Error:", error);
        res.status(500).json({ success: false, error: error.message });
    }
});

// --- HELPER: Navigation & Login Flow ---
async function setupFastPage(page) {
    // Playwright uses route() instead of setRequestInterception
    await page.route('**/*', (route) => {
        const request = route.request();
        const resourceType = request.resourceType();
        // Block unnecessary resources to reduce page load time per navigation
        if (['image', 'media', 'stylesheet', 'font'].includes(resourceType)) {
            route.abort();
        } else {
            route.continue();
        }
    });

    // Set timeout
    page.setDefaultTimeout(30000);
}

function parseRequestedDate(inputDate) {
    const raw = String(inputDate || '').trim();
    let year;
    let month;
    let day;

    // YYYY-MM-DD
    let match = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (match) {
        year = parseInt(match[1], 10);
        month = parseInt(match[2], 10);
        day = parseInt(match[3], 10);
    }

    // YYYYMMDD
    if (!match) {
        match = raw.match(/^(\d{4})(\d{2})(\d{2})$/);
        if (match) {
            year = parseInt(match[1], 10);
            month = parseInt(match[2], 10);
            day = parseInt(match[3], 10);
        }
    }

    // M/D/YYYY or MM/DD/YYYY
    if (!match) {
        match = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
        if (match) {
            month = parseInt(match[1], 10);
            day = parseInt(match[2], 10);
            year = parseInt(match[3], 10);
        }
    }

    if (!year || !month || !day || month < 1 || month > 12 || day < 1 || day > 31) {
        throw new Error(`Invalid date format "${inputDate}". Expected YYYY-MM-DD or YYYYMMDD.`);
    }

    const monthPadded = String(month).padStart(2, '0');
    const dayPadded = String(day).padStart(2, '0');

    return {
        year,
        month,
        day,
        compact: `${year}${monthPadded}${dayPadded}`,   // YYYYMMDD
        iso: `${year}-${monthPadded}-${dayPadded}`,     // YYYY-MM-DD
        mdyNoPad: `${month}/${day}/${year}`,            // M/D/YYYY
        mdyPad: `${monthPadded}/${dayPadded}/${year}`   // MM/DD/YYYY
    };
}

function normalizeGolferIds(rawGolfers) {
    if (!Array.isArray(rawGolfers)) return [];
    return rawGolfers
        .map(id => String(id || '').trim())
        .filter(Boolean);
}

function normalizeCourseForMatch(value) {
    return String(value || '')
        .toUpperCase()
        .replace(/\(\d+\)/g, '')
        .replace(/[^\w]/g, '');
}

async function closePageResources(page, label = '', options = {}) {
    const { closeContext = true } = options;
    if (!page) return;

    try {
        const context = page.context();
        if (context && closeContext) {
            await context.close();
            return;
        }
    } catch (contextError) {
        const suffix = label ? ` (${label})` : '';
        console.error(`Error during context cleanup${suffix}:`, contextError.message);
    }

    try {
        if (!page.isClosed()) {
            await page.close();
        }
    } catch (pageError) {
        const suffix = label ? ` (${label})` : '';
        console.error(`Error during page cleanup${suffix}:`, pageError.message);
    }
}

async function tryResumeActiveGolfSession(page, username, pin) {
    try {
        const resumeTargets = [
            'https://gis.thevillages.net/cgi-bin/glf100',
            'https://www.thevillages.net/golf/OpenGolf'
        ];
        let landed = false;
        for (const target of resumeTargets) {
            try {
                await page.goto(target, { waitUntil: 'domcontentloaded', timeout: 3000 });
                landed = true;
                break;
            } catch {
                // Try next URL.
            }
        }
        if (!landed) return false;

        const pageState = await page.evaluate(() => {
            const body = document.body?.innerText || '';
            return {
                hasPinField: !!document.querySelector('input[name="pinno"]'),
                hasReservationsLink: !!document.querySelector("a[href*='formin2']") || body.includes('Reservations-View Open Tee Times'),
                hasTeeTimeShell: body.includes('The Villages Tee Time System') && body.includes('LOGOUT')
            };
        });

        if (!(pageState.hasPinField || pageState.hasReservationsLink || pageState.hasTeeTimeShell)) {
            return false;
        }

        if (pageState.hasPinField) {
            await page.fill('input[name="pinno"]', '', { timeout: 1000 }).catch(() => null);
            await page.fill('input[name="pinno"]', pin, { timeout: 1000 }).catch(() => null);
            const continueButton = await page.$('input[name="Continue"]');
            if (!continueButton) return false;
            await Promise.all([
                page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 3000 }).catch(() => null),
                page.click('input[name="Continue"]', { timeout: 1000 }).catch(() => null)
            ]);
        }

        await page.waitForFunction(() => {
            const body = document.body?.innerText || '';
            return !!document.querySelector("a[href*='formin2']") || body.includes('Reservations-View Open Tee Times');
        }, { timeout: 4000 }).catch(() => null);

        const ready = await page.evaluate(() => {
            const body = document.body?.innerText || '';
            return !!document.querySelector("a[href*='formin2']") || body.includes('Reservations-View Open Tee Times');
        });

        if (!ready) return false;

        const cookies = await page.context().cookies();
        sessionCache.set(username, { cookies, pin, timestamp: new Date() });
        debugLog(`[${username}] Active golf session reused.`);
        return true;
    } catch (e) {
        debugLog(`[${username}] Active session resume failed: ${e.message}`);
        return false;
    }
}


async function performLoginAndNavigate(browser, username, password, pin, existingPage = null) {
    let page = existingPage;
    let context = null;
    const loginStart = Date.now();

    if (!page) {
        if (!browser) {
            browser = await getBrowser();
        }
        context = await browser.newContext({ userAgent: DEFAULT_USER_AGENT });
        page = await context.newPage();
        // Re-enable but use less aggressive blocking
        await setupFastPage(page);
    }

    // 0. Fast path for a reused authenticated browser context.
    const contextCookies = await page.context().cookies().catch(() => []);
    if (contextCookies.length > 0) {
        const activeResumeStart = Date.now();
        const resumed = await tryResumeActiveGolfSession(page, username, pin);
        if (PERF_METRICS) {
            console.log(`[PERF] login:${username} active-resume ${resumed ? 'hit' : 'miss'} in ${Date.now() - activeResumeStart}ms`);
        }
        if (resumed) return page;
    }


    // 1. Try Session Reuse
    const cached = sessionCache.get(username);
    const cacheAgeMs = cached?.timestamp ? (Date.now() - new Date(cached.timestamp).getTime()) : Number.POSITIVE_INFINITY;
    const canUseCachedSession = cached && cached.cookies.length > 0 && cacheAgeMs < SESSION_CACHE_TTL_MS;
    if (canUseCachedSession) {
        debugLog(`[${username}] Found cached session. Attempting direct navigation...`);
        const cookieResumeStart = Date.now();
        let cookieResumeHit = false;
        await page.context().addCookies(cached.cookies);

        try {
            // Try the gis subdomain directly first — faster than going through www redirect chain.
            // The session cookies include gis.thevillages.net cookies, so if still valid we land at glf100.
            const gisResumed = await (async () => {
                try {
                    await page.goto('https://gis.thevillages.net/cgi-bin/glf100', { waitUntil: 'domcontentloaded', timeout: 5000 });
                    const state = await page.evaluate(() => ({
                        hasPinField: !!document.querySelector('input[name="pinno"]'),
                        hasMenuLink: !!document.querySelector("a[href*='formin2']") || document.body.innerText.includes('Reservations-View Open Tee Times'),
                    }));
                    if (state.hasMenuLink) return 'at-menu';
                    if (state.hasPinField) return 'need-pin';
                    return false;
                } catch { return false; }
            })();

            if (gisResumed === 'at-menu') {
                debugLog(`[${username}] GIS session resumed — already at golf menu.`);
                cookieResumeHit = true;
                if (PERF_METRICS) console.log(`[PERF] login:${username} cookie-resume (gis-direct) hit in ${Date.now() - cookieResumeStart}ms`);
                return page;
            }

            if (gisResumed === 'need-pin') {
                await page.fill('input[name="pinno"]', '', { timeout: 1000 }).catch(() => null);
                await page.fill('input[name="pinno"]', pin || cached.pin, { timeout: 1000 }).catch(() => null);
                await Promise.all([
                    page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 5000 }).catch(() => null),
                    page.click('input[name="Continue"]', { timeout: 1000 }).catch(() => null)
                ]);
                const atMenu = await page.evaluate(() =>
                    !!document.querySelector("a[href*='formin2']") || document.body.innerText.includes('Reservations-View Open Tee Times')
                );
                if (atMenu) {
                    debugLog(`[${username}] GIS session resumed after PIN.`);
                    cookieResumeHit = true;
                    if (PERF_METRICS) console.log(`[PERF] login:${username} cookie-resume (gis-pin) hit in ${Date.now() - cookieResumeStart}ms`);
                    return page;
                }
            }

            // Fallback: go through www.thevillages.net
            await page.goto('https://www.thevillages.net/golf/OpenGolf', { waitUntil: 'domcontentloaded', timeout: 5000 });

            // Check if we are still logged in by looking for the PIN field or a session-only element
            const isLoggedIn = await page.evaluate(() => {
                return !!document.querySelector('input[name="pinno"]') || document.body.innerText.includes('Welcome');
            });

            if (isLoggedIn) {
                debugLog(`[${username}] Session resumed successfully via www fallback.`);
                // If we are on the PIN page, enter it
                if (await page.$('input[name="pinno"]')) {
                    await page.fill('input[name="pinno"]', '', { timeout: 1000 }).catch(() => null);
                    await page.fill('input[name="pinno"]', pin || cached.pin, { timeout: 1000 }).catch(() => null);
                    const continueButton = await page.$('input[name="Continue"]');
                    if (!continueButton) return page;
                    await Promise.all([
                        page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 3000 }).catch(() => null),
                        page.click('input[name="Continue"]', { timeout: 1000 }).catch(() => null)
                    ]);
                }
                cookieResumeHit = true;
                if (PERF_METRICS) {
                    console.log(`[PERF] login:${username} cookie-resume (www-fallback) hit in ${Date.now() - cookieResumeStart}ms`);
                }
                return page;
            }
            debugLog(`[${username}] Session expired or invalid. Falling back to full login.`);
        } catch (e) {
            debugLog(`[${username}] Direct navigation failed: ${e.message}. Falling back to full login.`);
        } finally {
            if (PERF_METRICS && !cookieResumeHit) {
                console.log(`[PERF] login:${username} cookie-resume miss in ${Date.now() - cookieResumeStart}ms`);
            }
        }
    } else if (cached && cacheAgeMs >= SESSION_CACHE_TTL_MS) {
        sessionCache.delete(username);
        debugLog(`[${username}] Cached session expired locally (${Math.round(cacheAgeMs / 1000)}s old). Skipping reuse.`);
    }

    // 2. Full Login Flow
    console.log(`[${username}] Navigating to home page for full login...`);
    await page.goto('https://www.thevillages.net/', { waitUntil: 'domcontentloaded' });

    // Login - use Playwright's fill() to fire proper DOM events
    await page.waitForSelector('#username', { state: 'visible', timeout: 15000 });
    await page.fill('#username', '');
    await page.fill('#username', username);
    await page.fill('#pw', '');
    await page.fill('#pw', password);


    const submitSelector = 'input[name="LogIN"]';
    await page.waitForSelector(submitSelector, { state: 'visible' });
    // Click login and wait for navigation to complete
    await Promise.all([
        page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
        page.click(submitSelector)
    ]);

    // Wait for success indicator
    try {
        await page.waitForFunction(() => {
            const body = document.body?.innerText || '';
            return body.includes('Sign Out') || body.includes('Invalid') || body.includes('incorrect') || body.includes('Login Failed');
        }, { timeout: 15000 });
    } catch (e) { }

    const content = await page.content();
    debugLog(`[${username}] Page content check - has 'Sign Out': ${content.includes('Sign Out')}, has 'OpenGolf': ${content.includes('OpenGolf')}`);

    if (content.includes('Sign Out') || content.includes('OpenGolf')) {
        console.log(`[${username}] Login successful.`);

        // Go to Golf
        console.log(`[${username}] Navigating to Golf...`);
        const golfTabSelector = "a[href*='OpenGolf']";
        await page.waitForSelector(golfTabSelector, { state: 'visible', timeout: 10000 });

        if (DEBUG_GOLF) {
            // DEBUG: Dump the GolfPush form that carries session data to gis.thevillages.net
            const golfPushInfo = await page.evaluate(() => {
                const form = document.querySelector('form[name="GolfPush"]') || document.forms['GolfPush'];
                if (!form) return { error: 'GolfPush form NOT FOUND', allFormNames: Array.from(document.forms).map(f => f.name || f.id || 'unnamed') };
                return {
                    action: form.action,
                    method: form.method,
                    target: form.target,
                    inputs: Array.from(form.querySelectorAll('input')).map(i => ({
                        name: i.name, type: i.type, value: i.value
                    })),
                    selects: Array.from(form.querySelectorAll('select')).map(s => ({
                        name: s.name, value: s.value, options: Array.from(s.options).map(o => o.value)
                    }))
                };
            });
            debugLog(`[${username}] GolfPush form structure:`, JSON.stringify(golfPushInfo, null, 2));
        }

        // Click Golf tab and wait for cross-domain navigation to gis.thevillages.net
        await Promise.all([
            page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
            page.click(golfTabSelector)
        ]);
        debugLog(`[${username}] After Golf click, URL: ${page.url()}`);

        // Enter PIN - Use Playwright's fill() to fire proper input events
        console.log(`[${username}] Entering PIN... (PIN length: ${pin ? pin.length : 'null'})`);
        try {
            await page.waitForSelector('input[name="pinno"]', { state: 'visible', timeout: 10000 });

            if (DEBUG_GOLF) {
                // Debug: Dump the FULL form structure including select elements
                const formInfo = await page.evaluate(() => {
                    const pinInput = document.querySelector('input[name="pinno"]');
                    const form = pinInput ? pinInput.closest('form') : null;
                    const submitBtn = document.querySelector('input[name="Continue"]');
                    // Also check for golfer select dropdown
                    const golferSelect = document.querySelector('select');
                    return {
                        pageUrl: window.location.href,
                        pageTitle: document.title,
                        formName: form ? form.name : 'no form',
                        formAction: form ? form.action : 'no action',
                        formMethod: form ? form.method : 'no method',
                        pinInputType: pinInput ? pinInput.type : 'no input',
                        pinInputMaxLength: pinInput ? pinInput.maxLength : 'n/a',
                        submitBtnType: submitBtn ? submitBtn.type : 'no submit btn',
                        submitBtnValue: submitBtn ? submitBtn.value : 'n/a',
                        submitBtnOnClick: submitBtn ? submitBtn.getAttribute('onclick') : 'n/a',
                        golferDropdown: golferSelect ? {
                            name: golferSelect.name,
                            value: golferSelect.value,
                            selectedText: golferSelect.options[golferSelect.selectedIndex]?.text || 'none',
                            optionCount: golferSelect.options.length,
                            allOptions: Array.from(golferSelect.options).map(o => ({ value: o.value, text: o.text, selected: o.selected }))
                        } : 'NO SELECT ELEMENT FOUND',
                        allFormInputs: form ? Array.from(form.querySelectorAll('input')).map(i => ({
                            name: i.name, type: i.type, value: i.value
                        })) : [],
                        allFormSelects: form ? Array.from(form.querySelectorAll('select')).map(s => ({
                            name: s.name, value: s.value, selectedText: s.options[s.selectedIndex]?.text
                        })) : [],
                        bodyTextSnippet: document.body?.innerText?.substring(0, 500) || 'empty'
                    };
                });
                debugLog(`[${username}] PIN form structure:`, JSON.stringify(formInfo, null, 2));
            }

            // Use Playwright's native fill method
            await page.fill('input[name="pinno"]', '');
            await page.fill('input[name="pinno"]', pin);

            // Verify PIN was actually entered
            const enteredPin = await page.evaluate(() => document.querySelector('input[name="pinno"]').value);
            const pinMatches = enteredPin === pin;
            debugLog(`[${username}] PIN entered verification - value length: ${enteredPin.length}, matches: ${pinMatches}`);
            debugLog(`[${username}] PIN entered: "${enteredPin}" vs expected: "${pin}"`);

            // Submit the form
            await Promise.all([
                page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
                page.click('input[name="Continue"]')
            ]);

            // Check for PIN rejection
            const afterPinContent = await page.evaluate(() => document.body?.innerText || '');
            if (afterPinContent.includes('Invalid Pin') || afterPinContent.includes('Id Not found')) {
                console.error(`[${username}] PIN WAS REJECTED. Page text: ${afterPinContent.substring(0, 300)}`);
                throw new Error("PIN was rejected by the server: Id Not found or Invalid Pin");
            }
            debugLog(`[${username}] PIN accepted. Current URL: ${page.url()}`);
        } catch (e) {
            throw new Error(`PIN entry failed: ${e.message}`);
        }

        // Wait for dynamic navigation links instead of fixed sleep.
        await page.waitForFunction(() => {
            const body = document.body?.innerText || '';
            return !!document.querySelector("a[href*='formin2']") || body.includes('Reservations-View Open Tee Times');
        }, { timeout: 4000 }).catch(() => null);

        // Cache the session
        const cookies = await page.context().cookies();
        sessionCache.set(username, { cookies, pin, timestamp: new Date() });
        debugLog(`[${username}] Session cached.`);
        if (PERF_METRICS) {
            console.log(`[PERF] login:${username} full-login in ${Date.now() - loginStart}ms`);
        }

        return page;
    } else {
        // Debug: Show snippet of what we actually got
        const bodyText = await page.evaluate(() => document.body?.innerText || 'No body element');
        console.error(`[${username}] Login check failed. Body text snippet: ${bodyText.substring(0, 500)}`);
        throw new Error('Login failed: Authentication timed out or rejected');
    }
}


async function navigateToResultsPage(page, username, password, pin, golferCount, courseType, requestedDate, golfers) {
    const navStartedAt = Date.now();
    let navLastMark = navStartedAt;
    const navMark = (step) => {
        if (!PERF_METRICS) return;
        const now = Date.now();
        console.log(`[PERF] nav:${username} ${step}: +${now - navLastMark}ms (total ${now - navStartedAt}ms)`);
        navLastMark = now;
    };

    const browser = await getBrowser();
    await performLoginAndNavigate(browser, username, password, pin, page);
    navMark('login ready');
    const golferIds = normalizeGolferIds(golfers);



    // (Legacy code removed)

    // 1. From Menu (glf100), Navigate to Reservations Options
    console.log("Navigating to Reservations Options...");
    const reservationsLink = page.getByText('Reservations-View Open Tee Times');
    if (await reservationsLink.count() > 0) {
        await Promise.all([
            reservationsLink.click(),
            page.waitForNavigation({ waitUntil: 'domcontentloaded' })
        ]);
    } else {
        // Fallback to older selector if text fails
        const link = await page.$("a[href*='formin2']");
        if (link) {
            await Promise.all([
                link.click(),
                page.waitForNavigation({ waitUntil: 'domcontentloaded' })
            ]);
        } else {
            console.log("Could not find Reservations link. Dumping page content...");
            const content = await page.content();
            console.log(content.substring(0, 2000)); // Log first 2k chars
            throw new Error("Cannot find Reservations link (formin2)");
        }
    }
    navMark('opened reservations options');

    // 2. From Options (glf109a), Click "Create New Reservation"
    console.log("Navigating to Create New Reservation...");
    await page.waitForSelector("a[href*='formin1']", { timeout: 15000 });
    await Promise.all([
        page.click("a[href*='formin1']"),
        page.waitForNavigation({ waitUntil: 'domcontentloaded' })
    ]);
    navMark('opened create reservation');

    // 3. On Details Page (glf109b): Num Golfers, Guests, Course Type
    console.log("Entering Reservation Details (Golfers, Guests)...");
    await page.waitForSelector('input[name="noofglf"]', { state: 'visible' });

    // Scrape course types here as a free side effect (same page)
    let scrapedCourseTypes = null;
    try {
        const courseTypeSelect = await page.$('select[name="crstype"]');
        if (courseTypeSelect) {
            scrapedCourseTypes = await page.evaluate(() => {
                const select = document.querySelector('select[name="crstype"]');
                if (!select) return [];
                return Array.from(select.options)
                    .filter(o => o.value && o.value !== '0')
                    .map(o => ({ id: o.value, name: o.innerText.trim() }));
            });
        }
    } catch (e) {
        debugLog(`[${username}] Could not scrape course types: ${e.message}`);
    }

    // Set Number of Golfers
    // Use max of requested golfers or 1
    const numGolfersToBook = Math.max(1, golferCount);
    await page.fill('input[name="noofglf"]', numGolfersToBook.toString());

    // Set Guests = No (Radio Button)
    // Find radio button with value 'n' or index 1
    // The subagent found input[name="anygsts"]
    const noGuestsRadio = await page.$('input[name="anygsts"][value="n"]');
    if (noGuestsRadio) await noGuestsRadio.click();

    // Set Course Type if provided
    if (courseType) {
        // Championship=1 (implied?), Executive=2? 
        // We'll try to select by value if we know it, or index.
        // For now, let's assume the user passes the ID from frontend.
        // If not, default handling.
        const courseTypeSelect = await page.$('select[name="crstype"]');
        if (courseTypeSelect) {
            // Check if option exists first
            const optionExists = await page.evaluate(({ ct, sel }) => {
                return !!document.querySelector(`select[name="${sel}"] option[value="${ct}"]`);
            }, { ct: courseType, sel: 'crstype' });

            if (optionExists) {
                await page.selectOption('select[name="crstype"]', courseType);
            }
        }
    }

    // Continue to Date Selection
    await Promise.all([
        page.click("a[href*='scrn2']"),
        page.waitForNavigation({ waitUntil: 'domcontentloaded' })
    ]);


    // 4. Date & Course Selection
    console.log("Selecting Date and Course...");
    await page.waitForSelector('select[name="playdate"]', { state: 'visible' });

    // Select Date from 'playdate' dropdown.
    // Accept YYYY-MM-DD and YYYYMMDD inputs.
    const parsedDate = parseRequestedDate(requestedDate);

    const dateOptions = await page.evaluate(() => {
        const select = document.querySelector('select[name="playdate"]');
        const options = Array.from(select.options);
        return options.map(opt => ({
            value: (opt.value || '').trim(),
            text: (opt.text || '').trim()
        }));
    });

    const dateOption = dateOptions.find(opt => {
        const value = opt.value;
        const text = opt.text;
        const valueDigits = value.replace(/\D/g, '');
        const textDigits = text.replace(/\D/g, '');

        // Direct matches against common representations.
        if (
            value.includes(parsedDate.compact) ||
            value.includes(parsedDate.iso) ||
            value.includes(parsedDate.mdyNoPad) ||
            value.includes(parsedDate.mdyPad) ||
            text.includes(parsedDate.compact) ||
            text.includes(parsedDate.iso) ||
            text.includes(parsedDate.mdyNoPad) ||
            text.includes(parsedDate.mdyPad)
        ) {
            return true;
        }

        // Numeric-only fallbacks (primarily for YYYYMMDD values).
        if (valueDigits === parsedDate.compact || textDigits.includes(parsedDate.compact)) {
            return true;
        }

        // Parse M/D/YYYY token from display text/value and normalize to YYYYMMDD.
        const mdySource = `${text} ${value}`;
        const mdyMatch = mdySource.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
        if (mdyMatch) {
            const mm = String(parseInt(mdyMatch[1], 10)).padStart(2, '0');
            const dd = String(parseInt(mdyMatch[2], 10)).padStart(2, '0');
            const yyyy = mdyMatch[3];
            return `${yyyy}${mm}${dd}` === parsedDate.compact;
        }

        return false;
    });

    if (!dateOption) {
        console.error(`Date ${requestedDate} (${parsedDate.compact}) not found in dropdown options.`);
        // Fallback: Dump options for debugging
        console.error("Available date options:", dateOptions);
        throw new Error(`Date ${requestedDate} is not available for booking.`);
    }

    await page.selectOption('select[name="playdate"]', dateOption.value);

    // Select Course (99 = View Open Times/All Courses)
    await page.selectOption('select[name="reqcs"]', '99'); // Default to All/Open

    await Promise.all([
        page.click('input[name="but2"]'), // Or but3? Subagent didn't specify, usually but2 'Continue'
        page.waitForNavigation({ waitUntil: 'domcontentloaded' })
    ]);
    navMark('selected date/course and opened golfer page');


    // 4. Handle Golfer Selection
    const buddySelector = 'select[name="buddy1"]';
    // For golferCount=1 the site skips the buddy page and lands on the tee sheet directly.
    // Use a short timeout so we don't block 8s waiting for a selector that will never appear.
    const buddyWaitMs = golferCount > 1 ? 8000 : 1500;
    const onBuddyPage = await page.waitForSelector(buddySelector, { timeout: buddyWaitMs }).catch(() => null);

    // Always scrape buddy list as a free side effect when on the buddy page
    let scrapedBuddies = null;
    if (onBuddyPage) {
        try {
            scrapedBuddies = await page.evaluate(({ sel }) => {
                const options = Array.from(document.querySelectorAll(`${sel} option`));
                return options
                    .filter(opt => opt.value && opt.value !== '0' && opt.text.trim() !== '')
                    .map(opt => ({ id: opt.value, name: opt.text.trim() }));
            }, { sel: buddySelector });
            debugLog(`[${username}] Scraped ${scrapedBuddies.length} buddies as side effect`);
        } catch (e) {
            debugLog(`[${username}] Could not scrape buddies as side effect: ${e.message}`);
        }
    }

    // Update buddy cache if we got fresh data
    if (scrapedBuddies !== null) {
        const cachedEntry = buddyCache.get(username);
        const courseTypesForCache = scrapedCourseTypes || (cachedEntry ? cachedEntry.courseTypes : []);
        buddyCache.set(username, { buddies: scrapedBuddies, courseTypes: courseTypesForCache, timestamp: Date.now() });
        if (PERF_METRICS) console.log(`[PERF] buddyCache populated as side effect of nav for ${username}`);
    }

    let resolvedGolferCount = golferCount;

    if (onBuddyPage && golferCount > 1) {
        console.log("Selecting buddies and populating form fields...");

        // Extract the user's golfer ID from the page
        const userId = await page.evaluate(() => {
            const form = Array.from(document.querySelectorAll('form')).find(f => f.querySelector('select[name="buddy1"]'));
            if (!form) return null;
            const idnum = form.querySelector('input[name="idnum"]');
            if (idnum && idnum.value) return idnum.value;
            const glfn = form.querySelector('input[name="glfn"]');
            if (glfn && glfn.value) return glfn.value;
            return null;
        });

        console.log(`User ID extracted: ${userId}`);
        const allGolferIds = Array.from(new Set(userId ? [userId, ...golferIds] : golferIds));
        resolvedGolferCount = Math.max(golferCount, allGolferIds.length);

        // Optimize Buddy Selection: Parallelize/Batch select operations
        await page.evaluate(({ golferIds }) => {
            const normalizedGolferIds = Array.isArray(golferIds) ? golferIds.filter(Boolean) : [];
            normalizedGolferIds.forEach((id, idx) => {
                const selector = `select[name="buddy${idx + 1}"]`;
                const el = document.querySelector(selector);
                if (el) {
                    el.value = id;
                    // Dispatch change event to ensure page logic triggers
                    const event = new Event('change', { bubbles: true });
                    el.dispatchEvent(event);
                }
            });
        }, { golferIds });


        await page.evaluate(({ golferIds }) => {
            const normalizedGolferIds = Array.isArray(golferIds) ? golferIds.filter(Boolean) : [];
            const form = Array.from(document.querySelectorAll('form')).find(f => f.querySelector('select[name="buddy1"]'));
            if (!form) return;
            let glfersInput = form.querySelector('input[name="glfers"]');
            if (!glfersInput) {
                glfersInput = document.createElement('input');
                glfersInput.type = 'hidden';
                glfersInput.name = 'glfers';
                form.appendChild(glfersInput);
            }
            glfersInput.value = normalizedGolferIds.join(',');
            normalizedGolferIds.forEach((id, idx) => {
                const name = `glfers${idx + 1}`;
                let input = form.querySelector(`input[name="${name}"]`);
                if (!input) {
                    input = document.createElement('input');
                    input.type = 'hidden';
                    input.name = name;
                    form.appendChild(input);
                }
                input.value = id;
            });
            let btnInput = form.querySelector('input[name="button"]');
            if (!btnInput) {
                btnInput = document.createElement('input');
                btnInput.type = 'hidden';
                btnInput.name = 'button';
                form.appendChild(btnInput);
            }
            btnInput.value = 'Submit';
        }, allGolferIds);

        console.log("Submitting form with populated fields...");
        await Promise.all([
            page.click('input[name="but3"]'),
            page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 10000 })
        ]);
        navMark('submitted golfers and opened tee sheet');
    }

    navMark('navigateToResultsPage done');
    return { page, resolvedGolferCount, buddies: scrapedBuddies, courseTypes: scrapedCourseTypes };
}


// --- ENDPOINT: Fetch Buddies & Course Types ---
// Prefer calling /search first — it populates this cache as a free side effect.
// This endpoint is kept for explicit refreshes or pre-search initialization.
app.post('/fetch-buddies', async (req, res) => {
    const { username, password, pin } = req.body;
    console.log(`Fetching data for user: ${username}`);
    const perf = createPerfTracker(`fetch-buddies:${username}`);

    // Serve from cache when available and fresh (populated by /search automatically)
    const cachedBuddies = buddyCache.get(username);
    if (cachedBuddies) {
        const age = Date.now() - cachedBuddies.timestamp;
        if (age < BUDDY_CACHE_TTL_MS) {
            console.log(`[${username}] Returning cached buddies (age ${Math.round(age / 1000)}s)`);
            perf.done('cache-hit');
            return res.json({ success: true, buddies: cachedBuddies.buddies, courseTypes: cachedBuddies.courseTypes });
        }
        buddyCache.delete(username);
    }

    // Cache miss — don't do a slow navigation just for buddies.
    // /search populates the buddy cache as a side effect, so return empty now
    // and the frontend will receive buddies in the next /search response.
    perf.done('cache-miss-fast-return');
    return res.json({ success: true, buddies: [], courseTypes: [] });

    let browser = null;
    let page = null;
    try {
        browser = await getBrowser();
        const context = await getOrCreateUserContext(browser, username);
        page = await context.newPage();
        await setupFastPage(page);
        page = await performLoginAndNavigate(browser, username, password, pin, page);
        perf.mark('login+pin complete');


        // Navigate to Reservations -> Create New
        console.log("Navigating to Reservations...");
        try {
            // Use text-based selector matching actual website navigation
            await page.waitForSelector("text=Reservations-View Open Tee Times", { timeout: 8000 });
        } catch (e) {
            // Debug: Capture page state if selector not found
            const url = page.url();
            const content = await page.content();
            console.error(`ERROR: Reservations link not found. Current URL: ${url}`);
            console.error(`Page content snippet: ${content.substring(0, 1000)}...`);

            // Check if we're on the right page
            const hasReservations = await page.evaluate(() => document.body?.innerText.includes('Reservations'));
            console.error(`Page has 'Reservations' text: ${hasReservations}`);

            // List ALL links to see what's available
            const allLinks = await page.evaluate(() => {
                return Array.from(document.querySelectorAll('a[href]'))
                    .map(a => ({ text: a.innerText.trim(), href: a.href }));
            });
            console.error(`All links on page: ${JSON.stringify(allLinks, null, 2)}`);

            // Take screenshot for debugging
            try {
                await page.screenshot({ path: '/tmp/glf100_debug.png', fullPage: true });
                console.error('Screenshot saved to /tmp/glf100_debug.png');
            } catch (screenshotErr) {
                console.error('Failed to capture screenshot:', screenshotErr.message);
            }

            throw new Error(`Navigation failed: Cannot find Reservations-View Open Tee Times link. Current URL: ${url}`);
        }
        await Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded' }), page.click("text=Reservations-View Open Tee Times")]);

        await page.waitForSelector("text=Create New Reservation");
        await Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded' }), page.click("text=Create New Reservation")]);
        perf.mark('navigated to create reservation');


        // --- SCRAPE COURSE TYPE OPTIONS FROM GOLFER COUNT PAGE ---
        let courseTypes = [];
        try {
            const courseTypeSelector = 'select[name="crstype"]';
            await page.waitForSelector(courseTypeSelector, { timeout: 5000 });
            courseTypes = await page.evaluate(({ sel }) => {
                const select = document.querySelector(sel);
                if (!select) return [];
                const options = Array.from(select.options);
                return options
                    .filter(o => o.value && o.value !== '0')
                    .map(o => ({
                        id: o.value,
                        name: o.innerText.trim()
                    }));
            }, { sel: courseTypeSelector });
        } catch (e) {
            console.log("Error scraping course types:", e.message);
        }

        // Enter 1 Golfer - Instant
        await page.waitForSelector('input[name="noofglf"]');
        await page.evaluate(() => {
            document.querySelector('input[name="noofglf"]').value = '1';
        });

        await page.evaluate(() => {
            const guests = document.querySelectorAll("input[name='anygsts']");
            if (guests.length > 1) guests[1].click();
        });

        await Promise.all([page.click("a[href*='scrn2']"), page.waitForNavigation({ waitUntil: 'domcontentloaded' })]);


        // Select Date (Tomorrow) & Default Course to get to buddy screen
        await page.waitForSelector('select[name="playdate"]');
        await page.evaluate(() => {
            const select = document.querySelector('select[name="playdate"]');
            if (select.options.length > 1) select.selectedIndex = 1;
        });

        await page.selectOption('select[name="reqcs"]', '99'); // View Open Times by Course

        await Promise.all([
            page.click('input[name="but2"]'),
            page.waitForNavigation({ waitUntil: 'domcontentloaded' })
        ]);


        // --- NOW ON BUDDY SELECTION PAGE (glf109c) ---
        // SCRAPE BUDDIES
        console.log("Scraping buddy list...");
        const buddySelector = 'select[name="buddy1"]';
        let buddies = [];
        try {
            await page.waitForSelector(buddySelector, { timeout: 5000 });
            buddies = await page.evaluate(({ sel }) => {
                const options = Array.from(document.querySelectorAll(`${sel} option`));
                return options
                    .filter(opt => opt.value && opt.value !== '0' && opt.text.trim() !== '')
                    .map(opt => ({
                        id: opt.value,
                        name: opt.text.trim()
                    }));
            }, { sel: buddySelector });
        } catch (e) {
            console.log("Buddy selector not found. Returning empty buddies.");
        }

        console.log(`Found ${buddies.length} buddies and ${courseTypes.length} course types.`);
        buddyCache.set(username, { buddies, courseTypes, timestamp: Date.now() });
        res.json({ success: true, buddies, courseTypes });
        perf.done('ok');

    } catch (error) {
        console.error("Fetch Data Error:", error);
        perf.done('error');
        res.status(500).json({ success: false, error: error.message });
    } finally {
        touchUserContext(username);
        await closePageResources(page, 'fetch', { closeContext: false });
    }
});


// --- ENDPOINT: Search Tee Times ---
app.post('/search', async (req, res) => {
    const { username, password, pin, date: requestedDate, golfers, courseType } = req.body;
    const golferIds = normalizeGolferIds(golfers);
    const golferCount = golferIds.length > 0 ? golferIds.length : 1;
    const perf = createPerfTracker(`search:${username}`);

    console.log(`Search request: ${username} | Date: ${requestedDate} | Golfers: ${golferCount}`);

    let page = null;
    let keepPageOpenForBooking = false;
    try {
        const requestedDateCompact = parseRequestedDate(requestedDate).compact;
        const browser = await getBrowser();
        const context = await getOrCreateUserContext(browser, username);
        page = await context.newPage();
        await setupFastPage(page);

        page.on('dialog', async dialog => {
            console.log(`Dialog detected: ${dialog.message()}`);
            await dialog.dismiss();
        });

        if (DEBUG_GOLF) {
            page.on('console', msg => console.log('PAGE LOG:', msg.text()));
        }

        const { page: finalPage, resolvedGolferCount, buddies: searchBuddies, courseTypes: searchCourseTypes } = await navigateToResultsPage(page, username, password, pin, golferCount, courseType, requestedDate, golferIds);
        perf.mark('navigated to tee sheet');


        console.log(`Scraping results from page: ${finalPage.url()}`);

        const rawTeeTimes = await finalPage.evaluate(() => {
            const results = [];
            const normalize = (s) => (s || '').replace(/\s+/g, ' ').trim();

            const tables = Array.from(document.querySelectorAll('table'));
            const targetTable = tables.find(tbl => /number available/i.test(tbl.innerText) && /course/i.test(tbl.innerText) && /time/i.test(tbl.innerText));
            const rows = targetTable ? Array.from(targetTable.querySelectorAll('tr')) : Array.from(document.querySelectorAll('table tr'));

            let timeIndex = -1;
            let courseIndex = -1;
            let availabilityIndex = -1;
            const headerRow = rows.find(row => /number available/i.test(row.innerText) && /time/i.test(row.innerText));
            if (headerRow) {
                const headerCells = Array.from(headerRow.querySelectorAll('th,td')).map(cell => normalize(cell.innerText).toLowerCase());
                timeIndex = headerCells.findIndex(h => /\btime\b/.test(h));
                courseIndex = headerCells.findIndex(h => /course/.test(h));
                availabilityIndex = headerCells.findIndex(h => /number available|available/.test(h));
            }

            rows.forEach(row => {
                const selectElement = Array.from(row.querySelectorAll('a, input[type="button"], input[type="submit"]'))
                    .find(el => /select/i.test(el.innerText || el.value || ''));

                if (!selectElement) return;

                const cells = Array.from(row.querySelectorAll('td'))
                    .map(td => normalize(td.innerText))
                    .filter(Boolean);

                const rowText = normalize(row.innerText);
                const indexedTime = (timeIndex >= 0 && cells[timeIndex]) ? cells[timeIndex] : '';
                const timeMatch = (indexedTime || rowText).match(/\b(\d{1,2}:\d{2})\b/);
                const time = timeMatch ? timeMatch[1] : null;

                const courseName = (courseIndex >= 0 && cells[courseIndex])
                    ? cells[courseIndex]
                    : (cells.find(c => /\/\s*hole|\bhole\b/i.test(c)) || '');

                let slots = 0;
                if (availabilityIndex >= 0 && cells[availabilityIndex]) {
                    const n = parseInt(cells[availabilityIndex].match(/\d+/)?.[0] || '', 10);
                    slots = Number.isNaN(n) ? 0 : n;
                } else {
                    // Fallback when headers are missing: pick the numeric cell nearest to the course/time cluster.
                    const numericCells = cells
                        .map(cell => parseInt(cell.match(/^\d+$/)?.[0] || '', 10))
                        .filter(n => !Number.isNaN(n) && n >= 0 && n <= 4);
                    if (numericCells.length > 0) slots = numericCells[0];
                }

                let bookingId = '';
                if (selectElement.tagName === 'A') {
                    bookingId = selectElement.href;
                } else {
                    bookingId = selectElement.getAttribute('onclick') || selectElement.name || 'button-booking';
                }

                if (time && courseName && bookingId) {
                    results.push({ course: courseName, time, slots, bookingId });
                }
            });
            return results;
        });

        const filteredTimes = rawTeeTimes.filter(t => t.slots >= resolvedGolferCount);
        console.log(`Total found: ${rawTeeTimes.length}, Filtered: ${filteredTimes.length}`);
        perf.mark('scraped and filtered tee times');

        // Include buddies/courseTypes scraped as a side effect of navigation
        const cachedBuddyData = buddyCache.get(username);
        res.json({
            success: true,
            count: filteredTimes.length,
            data: filteredTimes,
            buddies: searchBuddies ?? cachedBuddyData?.buddies ?? [],
            courseTypes: searchCourseTypes ?? cachedBuddyData?.courseTypes ?? []
        });
        await setCachedTeeSheet(username, {
            page: finalPage,
            createdAt: Date.now(),
            requestedDateCompact,
            golferCount,
            courseType: normalizeCourseTypeForCache(courseType)
        });
        keepPageOpenForBooking = true;
        perf.mark('cached tee sheet for booking');
        perf.done('ok');
    } catch (error) {
        console.error("Search Error:", error);
        perf.done('error');
        res.status(500).json({ success: false, error: error.message });
    } finally {
        touchUserContext(username);
        if (!keepPageOpenForBooking) {
            await closePageResources(page, 'search', { closeContext: false });
        }
    }
});


// --- ENDPOINT: Book Tee Time ---
app.post('/book', async (req, res) => {
    const { username, password, pin, date, golfers, courseType, bookingId, time, course } = req.body;
    const golferIds = normalizeGolferIds(golfers);
    const perf = createPerfTracker(`book:${username}`);

    // Validate required fields
    if (!date || !username || !password || !pin) {
        return res.status(400).json({
            success: false,
            error: 'Missing required fields: date, username, password, and pin are required'
        });
    }

    const golferCount = golferIds.length > 0 ? golferIds.length : 1;

    const mem = process.memoryUsage();
    console.log(`[BOOK] Request start: ${username} | ${time} at ${course} | Date: ${date} | Golfers: ${golferCount}`);
    console.log(`[BOOK] Memory Usage: RSS: ${(mem.rss / 1024 / 1024).toFixed(2)} MB, Heap: ${(mem.heapUsed / 1024 / 1024).toFixed(2)} MB`);

    let browser = null;
    let page = null;
    try {
        // Reach the results page
        const formattedDate = date.replace(/-/g, '');
        const cachedTeeSheetPage = await takeCachedTeeSheet(username, {
            requestedDateCompact: formattedDate,
            golferCount,
            courseType: normalizeCourseTypeForCache(courseType)
        });

        let finalPage = null;
        if (cachedTeeSheetPage) {
            page = cachedTeeSheetPage;
            finalPage = cachedTeeSheetPage;
            console.log(`[BOOK] Using cached tee sheet page for ${formattedDate}.`);
            perf.mark('reused cached tee sheet');
        } else {
            browser = await getBrowser();
            const context = await getOrCreateUserContext(browser, username);
            page = await context.newPage();
            await setupFastPage(page);

            page.on('dialog', async dialog => {
                console.log(`Booking Dialog: ${dialog.message()}`);
                await dialog.dismiss();
            });
            if (DEBUG_GOLF) {
                page.on('console', msg => console.log('BOOKING PAGE LOG:', msg.text()));
            }

            console.log(`[BOOK] Navigating to results page for ${formattedDate}...`);
            const navResult = await navigateToResultsPage(page, username, password, pin, golferCount, courseType, formattedDate, golferIds);
            finalPage = navResult.page;
            perf.mark('navigated to tee sheet');
        }


        // Execute the booking javascript
        console.log(`[BOOK] Executing booking action: ${bookingId}`);
        const selectedFormIndexMatch = String(bookingId || '').match(/formin(\d+)/i);
        const selectedRowIndex = selectedFormIndexMatch ? parseInt(selectedFormIndexMatch[1], 10) : null;
        if (bookingId.startsWith('javascript:')) {
            const script = bookingId.replace('javascript:', '');
            await finalPage.evaluate(({ s }) => eval(s), { s: script });
        } else {
            console.log(`[BOOK] Navigating to: ${bookingId}`);
            await finalPage.goto(bookingId, { waitUntil: 'domcontentloaded', timeout: 12000 });
        }

        // Wait for final confirmation
        console.log("Waiting for confirmation page...");
        const finalButtonSelector = 'input[type="submit"], input[name="but1"], input[value="Submit"]';
        const hasButton = await finalPage.waitForSelector(finalButtonSelector, { timeout: 10000 }).catch(() => null);

        if (hasButton) {
            console.log(`On confirmation page. Target: ${time} at ${course}`);

            // 1. Identify rows and fields to fill using hidden fields (Source of Truth)
            const allocationPlan = await finalPage.evaluate(({ targetTime, targetCourse, totalIds, selectedRowIndex }) => {
                const targetForm = Array.from(document.querySelectorAll('form')).find(f => f.querySelector('input[name="noofglf"]')) || document.forms[0];
                if (!targetForm) return [];

                const exacttStr = (targetForm.querySelector('input[name="exactt"]') || {}).value || "";
                const crshlStr = (targetForm.querySelector('input[name="crshl"]') || {}).value || "";

                console.log(`Target: ${targetTime} at ${targetCourse}`);
                console.log(`Form data - exactt: ${exacttStr}, crshl length: ${crshlStr.length}`);

                // Course criteria normalization
                const normalize = (s) => String(s || '').toUpperCase().replace(/\(\d+\)/g, '').replace(/[^\w]/g, '');
                const targetCourseRaw = String(targetCourse || '');
                const courseParts = targetCourseRaw.split(',');
                const clubToken = normalize(courseParts[0] || '');
                const subCourseToken = normalize((courseParts[1] || '').replace(/\(\d+\)/g, ''));
                const normalizedCrshl = normalize(crshlStr);
                const preferredToken = subCourseToken || clubToken;
                const requireCourseMatch = !!preferredToken && normalizedCrshl.includes(preferredToken);

                const plan = [];
                let remaining = totalIds;
                const cmpTime = String(targetTime || '').replace(':', '').padStart(4, '0');
                const exactChunkSize = exacttStr.includes(':') ? 5 : 4;
                const mode = requireCourseMatch ? 'strict' : (preferredToken ? 'blocked' : 'time-only fallback');
                console.log(`Course matching mode: ${mode} (token="${preferredToken || 'none'}")`);
                const targetTimeInt = parseInt(cmpTime, 10);

                const getRowAvailable = (row) => {
                    if (!row) return 1;
                    const cellTexts = Array.from(row.querySelectorAll('td'))
                        .map(td => td.innerText.trim())
                        .filter(Boolean);
                    const numericFromCells = cellTexts
                        .filter(text => /^\d+$/.test(text))
                        .map(n => parseInt(n, 10))
                        .filter(n => !Number.isNaN(n) && n >= 0 && n <= 4);
                    if (numericFromCells.length > 0) return numericFromCells[0];

                    const rowText = row.innerText.trim();
                    // Fallbacks (avoid time's minutes by preferring small slot counts).
                    const matches = Array.from(rowText.matchAll(/\b(\d+)\b/g))
                        .map(m => parseInt(m[1], 10))
                        .filter(n => !Number.isNaN(n) && n >= 0 && n <= 4);
                    return matches.length > 0 ? matches[0] : 1;
                };

                // Primary strategy: use visible allocation rows directly.
                const visibleCandidates = [];
                const tableRows = Array.from(targetForm.querySelectorAll('tr'));
                for (const row of tableRows) {
                    const field = row.querySelector('input[name^="allo"]:not([type="hidden"]), select[name^="allo"]');
                    if (!field) continue;
                    if (field.tagName !== 'SELECT' && field.readOnly) continue;

                    const text = row.innerText.toUpperCase();
                    const normalizedRowText = normalize(text);
                    const rowTimeMatch = text.match(/\b(\d{1,2}):(\d{2})\b/);
                    const rowTimeInt = rowTimeMatch
                        ? parseInt(`${parseInt(rowTimeMatch[1], 10)}${rowTimeMatch[2]}`, 10)
                        : Number.NaN;
                    const timeMatches = !Number.isNaN(rowTimeInt) && rowTimeInt === targetTimeInt;
                    if (!timeMatches) continue;
                    if (requireCourseMatch && !normalizedRowText.includes(preferredToken)) continue;

                    const rowAvailable = getRowAvailable(row);
                    visibleCandidates.push({ name: field.name, available: rowAvailable });
                }
                if (visibleCandidates.length > 0) {
                    for (const candidate of visibleCandidates) {
                        if (remaining <= 0) break;
                        const toAlloc = Math.min(remaining, candidate.available);
                        if (toAlloc > 0) {
                            console.log(`Visible row match: field=${candidate.name}, available=${candidate.available}`);
                            plan.push({ name: candidate.name, count: String(toAlloc) });
                            remaining -= toAlloc;
                        }
                    }
                    if (remaining <= 0) return plan;
                }

                // First choice: allocate using the selected row index from bookingId (formin###).
                if (selectedRowIndex && Number.isInteger(selectedRowIndex) && selectedRowIndex > 0) {
                    const selectedFieldName = `allo${selectedRowIndex}`;
                    const selectedField = targetForm.querySelector(`input[name="${selectedFieldName}"], select[name="${selectedFieldName}"]`);
                    const selectedEditable = selectedField && (selectedField.tagName === 'SELECT' || !selectedField.readOnly);
                    if (selectedEditable) {
                        const row = selectedField.closest('tr');
                        const rowAvailable = getRowAvailable(row);
                        const toAlloc = Math.min(remaining, rowAvailable);
                        if (toAlloc > 0) {
                            console.log(`Using selected row ${selectedRowIndex} from bookingId. Field: ${selectedFieldName}, Available: ${rowAvailable}`);
                            plan.push({ name: selectedFieldName, count: String(toAlloc) });
                            remaining -= toAlloc;
                        }
                    }
                    if (remaining <= 0) return plan;
                }

                // Safety guard: if user selected a course token, but this allocation page doesn't contain it,
                // do not fall back to time-only matching because that can book the wrong course.
                if (preferredToken && !requireCourseMatch) {
                    console.log(`Requested course token "${preferredToken}" not found in allocation map.`);
                    return [];
                }

                // Loop through exactt chunks (supports HHMM or HH:MM formats)
                for (let i = 0; i < exacttStr.length; i += exactChunkSize) {
                    if (remaining <= 0) break;

                    const rowTimeRaw = exacttStr.substring(i, i + exactChunkSize);
                    const rowTime = rowTimeRaw.replace(':', '').padStart(4, '0');

                    if (rowTime === cmpTime) {
                        const courseIdx = (i / exactChunkSize);
                        const rowCourseText = crshlStr.substring(courseIdx * 25, (courseIdx + 1) * 25).trim().toUpperCase();

                        console.log(`Row ${courseIdx + 1}: time=${rowTime}, course text="${rowCourseText}"`);

                        // Match if course matches
                        if (!requireCourseMatch || normalize(rowCourseText).includes(preferredToken) || preferredToken.includes(normalize(rowCourseText))) {
                            const fieldName = `allo${courseIdx + 1}`;
                            const field = targetForm.querySelector(`input[name="${fieldName}"], select[name="${fieldName}"]`);
                            const isEditable = field && (field.tagName === 'SELECT' || !field.readOnly);
                            if (isEditable) {
                                // Extract available from label next to field or row
                                const row = field.closest('tr');
                                const rowAvailable = getRowAvailable(row);

                                console.log(`Matched! Field: ${fieldName}, Available: ${rowAvailable}`);

                                const toAlloc = Math.min(remaining, rowAvailable);
                                if (toAlloc > 0) {
                                    plan.push({ name: fieldName, count: String(toAlloc) });
                                    remaining -= toAlloc;
                                }
                            } else {
                                console.log(`Matched row ${courseIdx + 1} but allocation field ${fieldName} is missing or read-only.`);
                            }
                        }
                    }
                }

                // Fallback: If plan is empty, try a broader search
                if (plan.length === 0) {
                    console.log("No specific row matched via hidden fields. Trying broad row search...");
                    const rows = Array.from(document.querySelectorAll('tr'));
                    const cleanTargetTime = String(targetTime || '').replace(/:/g, '').padStart(4, '0');
                    const compactTargetTime = String(parseInt(cleanTargetTime, 10));
                    const targetTimeIntFallback = parseInt(cleanTargetTime, 10);

                    for (const row of rows) {
                        if (remaining <= 0) break;
                        const text = row.innerText.toUpperCase();
                        const cleanText = text.replace(/[\s:]+/g, '');
                        const normalizedRowText = normalize(text);
                        const rowTimeMatch = text.match(/\b(\d{1,2}):(\d{2})\b/);
                        const rowTimeInt = rowTimeMatch
                            ? parseInt(`${parseInt(rowTimeMatch[1], 10)}${rowTimeMatch[2]}`, 10)
                            : Number.NaN;
                        const timeMatches = !Number.isNaN(rowTimeInt)
                            ? rowTimeInt === targetTimeIntFallback
                            : (cleanText.includes(cleanTargetTime) || cleanText.includes(compactTargetTime));

                        if (timeMatches && (!requireCourseMatch || normalizedRowText.includes(preferredToken))) {
                            const field = row.querySelector('input:not([type="hidden"]), select');
                            if (field && !field.readOnly) {
                                const rowAvailable = getRowAvailable(row);

                                console.log(`Broad Match Row: field=${field.name}, Available=${rowAvailable}`);

                                const toAlloc = Math.min(remaining, rowAvailable);
                                if (toAlloc > 0) {
                                    plan.push({ name: field.name, count: String(toAlloc) });
                                    remaining -= toAlloc;
                                }
                            }
                        }
                    }
                }

                return plan;
            }, { targetTime: time, targetCourse: course, totalIds: golferCount, selectedRowIndex });

            console.log(`Allocation plan: ${JSON.stringify(allocationPlan)}`);
            if (!Array.isArray(allocationPlan) || allocationPlan.length === 0) {
                throw new Error("Unable to allocate golfers for the selected tee time. The selected slot may have changed or no allocation field matched.");
            }
            perf.mark('allocation plan built');

            // 2. Fill allocation fields using Playwright interactions so page scripts recalculate "sofar".
            for (const item of allocationPlan) {
                const inputLocator = finalPage.locator(`input[name="${item.name}"]:not([type="hidden"])`).first();
                const selectLocator = finalPage.locator(`select[name="${item.name}"]`).first();

                if (await inputLocator.count() > 0) {
                    console.log(`[BOOK] Filling input ${item.name} with ${item.count}`);
                    await inputLocator.fill(String(item.count));
                    await inputLocator.dispatchEvent('input');
                    await inputLocator.dispatchEvent('change');
                    await inputLocator.blur().catch(() => null);
                    continue;
                }

                if (await selectLocator.count() > 0) {
                    console.log(`[BOOK] Selecting ${item.count} in ${item.name}`);
                    await selectLocator.selectOption(String(item.count));
                    await selectLocator.dispatchEvent('change');
                    continue;
                }

                console.log(`[BOOK] Allocation field ${item.name} not found in DOM.`);
            }

            console.log("[BOOK] Fields filled. Submitting with Playwright native click...");

            // Use Playwright's trusted click — more reliable than DOM .click() inside evaluate.
            // The page may require a trusted user event to accept the form submission.
            const submitLocator = finalPage.locator(
                'input[type="submit"], input[name="but1"], input[value="Submit"], input[type="image"], button[type="submit"]'
            ).first();

            const submitVisible = await submitLocator.count() > 0;
            if (!submitVisible) {
                // Fallback: try form.requestSubmit() via evaluate
                const submitted = await finalPage.evaluate(() => {
                    const form = Array.from(document.querySelectorAll('form'))
                        .find(f => f.querySelector('input[name^="allo"], select[name^="allo"]')) || document.forms[0];
                    if (!form) return false;
                    if (typeof form.requestSubmit === 'function') { form.requestSubmit(); return true; }
                    if (typeof form.submit === 'function') { form.submit(); return true; }
                    return false;
                });
                if (!submitted) throw new Error("Could not find submit button even for last-ditch effort.");
            } else {
                await submitLocator.click({ timeout: 5000 });
            }

            // stub to keep later `perf.mark` and navigation wait working
            const submissionResult = { clicked: true };

            if (!submissionResult.clicked) {
                // Dead code kept for safety — the block above always throws or sets clicked=true
                const lastDitch = await finalPage.evaluate(() => {
                    const forms = Array.from(document.querySelectorAll('form'));
                    const targetForm =
                        forms.find(f => f.querySelector('input[name="noofglf"]')) ||
                        forms.find(f => f.querySelector('input[name^="allo"], select[name^="allo"]')) ||
                        document.forms[0];
                    const allocatedCount = targetForm
                        ? Array.from(targetForm.querySelectorAll('input[name^="allo"], select[name^="allo"]'))
                            .reduce((sum, el) => {
                                const n = parseInt((el.value || '').trim(), 10);
                                return sum + (Number.isNaN(n) ? 0 : n);
                            }, 0)
                        : 0;
                    if (allocatedCount <= 0) return false;
                    const submitBtn = Array.from(document.querySelectorAll('input, button, a')).find(el => {
                        const tag = (el.tagName || '').toUpperCase();
                        const type = String(el.type || '').toLowerCase();
                        const value = String(el.value || '').trim();
                        const text = String(el.innerText || el.textContent || '').trim();
                        const label = `${value} ${text}`.toLowerCase();
                        if (tag === 'A' && String(el.getAttribute('href') || '').toLowerCase().startsWith('javascript:')) {
                            return /submit|book|confirm|continue|reserve/.test(label);
                        }
                        const isSubmit = tag === 'BUTTON' || (tag === 'INPUT' && (type === 'submit' || type === 'button' || type === 'image'));
                        return isSubmit && /submit|book|confirm|continue|reserve/.test(label);
                    });
                    if (submitBtn) {
                        submitBtn.click();
                        return true;
                    }
                    if (targetForm && typeof targetForm.requestSubmit === 'function') {
                        targetForm.requestSubmit();
                        return true;
                    }
                    if (targetForm && typeof targetForm.submit === 'function') {
                        targetForm.submit();
                        return true;
                    }
                    return false;
                });
                if (!lastDitch) throw new Error("Could not find submit button even for last-ditch effort.");
            }
            perf.mark('submission triggered');

            console.log(`[BOOK] Waiting for confirmation result (sofar: ${submissionResult.sofar})...`);
            await finalPage.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 12000 }).catch(() => null);

            // 6. Verify success and extract reservation number
            const finalContent = await finalPage.evaluate(() => document.body.innerText);
            perf.mark('confirmation page parsed');

            // Try several known confirmation formats from this system.
            const reservationPatterns = [
                /Reservation\s*No\.?\s*:?\s*([A-Z0-9]{4,})/i,
                /Reservation\s*#\s*:?\s*([A-Z0-9]{4,})/i,
                /\bConfirmation\s*(?:No\.?|#)?\s*:?\s*([A-Z0-9]{4,})/i,
                /\bRef(?:erence)?\s*(?:No\.?|#)?\s*:?\s*([A-Z0-9]{4,})/i
            ];

            let resNumberMatch = null;
            for (const pattern of reservationPatterns) {
                const match = finalContent.match(pattern);
                if (match && match[1]) {
                    resNumberMatch = match;
                    break;
                }
            }

            let resNumber = resNumberMatch ? resNumberMatch[1] : null;

            // Explicit rejection of label leftovers
            if (resNumber && (resNumber.toLowerCase() === 'ervations' || resNumber.toLowerCase() === 'ervation')) {
                console.log(`[BOOK] Rejected false positive match: ${resNumber}`);
                resNumber = null;
            }

            if (resNumberMatch) {
                const start = Math.max(0, resNumberMatch.index - 20);
                const end = Math.min(finalContent.length, resNumberMatch.index + 50);
                console.log(`[BOOK] Match context: "...${finalContent.substring(start, end).replace(/\n/g, ' ')}..."`);
            }

            // Success is defined as: found an ID, explicit success text, or the reservation summary page markers.
            const hasSuccessText =
                finalContent.includes("successfully registered") ||
                finalContent.includes("Booking Confirmed") ||
                finalContent.includes("Reservation No.") ||
                (finalContent.includes("Play Date:") && finalContent.includes("Course:") && finalContent.includes("Back to the Menu"));

            const bookedCourseMatch = finalContent.match(/Course:\s*([^\n\r]+)/i);
            const bookedCourse = bookedCourseMatch ? bookedCourseMatch[1].trim() : null;
            const bookedHoleMatches = finalContent.match(/[A-Za-z]+\/hole\s*\d+/gi) || [];
            const bookedHole = bookedHoleMatches.length > 0 ? bookedHoleMatches[0].trim() : null;

            if (resNumber || hasSuccessText) {
                const requestedCourseParts = String(course || '').split(',');
                const requestedClubToken = normalizeCourseForMatch(requestedCourseParts[0] || '');
                const requestedSubCourseToken = normalizeCourseForMatch((requestedCourseParts[1] || '').replace(/\(\d+\)/g, ''));
                const bookedCourseNormalized = normalizeCourseForMatch(bookedCourse || '');
                const bookedHoleNormalized = normalizeCourseForMatch(bookedHole || '');

                // Header line always includes club, so club mismatch is a hard failure.
                if (requestedClubToken && bookedCourseNormalized && !bookedCourseNormalized.includes(requestedClubToken)) {
                    throw new Error(`Booking completed but course mismatch. Requested "${course}" but system booked "${bookedCourse}". Reservation #${resNumber || 'unknown'}.`);
                }

                // Sub-course/hole mismatch should only fail when we can actually detect booked hole info.
                if (requestedSubCourseToken && bookedHoleNormalized && !bookedHoleNormalized.includes(requestedSubCourseToken)) {
                    throw new Error(`Booking completed but course mismatch. Requested "${course}" but system booked "${bookedCourse}, ${bookedHole}". Reservation #${resNumber || 'unknown'}.`);
                }

                console.log(`Booking confirmed! Res #: ${resNumber || 'Pending/Unknown'}`);
                res.json({
                    success: true,
                    message: `Successfully booked ${time} at ${course}!`,
                    reservationNumber: resNumber || 'Pending',
                    bookedCourse: bookedCourse || course,
                    bookedHole: bookedHole || null
                });
                perf.done('ok');
                return;
            } else {
                console.log("=== BOOKING CONFIRMATION MISSING ===");
                console.log("Page Content Preview: " + finalContent.substring(0, 1000).replace(/\n/g, ' '));
                throw new Error("Booking submitted but could not find confirmation number or success message on the result page.");
            }
        } else {
            // Check if we are already finished
            const content = await finalPage.evaluate(() => document.body.innerText);
            if (content.includes("Confirmation") || content.includes("successfully") || content.includes("Reservation Number")) {
                console.log("Reservation appears successful based on page content.");
            } else {
                throw new Error("Could not find final confirmation button on the page.");
            }
        }

        res.json({ success: true, message: `Successfully booked ${time} at ${course}` });
        perf.done('ok');
    } catch (error) {
        console.error("Booking Error:", error);
        perf.done('error');
        // Try to capture more info on failure
        if (browser) {
            try {
                const pages = browser.contexts().flatMap(context => context.pages());
                if (pages.length > 0) {
                    const activePage = pages[pages.length - 1];
                    const content = await activePage.evaluate(() => document.body ? document.body.innerHTML : 'No body');
                    console.log("=== FAILURE PAGE CONTENT START ===");
                    console.log(content.substring(0, 5000)); // Log first 5KB
                    console.log("=== FAILURE PAGE CONTENT END ===");
                    await activePage.screenshot({ path: 'booking_failure_debug.png' }).catch(e => console.log("Screenshot failed:", e.message));
                    console.log("Saved failure screenshot to booking_failure_debug.png");
                }
            } catch (e) {
                console.error("Failed to capture debug info:", e.message);
            }
        }
        res.status(500).json({ success: false, error: error.message });
    } finally {
        touchUserContext(username);
        await closePageResources(page, 'book', { closeContext: false });
    }
});


// The "catchall" handler: for any request that doesn't
// match one above, send back React's index.html file.
app.get('*', (req, res) => {
    res.sendFile(path.join(__dirname, 'villages-frontend/dist/index.html'));
});

const server = app.listen(PORT, () => {
    console.log(`Server is running on port ${PORT}`);
    // Warm up the browser
    getBrowser().then(() => console.log("Initial browser instance ready."));
});

// Increase timeout for long-running Puppeteer operations (booking can take 20-30 seconds)
server.timeout = 120000; // 2 minutes
server.keepAliveTimeout = 120000; // 2 minutes
server.headersTimeout = 125000; // Slightly more than keepAliveTimeout
