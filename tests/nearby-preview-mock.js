// Local QA ONLY; never included in a production template or static bundle.
let maps = 0, queries = 0;
const stats = () => { document.getElementById('mock-stats').textContent = `maps=${maps};queries=${queries}`; };
class MapMock {
    constructor(node, options) {
        Object.assign(this, options); maps++; stats();
        this.listeners = {};
        node.innerHTML = '<p style="padding:20px">LOCAL MOCK — not live Google data</p><button type="button">Move map (QA)</button><p style="padding:20px">Google attribution placeholder — QA only</p>';
        node.querySelector('button').addEventListener('click', () => {
            this.center = { lat: this.center.lat + .01, lng: this.center.lng };
            this.listeners.bounds_changed?.();
        });
    }
    addListener(name, fn) { this.listeners[name] = fn; }
    getCenter() { return { toJSON: () => this.center }; }
    getBounds() { return { getNorthEast: () => ({ toJSON: () => ({ lat: this.center.lat + .02, lng: this.center.lng + .02 }) }) }; }
    setCenter(center) { this.center = center; this.listeners.bounds_changed?.(); }
    setZoom(zoom) { this.zoom = zoom; this.listeners.bounds_changed?.(); }
}
class MarkerMock {
    constructor(options) { Object.assign(this, options); }
    addEventListener() {}
}
class RequestMock extends HTMLElement { constructor(options) { super(); Object.assign(this, options); } }
class SearchMock extends HTMLElement {
    constructor(options) { super(); Object.assign(this, options); this.places = []; }
    connectedCallback() {
        queries++; stats();
        const centre = this.querySelector('gmp-place-nearby-search-request').locationRestriction.center;
        this.places = [{ id: 'qa-only-place', location: { toJSON: () => centre } }];
        this.innerHTML += '<button type="button">Example place (QA)</button><p>Google attribution placeholder — QA only</p>';
        this.querySelector('button').addEventListener('click', () => {
            const event = new Event('gmp-select'); event.place = this.places[0]; this.dispatchEvent(event);
        });
        setTimeout(() => this.dispatchEvent(new Event('gmp-load')), 30);
    }
}
class DetailsMock extends HTMLElement {
    constructor(options) { super(); Object.assign(this, options); }
    connectedCallback() { queries++; stats(); this.append('Example details — LOCAL MOCK'); setTimeout(() => this.dispatchEvent(new Event('gmp-load')), 30); }
}
class DetailsRequestMock extends RequestMock {}
customElements.define('gmp-place-nearby-search-request', RequestMock);
customElements.define('gmp-place-search', SearchMock);
customElements.define('gmp-place-details-compact', DetailsMock);
customElements.define('gmp-place-details-place-request', DetailsRequestMock);
window.google = { maps: { importLibrary: async (name) => ({
    maps: { Map: MapMock }, marker: { AdvancedMarkerElement: MarkerMock }, places: {
        PlaceSearchElement: SearchMock, PlaceNearbySearchRequestElement: RequestMock,
        PlaceDetailsCompactElement: DetailsMock, PlaceDetailsPlaceRequestElement: DetailsRequestMock,
    },
})[name] } };
