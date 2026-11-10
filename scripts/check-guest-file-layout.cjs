const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
(async () => {
    const output = process.env.LSA_CONTACT_VISUAL_DIR;
    const browser = await chromium.launch({executablePath: execFileSync("which", ["google-chrome"], {encoding: "utf8"}).trim(), args: ["--allow-file-access-from-files"]});
    try {
        for (const width of [390, 820, 1366]) {
            const page = await browser.newPage({viewport: {width, height: 900}});
            await page.route(/^https?:/, route => route.abort());
            await page.goto(pathToFileURL(path.join(output, "guest-file.html")).href);
            assert.equal(await page.locator("tbody tr").count(), 3);
            const text = await page.locator("tbody").innerText();
            for (const label of ["بوكنق", "إير بي إن بي", "موقعنا", "فتح الحجز وإدارته"]) assert.ok(text.includes(label));
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `Guest file overflows at ${width}`);
            assert.ok(await page.getByRole("button", {name: "تحديث ملف الضيف من هوستاوي"}).isVisible());
            const link = page.locator("tbody a").first();
            assert.ok((await link.getAttribute("href")).includes("/bookings/"));
            const box = await link.boundingBox();
            assert.ok(box.height >= 44 && box.x >= 0 && box.x + box.width <= width + 1);
            await page.screenshot({path: path.join(output, `guest-file-${width}.png`), fullPage: true});
            await page.close();
            console.log(`Guest file ${width}px: saved classification, ordered history, channels and existing management link visible`);
        }
    } finally {await browser.close();}
})().catch(error => {console.error(error); process.exit(1);});
