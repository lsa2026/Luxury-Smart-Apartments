"use strict";

document.addEventListener("DOMContentLoaded", () => {
    const picker = document.querySelector("[data-location-picker]");
    const mapElement = picker?.querySelector("[data-location-picker-map]");
    const latitudeInput = document.getElementById("id_public_location_latitude");
    const longitudeInput = document.getElementById("id_public_location_longitude");
    const enabledInput = document.getElementById("id_public_location_enabled");
    const clearButton = picker?.querySelector("[data-location-picker-clear]");
    const status = picker?.querySelector("[data-location-picker-status]");

    if (!picker || !mapElement || !latitudeInput || !longitudeInput || !window.L) {
        return;
    }

    const fallbackLatitude = Number(picker.dataset.defaultLatitude || 24.713552);
    const fallbackLongitude = Number(picker.dataset.defaultLongitude || 46.675296);
    const savedLatitude = Number(latitudeInput.value);
    const savedLongitude = Number(longitudeInput.value);
    const hasSavedLocation = Number.isFinite(savedLatitude) && Number.isFinite(savedLongitude)
        && latitudeInput.value !== "" && longitudeInput.value !== "";
    const center = hasSavedLocation
        ? [savedLatitude, savedLongitude]
        : [fallbackLatitude, fallbackLongitude];

    const map = window.L.map(mapElement, { scrollWheelZoom: false }).setView(
        center,
        hasSavedLocation ? 16 : 13,
    );
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
    let marker = null;

    function updateStatus(selected) {
        if (status) {
            status.textContent = selected
                ? picker.dataset.selectedLabel
                : picker.dataset.emptyLabel;
        }
    }

    function setLocation(latitude, longitude, pan = false, enable = false) {
        const normalizedLatitude = Number(latitude).toFixed(6);
        const normalizedLongitude = Number(longitude).toFixed(6);
        latitudeInput.value = normalizedLatitude;
        longitudeInput.value = normalizedLongitude;
        if (!marker) {
            marker = window.L.marker([latitude, longitude], {
                draggable: true,
                icon: markerIcon,
            }).addTo(map);
            marker.on("dragend", () => {
                const position = marker.getLatLng();
                setLocation(position.lat, position.lng, false, true);
            });
        } else {
            marker.setLatLng([latitude, longitude]);
        }
        if (pan) {
            map.panTo([latitude, longitude]);
        }
        if (enable && enabledInput) {
            enabledInput.checked = true;
        }
        updateStatus(true);
    }

    if (hasSavedLocation) {
        setLocation(savedLatitude, savedLongitude);
    } else {
        updateStatus(false);
    }

    map.on("click", (event) => {
        setLocation(event.latlng.lat, event.latlng.lng, true, true);
    });
    clearButton?.addEventListener("click", () => {
        latitudeInput.value = "";
        longitudeInput.value = "";
        if (marker) {
            marker.remove();
            marker = null;
        }
        if (enabledInput) {
            enabledInput.checked = false;
        }
        updateStatus(false);
    });
});
