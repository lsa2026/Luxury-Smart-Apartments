const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("static/js/property-map.js", "utf8");

for (const [language, zoomInTitle, zoomOutTitle] of [
    ["ar", "تكبير", "تصغير"], ["en", "Zoom in", "Zoom out"], ["fr", "Agrandir", "Réduire"]
]) {
    test(`${language}: map titles are localized without changing location or zoom behavior`, () => {
        const mapElement = {dataset: {latitude: "24.7", longitude: "46.6"}};
        const map = {attributionControl: {setPrefix(html) {
            assert.equal(html, '<a href="https://leafletjs.com/">Leaflet</a>');
        }}, setView(coordinates, zoom) {
            assert.equal(JSON.stringify(coordinates), "[24.7,46.6]");
            assert.equal(zoom, 16);
            return this;
        }};
        let zoomControls = 0;
        const layer = {addTo(target) {assert.equal(target, map);}};
        vm.runInNewContext(source, {
            document: {documentElement: {lang: language}, querySelectorAll: () => [mapElement]},
            window: {L: {
                map(target, options) {
                    assert.equal(target, mapElement);
                    assert.equal(options.scrollWheelZoom, false);
                    assert.equal(options.zoomControl, false);
                    return map;
                },
                control: {zoom(options) {
                    assert.equal(options.zoomInTitle, zoomInTitle);
                    assert.equal(options.zoomOutTitle, zoomOutTitle);
                    zoomControls++;
                    return layer;
                }},
                tileLayer(url, options) {
                    assert.equal(url, "https://tile.openstreetmap.org/{z}/{x}/{y}.png");
                    assert.equal(options.maxZoom, 19);
                    return layer;
                },
                divIcon: () => ({}), marker: () => layer,
            }}
        });
        assert.equal(zoomControls, 1);
    });
}
