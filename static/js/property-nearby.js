// Google data stays in the open page only. No server search, storage or geolocation.
export const CATEGORIES = Object.freeze({
    coffee: ['cafe', 'coffee_shop'], restaurant: ['restaurant'], laundry: ['laundry'],
    supermarket: ['supermarket', 'grocery_store', 'convenience_store'], mosque: ['mosque'],
    gym: ['gym', 'fitness_center'], bus: ['bus_stop', 'bus_station'], metro: ['subway_station'],
});

export function distanceMetres(a, b) {
    const radians = (n) => n * Math.PI / 180;
    const x = Math.sin(radians(b.lat - a.lat) / 2) ** 2
        + Math.cos(radians(a.lat)) * Math.cos(radians(b.lat))
        * Math.sin(radians(b.lng - a.lng) / 2) ** 2;
    return 12742000 * Math.asin(Math.sqrt(Math.min(1, x)));
}

export function searchOptions(category, center, corner = null) {
    if (!Object.hasOwn(CATEGORIES, category)) throw new Error('Unsupported category');
    const base = category === 'metro' ? 5000 : 1000;
    const cap = category === 'metro' ? 20000 : 10000;
    const radius = corner ? Math.max(100, Math.min(cap, distanceMetres(center, corner))) : base;
    return { includedTypes: [...CATEGORIES[category]], maxResultCount: 10,
        rankPreference: 'DISTANCE', locationRestriction: { center, radius } };
}

export function directionsUrl(place) {
    if (!place?.id || !place.location) return null;
    const { lat, lng } = place.location.toJSON();
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
    return 'https://www.google.com/maps/dir/?' + new URLSearchParams({
        api: '1', destination: `${lat},${lng}`, destination_place_id: place.id,
    });
}

export function makeQueryBudget(now = Date.now, max = 30) {
    let count = 0, last = -Infinity;
    return () => {
        const time = now();
        if (count >= max || time - last < 1000) return false;
        count += 1; last = time;
        return true;
    };
}

let sdkPromise = null, sdkAttempt = 0;
export function loadMaps(config, win = window, doc = document) {
    if (win.google?.maps?.importLibrary) return Promise.resolve(win.google.maps);
    if (!config.key) return Promise.reject(new Error('Missing browser key'));
    if (sdkPromise) return sdkPromise;
    sdkPromise = new Promise((resolve, reject) => {
        const callback = `__lsaNearbyMaps${++sdkAttempt}`;
        const script = doc.createElement('script');
        let timer;
        const finish = (error) => {
            win.clearTimeout(timer);
            delete win[callback];
            script.onerror = null;
            if (error) { script.remove(); reject(error); }
            else resolve(win.google.maps);
        };
        win[callback] = () => finish();
        script.async = true;
        script.nonce = doc.querySelector('script[nonce]')?.nonce || '';
        script.src = 'https://maps.googleapis.com/maps/api/js?' + new URLSearchParams({
            key: config.key, v: 'weekly', loading: 'async', callback,
            language: config.language, region: config.region,
        });
        script.onerror = () => finish(new Error('Maps unavailable'));
        timer = win.setTimeout(() => finish(new Error('Maps timed out')), 20000);
        doc.head.append(script);
    }).catch((error) => { sdkPromise = null; throw error; });
    return sdkPromise;
}

