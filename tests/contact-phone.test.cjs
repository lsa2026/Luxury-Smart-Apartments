const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("static/js/contact-phone.js", "utf8");

for (const [language, name] of [["ar", "السعودية"], ["en", "Saudi Arabia"], ["fr", "Arabie saoudite"]]) {
    test(`country label is localized without changing selection or submitting: ${language}`, () => {
        const select = {value: "MA", options: [{value: "SA", textContent: "SA (+966)"}]};
        vm.runInNewContext(source, {
            document: {documentElement: {lang: language}, querySelectorAll: () => [select]},
            Intl,
        });
        assert.ok(select.options[0].textContent.includes(name));
        assert.ok(select.options[0].textContent.includes("(+966)"));
        assert.equal(select.value, "MA");
    });
}

test("unavailable localization leaves a usable original selector", () => {
    const select = {value: "SA", options: [{value: "SA", textContent: "SA (+966)"}]};
    vm.runInNewContext(source, {
        document: {documentElement: {lang: "ar"}, querySelectorAll: () => [select]},
        Intl: {},
    });
    assert.equal(select.options[0].textContent, "SA (+966)");
    assert.equal(select.value, "SA");
});
