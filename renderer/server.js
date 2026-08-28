/**
 * Relay renderer. Stateless: URL in, screenshot and DOM metrics out.
 *
 * It never touches Firestore or Vertex. That is what lets it scale to zero and
 * keeps the browser dependency from spreading into the agent service.
 *
 * It also never submits a form. There is no code path here that clicks a submit
 * control, and there is no flag that enables one. Submitting sends a real
 * person a real lead notification. Form health (B2) is measured by filling
 * fields and reading the browser's own validity state, which is a read.
 *
 * robots.txt is enforced by the caller, which has already fetched and parsed it
 * during recon. This service renders what it is told to render.
 */

import http from "node:http";
import { chromium } from "playwright-core";

const PORT = process.env.PORT || 8080;
const SHARED_SECRET = process.env.RENDERER_SHARED_SECRET || "";
const BUILD_SHA = process.env.BUILD_SHA || "dev";
const NAV_TIMEOUT_MS = Number(process.env.NAV_TIMEOUT_MS || 25000);
const MAX_CONCURRENCY = Number(process.env.MAX_CONCURRENCY || 2);

// A mobile viewport, because every threshold in the Chosen section is a mobile
// threshold. iPhone 14 / Pixel 7 class.
const DEFAULT_VIEWPORT = { width: 390, height: 844 };

// Honest identification, per the criteria doc. The mobile token is needed for
// sites to serve their mobile layout at all; the bot token is appended so we
// are never pretending to be a homeowner.
const USER_AGENT =
  "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) " +
  "Chrome/131.0.0.0 Mobile Safari/537.36 RelayAuditBot/1.0 (+https://relayforroofers.com/bot)";

let browserPromise = null;
let inFlight = 0;

async function getBrowser() {
  if (!browserPromise) {
    // In the container the bundled chromium is on PLAYWRIGHT_BROWSERS_PATH. On a
    // workstation the pinned revision usually is not, so allow a system Chrome.
    const launch = {
      args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    };
    if (process.env.CHROME_EXECUTABLE_PATH) {
      launch.executablePath = process.env.CHROME_EXECUTABLE_PATH;
    }
    browserPromise = chromium.launch(launch);
  }
  return browserPromise;
}

/**
 * Runs inside the page. Returns facts, never verdicts: the thresholds for C2,
 * C5 and C7 live in Python next to the criteria doc, so retuning one does not
 * mean redeploying a browser.
 */

/**
 * B2 form health, run inside the page. Fills the most likely lead form and
 * reads the browser's own constraint validation state. It never calls
 * form.submit(), never clicks a submit control, and never dispatches a submit
 * event. Filling to test validation is check B2; submitting sends a real
 * person a real lead notification, and nothing here can do it.
 */