export function setupNearby(doc = document, win = window, loader = loadMaps) {
    const dialog = doc.querySelector('[data-nearby-dialog]');
    const openButton = doc.querySelector('[data-nearby-open]');
    const configNode = doc.getElementById('property-nearby-config');
    if (!dialog || !openButton || !configNode) return null;
    let config;
    try { config = JSON.parse(configNode.textContent); } catch { return null; }
    const find = (name) => dialog.querySelector(`[data-nearby-${name}]`);
    const categoryButtons = [...dialog.querySelectorAll('[data-nearby-category]')];
    const status = find('status'), areaButton = find('search');
    const results = find('results'), selected = find('selected');
    const consumeQuery = makeQueryBudget();
    let maps, places, markerLibrary, map, markers = [], initialising;
    let category = null, areaMode = false, busy = false, searchElement;
    let searchTimer, searchVersion = 0, selectedId = null, detailsTimer, detailsVersion = 0;
    let authFailed = false;
    let opener;
    const message = (key) => { status.textContent = dialog.dataset[key]; };
    const setBusy = (value) => {
        busy = value;
        for (const button of categoryButtons) button.disabled = value || !map || authFailed;
        areaButton.disabled = value || !category || !map || authFailed;
        find('home').disabled = !map || authFailed;
        results.setAttribute('aria-busy', String(value));
    };
    const clearMarkers = () => { for (const marker of markers) marker.map = null; markers = []; };
    const hideSelected = () => {
        ++detailsVersion;
        win.clearTimeout(detailsTimer);
        selected.hidden = true; selectedId = null;
        find('details').replaceChildren();
        find('directions').removeAttribute('href');
    };
    const fail = () => { setBusy(false); message('unavailable'); };
    const selectPlace = (place) => {
        const url = directionsUrl(place);
        if (!url || !dialog.open || selectedId === place.id) return;
        if (!consumeQuery()) { message('limit'); return; }
        hideSelected(); selectedId = place.id;
        const version = detailsVersion;
        const details = new places.PlaceDetailsCompactElement({ orientation: 'vertical' });
        const request = new places.PlaceDetailsPlaceRequestElement({ place });
        const content = doc.createElement('gmp-place-content-config');
        const attribution = doc.createElement('gmp-place-attribution');
        attribution.setAttribute('light-scheme-color', 'gray');
        content.append(attribution);
        details.append(content, request);
        details.addEventListener('gmp-error', () => {
            if (version === detailsVersion && dialog.open) {
                win.clearTimeout(detailsTimer); message('unavailable');
            }
        });
        details.addEventListener('gmp-load', () => {
            if (version === detailsVersion) win.clearTimeout(detailsTimer);
        });
        detailsTimer = win.setTimeout(() => {
            if (version === detailsVersion && dialog.open) message('unavailable');
        }, 20000);
        find('details').replaceChildren(details);
        find('directions').href = url;
        selected.hidden = false;
    };
    const search = (inVisibleArea = false) => {
        if (!map || !category || busy || authFailed || !dialog.open) return false;
        if (!consumeQuery()) { message('limit'); return false; }
        const center = inVisibleArea ? map.getCenter().toJSON() : config.center;
        const corner = inVisibleArea ? map.getBounds()?.getNorthEast().toJSON() : null;
        areaMode = inVisibleArea;
        const version = ++searchVersion;
        clearMarkers(); hideSelected(); setBusy(true); message('searching');
        areaButton.hidden = true;
        // Fully configure a detached request, then connect once: no piecemeal updates.
        const request = new places.PlaceNearbySearchRequestElement(searchOptions(category, center, corner));
        const element = new places.PlaceSearchElement({ selectable: true });
        element.append(doc.createElement('gmp-place-all-content'), request);
        searchElement = element;
        element.addEventListener('gmp-select', (event) => selectPlace(event.place));
        element.addEventListener('gmp-error', () => {
            if (version !== searchVersion) return;
            win.clearTimeout(searchTimer); fail(); areaButton.hidden = false;
        });
        element.addEventListener('gmp-load', () => {
            if (version !== searchVersion || !dialog.open) return;
            win.clearTimeout(searchTimer); setBusy(false);
            message(element.places.length ? 'results' : 'empty');
            for (const place of element.places) {
                if (!place.location) continue;
                const marker = new markerLibrary.AdvancedMarkerElement({
                    map, position: place.location, gmpClickable: true,
                });
                marker.addEventListener('gmp-click', () => selectPlace(place));
                markers.push(marker);
            }
        });
        searchTimer = win.setTimeout(() => {
            if (version !== searchVersion) return;
            ++searchVersion; element.remove(); fail(); areaButton.hidden = false;
        }, 25000);
        results.replaceChildren(element);
        return true;
    };
    const initialise = async () => {
        maps = await loader(config, win, doc);
        let libraryTimer, libraries;
        try {
            libraries = await Promise.race([
                Promise.all([
                    maps.importLibrary('maps'), maps.importLibrary('places'), maps.importLibrary('marker'),
                ]),
                new Promise((resolve, reject) => {
                    libraryTimer = win.setTimeout(() => reject(new Error('Libraries timed out')), 20000);
                }),
            ]);
        } finally { win.clearTimeout(libraryTimer); }
        places = libraries[1]; markerLibrary = libraries[2];
        if (!places.PlaceSearchElement || !places.PlaceNearbySearchRequestElement
            || !places.PlaceDetailsCompactElement) throw new Error('UI Kit unavailable');
        // Closing during SDK loading must not construct a hidden, billable map.
        if (!dialog.open) return;
        if (authFailed) throw new Error('Maps authentication failed');
        map = new libraries[0].Map(find('map'), {
            center: config.center, zoom: 15, mapId: config.mapId,
            clickableIcons: false, mapTypeControl: false, streetViewControl: false,
            fullscreenControl: false, gestureHandling: 'cooperative',
        });
        const pin = doc.createElement('span');
        pin.className = 'nearby-apartment-pin'; pin.textContent = dialog.dataset.apartment;
        new markerLibrary.AdvancedMarkerElement({
            map, position: config.center, content: pin, title: dialog.dataset.apartment, zIndex: 1000,
        });
        // Movement reveals a button only; it never changes the query element.
        map.addListener('bounds_changed', () => { if (category && dialog.open) areaButton.hidden = false; });
    };
    const open = async () => {
        if (dialog.open) return;
        opener = doc.activeElement || openButton;
        dialog.showModal(); doc.body.classList.add('nearby-open');
        find('close').focus();
        if (authFailed) { fail(); return; }
        if (map) {
            map.setCenter(config.center); map.setZoom(15);
            message('ready'); setBusy(false); return;
        }
        message('loading'); setBusy(true);
        try {
            initialising ||= initialise().finally(() => { initialising = null; });
            await initialising;
            if (dialog.open) { message(map ? 'ready' : 'unavailable'); setBusy(false); }
        } catch { if (dialog.open) fail(); }
    };
    const close = () => { if (dialog.open) dialog.close(); };
    openButton.addEventListener('click', open);
    find('close').addEventListener('click', close);
    dialog.addEventListener('click', (event) => { if (event.target === dialog) close(); });
    dialog.addEventListener('close', () => {
        doc.body.classList.remove('nearby-open');
        win.clearTimeout(searchTimer); ++searchVersion; busy = false;
        clearMarkers(); hideSelected(); results.replaceChildren(); searchElement = null;
        category = null; areaMode = false; areaButton.hidden = true;
        for (const button of categoryButtons) button.setAttribute('aria-pressed', 'false');
        opener?.focus();
    });
    const previousAuthFailure = win.gm_authFailure;
    win.gm_authFailure = () => {
        authFailed = true; fail();
        if (typeof previousAuthFailure === 'function') previousAuthFailure();
    };
    for (const button of categoryButtons) button.addEventListener('click', () => {
        const previous = category;
        category = button.dataset.nearbyCategory;
        if (search(areaMode)) {
            for (const item of categoryButtons) item.setAttribute('aria-pressed', String(item === button));
        } else category = previous;
    });
    areaButton.addEventListener('click', () => search(true));
    find('home').addEventListener('click', () => {
        if (!map) return;
        areaMode = false; map.setCenter(config.center); map.setZoom(category === 'metro' ? 13 : 15);
        // Keep old results explicitly until the guest requests an apartment-centred query.
        if (category) areaButton.hidden = false;
    });
    // No geolocation; the native dialog handles Escape, focus trapping and top-layer stacking.
    return { open, close, search, getMap: () => map, getSearchElement: () => searchElement };
}

if (typeof document !== 'undefined') setupNearby();
