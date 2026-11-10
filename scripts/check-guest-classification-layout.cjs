/* Synthetic private page snapshot only; no authentication or outgoing requests. */
const { chromium } = require("playwright");
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

(async () => {
    const output = process.env.LSA_CONTACT_VISUAL_DIR;
    if (!output) throw new Error("Synthetic layout directory required");
    const executablePath = execFileSync("which", ["google-chrome"], { encoding: "utf8" }).trim();
    const browser = await chromium.launch({ executablePath, args: ["--allow-file-access-from-files"] });
    try {
        for (const width of [390, 820, 1366]) {
            const page = await browser.newPage({ viewport: { width, height: 900 } });
            await page.route(/^https?:/, route => route.abort());
            await page.goto(pathToFileURL(path.join(output, "guest-classification.html")).href);
            const surface = page.locator(width <= 1100 ? ".lsa-booking-list__cards" : ".lsa-booking-list__table");
            await surface.waitFor({ state: "visible" });
            for (const code of ["new", "returning", "unknown"]) {
                const badge = surface.locator(`.lsa-guest-classification__badge--${code}`).first();
                if (!(await badge.isVisible())) throw new Error(`Missing ${code} label at ${width}`);
            }
            const selector = page.locator('select[name="guest_type"]');
            if (!(await selector.isVisible())) throw new Error("Guest filter missing");
            if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)) {
                throw new Error(`Guest list overflows at ${width}`);
            }
            const bounds = await selector.boundingBox();
            if (bounds.height < 44 || bounds.x < 0 || bounds.x + bounds.width > width + 1) {
                throw new Error(`Guest filter is not usable at ${width}`);
            }
            await page.screenshot({ path: path.join(output, `guest-classification-${width}.png`), fullPage: true });
            await page.close();
            console.log(`Guest classification ${width}px: visible labels/filter, no overflow`);
        }
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