function probeFormHealth() {
  const INVISIBLE = ["hidden", "submit", "button", "image", "reset"];
  const visible = (el) => {
    const s = window.getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };

  const forms = [...document.querySelectorAll("form")];
  const candidates = forms
    .map((form) => {
      const controls = [...form.querySelectorAll("input, select, textarea")].filter((c) => {
        const type = (c.getAttribute("type") || "").toLowerCase();
        return !INVISIBLE.includes(type);
      });
      const names = controls.map((c) => (c.getAttribute("name") || c.id || "").toLowerCase()).join(" ");
      const kinds = new Set(controls.map((c) => (c.getAttribute("type") || c.tagName).toLowerCase()));
      // By name, by input type (Gravity Forms names fields input_9 but still
      // types the email field), or by shape: several fields plus a message box.
      const contactish =
        /email|phone|tel|name|message|zip|address/.test(names) ||
        kinds.has("email") || kinds.has("tel") ||
        (controls.length >= 3 && kinds.has("textarea"));
      const searchish = /(^|[^a-z])s(earch)?([^a-z]|$)/.test(names) && controls.length < 2;
      // The form a homeowner would use, not the first one in the DOM. A page
      // carries newsletter strips and hidden modal forms alongside the real
      // one; probing an invisible modal produced a verdict about a form no
      // visitor can see.
      const score =
        (visible(form) ? 100 : 0) +
        controls.filter(visible).length * 2 +
        (kinds.has("email") ? 10 : 0) +
        (kinds.has("tel") ? 10 : 0) +
        (kinds.has("textarea") ? 5 : 0);
      return { form, controls, contactish, searchish, score };
    })
    .filter((c) => c.controls.length >= 2 && c.contactish && !c.searchish)
    .sort((a, b) => b.score - a.score);

  if (!candidates.length) return { found: false };

  const { form, controls } = candidates[0];
  const requiredControls = controls.filter(
    (c) => c.required || (c.getAttribute("aria-required") || "").toLowerCase() === "true"
  );

  // Browser-enforced validation only counts when the form has not opted out.
  const novalidate = form.hasAttribute("novalidate");
  const emptyValid = form.checkValidity();

  // Fill with an obviously-an-audit identity. Values never leave the page.
  for (const c of controls) {
    const kind = (c.getAttribute("type") || c.tagName).toLowerCase();
    const name = (c.getAttribute("name") || c.id || "").toLowerCase();
    try {
      if (kind === "checkbox" || kind === "radio") {
        // A required consent box left unticked reads as "rejects a correctly
        // filled entry", which is a false finding about a working form. Tick
        // what the form requires: the value never leaves the page, nothing is
        // ever submitted, so no consent is ever actually given.
        const requiredGroup =
          c.required ||
          (c.name && form.querySelector(`input[name="${CSS.escape(c.name)}"][required]`));
        if (requiredGroup && !c.checked) {
          c.checked = true;
          c.dispatchEvent(new Event("change", { bubbles: true }));
        }
        continue;
      }
      if (kind === "select") {
        const opt = [...c.options].find((o) => o.value && !o.disabled);
        if (opt) c.value = opt.value;
      } else if (kind === "email" || name.includes("email")) {
        c.value = "audit@relayforroofers.com";
      } else if (kind === "tel" || /phone|tel|mobile/.test(name)) {
        c.value = "7195550100";
      } else if (/zip|postal/.test(name)) {
        c.value = "80903";
      } else if (kind === "textarea" || /message|comment|detail/.test(name)) {
        c.value = "Relay site audit. This form is being checked, not submitted.";
      } else {
        c.value = "Relay Audit";
      }
      c.dispatchEvent(new Event("input", { bubbles: true }));
      c.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (e) { /* a readonly control is fine */ }
  }
  const filledValid = form.checkValidity();

  // When a filled form still refuses, name the control and the browser's own
  // words for why. "Your phone field rejects 7195550100" is a demonstrable
  // finding; "the form is broken" is an assertion.
  const invalidFields = [];
  if (!filledValid) {
    for (const c of controls) {
      if (invalidFields.length >= 5) break;
      if (typeof c.checkValidity === "function" && !c.checkValidity()) {
        invalidFields.push({
          name: c.getAttribute("name") || c.id || c.tagName.toLowerCase(),
          type: (c.getAttribute("type") || c.tagName).toLowerCase(),
          value_we_entered: String(c.value || "").slice(0, 40),
          browser_says: String(c.validationMessage || "").slice(0, 120),
        });
      }
    }
  }

  const submitControl = form.querySelector(
    'button[type="submit"], input[type="submit"], button:not([type])'
  );

  return {
    found: true,
    action: form.getAttribute("action"),
    method: (form.getAttribute("method") || "get").toLowerCase(),
    field_count: controls.filter(visible).length,
    required_count: requiredControls.length,
    novalidate,
    empty_valid: emptyValid,
    filled_valid: filledValid,
    invalid_fields: invalidFields,
    has_submit_control: !!submitControl,
    visible: visible(form),
  };
}

function collectMetrics() {
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  const styleOf = (el) => window.getComputedStyle(el);
  const visible = (el) => {
    const s = styleOf(el);
    if (s.display === "none" || s.visibility === "hidden") return false;
    if (parseFloat(s.opacity || "1") === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return {
      top: Math.round(r.top),
      left: Math.round(r.left),
      width: Math.round(r.width),
      height: Math.round(r.height),
    };
  };
  // Rendered without scrolling. Anything whose top edge is past the fold needs
  // a scroll to reach, which is the thing C5 and C7 are actually asking about.
  const inFold = (el) => {
    const r = el.getBoundingClientRect();
    return r.top < vh && r.bottom > 0 && r.left < vw && r.right > 0;
  };
  const textOf = (el) => (el.innerText || el.textContent || "").trim().slice(0, 120);

  // ── Horizontal overflow ────────────────────────────────────────────────────
  const doc = document.documentElement;
  const scrollWidth = Math.max(doc.scrollWidth, document.body ? document.body.scrollWidth : 0);
  const scrollHeight = Math.max(doc.scrollHeight, document.body ? document.body.scrollHeight : 0);
  const overflowing = [];
  for (const el of document.querySelectorAll("body *")) {
    if (overflowing.length >= 5) break;
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width > vw + 2 || r.right > vw + 2) {
      overflowing.push({ tag: el.tagName.toLowerCase(), cls: (el.className || "").toString().slice(0, 60), rect: rect(el) });
    }
  }

  // ── Font sizes, weighted by how much text is actually set at each size ─────
  // A 12px legal footer should not condemn a site whose body copy is 18px, so
  // the caller gets a histogram and decides.
  const histogram = {};
  let totalChars = 0;
  const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const value = (node.nodeValue || "").trim();
    if (value.length < 12) continue;
    const parent = node.parentElement;
    if (!parent || !visible(parent)) continue;
    if (["SCRIPT", "STYLE", "NOSCRIPT"].includes(parent.tagName)) continue;
    const px = Math.round(parseFloat(styleOf(parent).fontSize) || 0);
    if (!px) continue;
    histogram[px] = (histogram[px] || 0) + value.length;
    totalChars += value.length;
  }

  // ── Above the fold ─────────────────────────────────────────────────────────
  const telLinks = [];
  for (const el of document.querySelectorAll('a[href^="tel:" i]')) {
    if (!visible(el)) continue;
    telLinks.push({ href: el.getAttribute("href"), text: textOf(el), rect: rect(el), above_fold: inFold(el) });
  }

  const PHONE = /(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}/;
  const phoneText = [];
  for (const el of document.querySelectorAll("body *")) {
    if (phoneText.length >= 6) break;
    if (el.children.length > 0) continue;
    const value = (el.textContent || "").trim();
    if (value.length > 60 || !PHONE.test(value)) continue;
    if (!visible(el)) continue;
    phoneText.push({ text: value.slice(0, 60), rect: rect(el), above_fold: inFold(el) });
  }

  const forms = [];
  for (const el of document.querySelectorAll("form")) {
    const controls = [...el.querySelectorAll("input, select, textarea")].filter((c) => {
      const type = (c.getAttribute("type") || "").toLowerCase();
      return !["hidden", "submit", "button", "image", "reset"].includes(type);
    });
    forms.push({
      action: el.getAttribute("action"),
      method: (el.getAttribute("method") || "get").toLowerCase(),
      field_count: controls.length,
      visible: visible(el),
      above_fold: visible(el) && inFold(el),
      rect: rect(el),
    });
  }

  const CTA = /(free|no.?obligation)?\s*(estimate|quote|inspection|consultation)|get started|book (now|online|a)|schedule|request (a )?(quote|service|estimate)|contact us|call now|text us/i;
  const ctas = [];
  for (const el of document.querySelectorAll('a, button, [role="button"], input[type="submit"]')) {
    if (ctas.length >= 8) break;
    if (!visible(el)) continue;
    const label = textOf(el) || el.getAttribute("value") || el.getAttribute("aria-label") || "";
    if (!label || !CTA.test(label)) continue;
    ctas.push({ text: label.slice(0, 60), href: el.getAttribute("href"), rect: rect(el), above_fold: inFold(el) });
  }

  // The rendered DOM's visible text and markup. A site that builds its page
  // with JavaScript serves almost nothing in its HTML: measured on a real
  // prospect, 4 characters of source text against 4478 rendered. Every text
  // based check was reading the empty version and failing by default.
  var renderedText = "";
  var renderedHtml = "";
  try {
    renderedText = (document.body && document.body.innerText || "").slice(0, 200000);
    renderedHtml = (document.documentElement.outerHTML || "").slice(0, 300000);
  } catch (e) { /* a hostile page is not worth failing the whole render for */ }

  return {
    text: renderedText,
    html: renderedHtml,
    viewport: { width: vw, height: vh },
    document: {
      scroll_width: Math.round(scrollWidth),
      client_width: vw,
      // How much page exists below the fold. The vision screenshot is clipped,
      // so this is what says whether the model saw the whole story.
      scroll_height: Math.round(scrollHeight),
      client_height: vh,
    },
    horizontal_scroll: scrollWidth > vw + 2,
    overflowing_elements: overflowing,
    fonts: { histogram, total_chars: totalChars },
    tel_links: telLinks,
    phone_text: phoneText,
    forms,
    ctas,
    title: document.title || "",
  };
}

