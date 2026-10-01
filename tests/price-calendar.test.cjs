const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const modulePromise = import(`data:text/javascript;base64,${Buffer.from(fs.readFileSync("static/js/price-calendar.js", "utf8")).toString("base64")}`);
const days = (prices = [665, 800, 950, 700]) => new Map(prices.map((price, i) => {
    const date = `2026-10-${String(i + 1).padStart(2, "0")}`;
    return [date, {date, price: String(price), available: true, arrival_available: true, departure_available: true, minimum_stay: 1}];
}));
test("actual lower dates are highlighted, never an invented discount", async () => {
    const {lowestInMonth} = await modulePromise;
    assert.equal(lowestInMonth(days(), 2026, 9, "2026-10-01"), 665);
    assert.equal(lowestInMonth(days([665, 665, 665]), 2026, 9, "2026-10-01"), null);
});
test("past and occupied nights cannot create a cheapest-price claim", async () => {
    const {lowestInMonth} = await modulePromise;
    const data = days([1, 2, 950, 700]);
    data.get("2026-10-02").available = false;
    assert.equal(lowestInMonth(data, 2026, 9, "2026-10-02"), 700);
});
test("date selection respects unavailable nights and minimum stay", async () => {
    const {stayIsAvailable} = await modulePromise;
    const data = days();
    assert.equal(stayIsAvailable(data, "2026-10-01", "2026-10-03"), true);
    data.get("2026-10-02").available = false;
    assert.equal(stayIsAvailable(data, "2026-10-01", "2026-10-03"), false);
    data.get("2026-10-01").minimum_stay = 2;
    assert.equal(stayIsAvailable(data, "2026-10-01", "2026-10-02"), false);
});
test("checkout is allowed on a booked arrival day but not through a booked night", async () => {
    const {stayIsAvailable} = await modulePromise;
    const data = days();
    Object.assign(data.get("2026-10-03"), {available: false, arrival_available: false});
    assert.equal(stayIsAvailable(data, "2026-10-01", "2026-10-03"), true);
    assert.equal(stayIsAvailable(data, "2026-10-01", "2026-10-04"), false);
});
test("missing prices cannot become zero-price promotions", async () => {
    const {lowestInMonth} = await modulePromise;
    const data = days([665, 800]);
    data.get("2026-10-01").price = null;
    assert.equal(lowestInMonth(data, 2026, 9, "2026-10-01"), null);
});
test("Arabic month names use the same Gregorian dates as bookings", async () => {
    const {calendarLocale} = await modulePromise;
    for (const language of ["ar", "en", "fr"]) {
        const formatter = new Intl.DateTimeFormat(calendarLocale(language), {
            year: "numeric", month: "long", timeZone: "UTC"
        });
        assert.equal(formatter.resolvedOptions().calendar, "gregory");
    }
});
