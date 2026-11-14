/* Real browser, synthetic pages only. No external or production requests. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const {execFileSync} = require("node:child_process");
const {chromium} = require("playwright");

async function chooseEssentialOnly(page) {
  const reject = page.locator("[data-consent-reject]");
  if (await reject.isVisible()) await reject.click();
}

(async () => {
  const output = process.env.LSA_CONTACT_VISUAL_DIR;
  assert(output, "Set the synthetic layout directory");
  const requests = [];
  const server = http.createServer((req, res) => {
    requests.push({method: req.method, url: req.url});
    const url = new URL(req.url, "http://localhost");
    if (req.method !== "GET") { res.writeHead(405).end(); return; }
    if (url.pathname.endsWith("/price-calendar/")) {
      const start = new Date(`${url.searchParams.get("start")}T12:00:00Z`);
      const end = new Date(`${url.searchParams.get("end")}T12:00:00Z`);
      const days = [];
      for (let day = start; day < end; day = new Date(+day + 86400000)) {
        days.push({date: day.toISOString().slice(0, 10), price: "723", available: true,
          arrival_available: true, departure_available: true, minimum_stay: 1});
      }
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({currency: "SAR", updated_at: new Date().toISOString(),
        today: days[0].date, coverage_end: "2099-01-01", days}));
      return;
    }
    let file;
    if (url.pathname.startsWith("/static/")) {
      file = path.resolve("static", url.pathname.slice(8));
      if (!file.startsWith(path.resolve("static") + path.sep)) { res.writeHead(404).end(); return; }
    } else if (url.pathname.startsWith("/media/")) {
      file = path.resolve("static/images/brand/luxury-living-lockup-light.png");
    } else {
      const match = url.pathname.match(/^\/(ar|en|fr)\/properties\/(?:anchor-9101\/(gallery\/)?)?$/);
      if (match) file = path.join(output, `property-link-${match[2] ? "gallery" : url.pathname.endsWith("anchor-9101/") ? "detail" : "list"}-${match[1]}.html`);
    }
    if (!file || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
    const types = {".js":"text/javascript", ".css":"text/css", ".html":"text/html", ".png":"image/png", ".svg":"image/svg+xml", ".woff2":"font/woff2"};
    res.setHeader("Content-Type", types[path.extname(file)] || "application/octet-stream");
    fs.createReadStream(file).pipe(res);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const executablePath = execFileSync("which", ["google-chrome"], {encoding: "utf8"}).trim();
  const browser = await chromium.launch({executablePath});
  try {
    for (const language of ["ar", "en", "fr"]) {
      for (const width of [390, 820, 1366]) {
        const page = await browser.newPage({viewport: {width, height: 950}});
        await page.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
        const root = `${origin}/${language}/properties/anchor-9101/`;
        let calendarReads = 0;
        page.on("request", request => { if (request.url().includes("/price-calendar/?")) calendarReads++; });
        await page.goto(root + "?guests=3#price-calendar");
        await page.locator("[data-price-calendar][open]").waitFor();
        await page.locator(".price-calendar__day small").first().waitFor();
        assert.equal(calendarReads, 1, "Direct link loads the existing calendar once");
        assert.equal(await page.locator("[data-price-calendar]").count(), 1);
        assert.equal(await page.locator("[data-price-property]").textContent(), await page.locator("[data-property-price-calendar]").getAttribute("data-price-calendar-name"));
        assert.equal(await page.locator("[data-price-calendar]").evaluate(node => node.scrollWidth > node.clientWidth + 1), false);
        const ready = page.locator(".price-calendar__day:not([disabled])");
        await ready.nth(0).click();
        await ready.nth(1).click();
        const continuation = new URL(await page.locator("[data-price-continue]").getAttribute("href"));
        assert.equal(continuation.pathname, `/${language}/properties/anchor-9101/`);
        assert.equal(continuation.hash, "#availability");
        assert.equal(continuation.searchParams.get("guests"), "3");
        assert(continuation.searchParams.get("check_in"));
        assert(continuation.searchParams.get("check_out"));
        await page.screenshot({path: path.join(output, `property-calendar-${language}-${width}.png`)});
        await page.locator("[data-price-close]").click();
        assert.equal(await page.locator("[data-property-price-calendar]").evaluate(node => node === document.activeElement), true);
        await chooseEssentialOnly(page);
        await page.reload();
        await page.locator("[data-price-calendar][open]").waitFor();
        await page.locator("[data-price-close]").click();
        await page.goto(root + "#availability");
        const baseline = calendarReads;
        await page.waitForTimeout(150);
        assert.equal(await page.locator("[data-price-calendar][open]").count(), 0);
        assert.equal(calendarReads, baseline, "Ordinary booking anchor stays lazy");
        await page.evaluate(() => { location.hash = "price-calendar"; });
        await page.locator("[data-price-calendar][open]").waitFor();
        await page.goto(root + "gallery/");
        await chooseEssentialOnly(page);
        await page.locator(".gallery-page__item img").first().waitFor();
        await page.locator("[data-lightbox-open]").first().click();
        await page.locator("[data-lightbox][open]").waitFor();
        assert.equal(calendarReads, baseline + 1, "Gallery does not fetch prices");
        if (width === 390) await page.screenshot({path: path.join(output, `property-gallery-${language}.png`)});
        await page.close();
        console.log(`${language} ${width}px: direct/reloaded/hash/calendar/continuation/gallery passed`);
      }
      const page = await browser.newPage();
      await page.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
      await page.goto(`${origin}/${language}/properties/`);
      await chooseEssentialOnly(page);
      const card = page.locator('[data-price-calendar-open][href*="anchor-9101/"]').first();
      const href = await card.getAttribute("href");
      assert(href.endsWith("#price-calendar"));
      await card.click();
      await page.locator("[data-price-calendar][open]").waitFor();
      const tab = await page.context().newPage();
      await tab.goto(origin + href);
      await tab.locator("[data-price-calendar][open]").waitFor();
      await tab.close();
      await page.close();
    }
    const fallback = await browser.newPage();
    await fallback.route("**/*", route => {
      if (route.request().url().includes("/static/js/price-calendar.js")) return route.abort();
      return new URL(route.request().url()).origin === origin ? route.continue() : route.abort();
    });
    await fallback.goto(`${origin}/ar/properties/anchor-9101/#price-calendar`);
    await fallback.waitForTimeout(400);
    assert.equal(await fallback.locator("[data-price-calendar][open]").count(), 0);
    assert(await fallback.locator("#price-calendar").isVisible());
    assert.equal(await fallback.locator("#price-calendar").getAttribute("href"), "#availability");
    assert.equal(requests.filter(r => r.method !== "GET").length, 0, "No booking, quote or payment submitted");
    console.log("Lazy module failure stays on visible fallback; no writes submitted");
    await fallback.close();
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => {console.error(error); process.exit(1);});