async function render(payload) {
  const {
    url,
    viewport = DEFAULT_VIEWPORT,
    screenshot = "viewport",
    settle_ms = 1200,
    format = "png",
    max_height = 6000,
    form_health = false,
  } = payload;

  const browser = await getBrowser();
  const context = await browser.newContext({
    viewport,
    userAgent: USER_AGENT,
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
    ignoreHTTPSErrors: true,
    javaScriptEnabled: true,
  });
  context.setDefaultTimeout(NAV_TIMEOUT_MS);

  const page = await context.newPage();
  const started = Date.now();
  const consoleErrors = [];
  page.on("pageerror", (err) => {
    if (consoleErrors.length < 5) consoleErrors.push(String(err).slice(0, 200));
  });

  try {
    const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: NAV_TIMEOUT_MS });
    // Lazy loaded heroes and cookie banners settle after DOMContentLoaded, and
    // both move what is above the fold.
    await page.waitForTimeout(settle_ms);
    try {
      await page.waitForLoadState("networkidle", { timeout: 5000 });
    } catch {
      // A site with a polling widget never goes idle. Measure it anyway.
    }

    const metrics = await page.evaluate(collectMetrics);

    let formHealth = null;
    if (form_health) {
      // Read-only in effect: values are set and validity is read, nothing is
      // ever submitted. See probeFormHealth.
      formHealth = await page.evaluate(probeFormHealth);
    }

    let shot = null;
    if (screenshot !== "none") {
      let options = { type: format === "jpeg" ? "jpeg" : "png" };
      if (options.type === "jpeg") options.quality = 82;

      if (screenshot === "full") {
        // A roofing homepage can run past 20000px, and a model downsamples a
        // strip that tall until nothing in it is legible. Clipping keeps the
        // hero, the services band and the start of the gallery at a size worth
        // looking at, which is where project photos actually live.
        // clip on its own intersects the viewport and silently returns the
        // fold, byte for byte identical to a viewport shot. fullPage is what
        // makes clip address the whole scrollable page. Both are required.
        const height = await page.evaluate(() => document.documentElement.scrollHeight);
        options.fullPage = true;
        options.clip = {
          x: 0,
          y: 0,
          width: viewport.width,
          height: Math.min(height, max_height),
        };
      }

      const buffer = await page.screenshot(options);
      shot = {
        encoding: "base64",
        format: options.type,
        bytes: buffer.toString("base64"),
        size_bytes: buffer.length,
        clipped_height: options.clip ? options.clip.height : viewport.height,
      };
    }

    return {
      ok: true,
      url,
      final_url: page.url(),
      status: response ? response.status() : null,
      elapsed_ms: Date.now() - started,
      console_errors: consoleErrors,
      screenshot: shot,
      form_health: formHealth,
      ...metrics,
    };
  } finally {
    await context.close().catch(() => {});
  }
}

