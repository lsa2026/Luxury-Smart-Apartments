"use strict";

document.querySelectorAll("[data-property-map]").forEach((mapElement) => {
    if (!window.L) {
        return;
    }
    const latitude = Number(mapElement.dataset.latitude);
    const longitude = Number(mapElement.dataset.longitude);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
        return;
    }

    const map = window.L.map(mapElement, {
        scrollWheelZoom: false,
    }).setView([latitude, longitude], 16);
    window.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);
    const markerIcon = window.L.divIcon({
        className: "lsa-map-marker-shell",
        html: '<span class="lsa-map-marker" aria-hidden="true"></span>',
        iconAnchor: [15, 30],
        iconSize: [30, 30],
    });
    window.L.marker([latitude, longitude], { icon: markerIcon }).addTo(map);
});