function send(res, code, body) {
  const payload = JSON.stringify(body);
  res.writeHead(code, { "content-type": "application/json", "content-length": Buffer.byteLength(payload) });
  res.end(payload);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > 1_000_000) reject(new Error("body too large"));
      chunks.push(c);
    });
    req.on("end", () => {
      try {
        resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {});
      } catch (err) {
        reject(err);
      }
    });
    req.on("error", reject);
  });
}

const server = http.createServer(async (req, res) => {
  // The engine spec asks for /healthz. Cloud Run's frontend intercepts exactly
  // that path and answers its own 404, so the request never reaches this
  // process. Verified on this service: /health, /livez and even /healthz2 all
  // arrive, /healthz never does. /health is the one that works in production;
  // /healthz stays registered because it works everywhere else.
  if (req.method === "GET" && ["/health", "/healthz", "/_health"].includes(req.url)) {
    return send(res, 200, { ok: true, build: BUILD_SHA, in_flight: inFlight });
  }

  if (req.method !== "POST" || !req.url.startsWith("/render")) {
    return send(res, 404, { ok: false, error: "not found" });
  }

  // An open endpoint that drives a headless browser is a credit drain, so the
  // secret is required even behind Cloud Run IAM.
  if (SHARED_SECRET && req.headers["x-relay-secret"] !== SHARED_SECRET) {
    return send(res, 401, { ok: false, error: "unauthorized" });
  }

  if (inFlight >= MAX_CONCURRENCY) {
    return send(res, 429, { ok: false, error: "busy" });
  }

  let payload;
  try {
    payload = await readBody(req);
  } catch (err) {
    return send(res, 400, { ok: false, error: `bad request: ${err.message}` });
  }

  if (!payload.url || !/^https?:\/\//i.test(payload.url)) {
    return send(res, 400, { ok: false, error: "url must be http or https" });
  }

  inFlight += 1;
  try {
    return send(res, 200, await render(payload));
  } catch (err) {
    // A site that will not render is a finding, not a server fault. The caller
    // records the checks as skipped and continues.
    return send(res, 200, {
      ok: false,
      url: payload.url,
      error: `${err.name}: ${err.message}`.slice(0, 300),
    });
  } finally {
    inFlight -= 1;
  }
});

server.listen(PORT, () => console.log(`renderer listening on ${PORT} build=${BUILD_SHA}`));

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, async () => {
    server.close();
    if (browserPromise) await (await browserPromise).close().catch(() => {});
    process.exit(0);
  });
}
