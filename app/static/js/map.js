/**
 * SafeRoute Map Initialization & Browser Live-Location Controller (Issue #12 — B3).
 *
 * Privacy guarantee:
 * - User live coordinates remain strictly in browser memory for the active map session.
 * - Live coordinates are never persisted to browser storage, logged to the console,
 *   placed in URLs, or transmitted to FastAPI or Supabase.
 */
(function () {
  "use strict";

  var mapInstance = null;
  var userMarker = null;
  var resizeObserver = null;
  var resizeTimer = null;
  var resizeBound = false;
  var activeTimeoutId = null;
  var requestSeq = 0;
  var lastLiveLocation = null;
  var currentConfig = null;
  var stateListener = null;

  var mapState = {
    initialized: false,
    locating: false,
    locationMode: "loading", // loading | live | fallback | error
    statusTitle: "Locating you...",
    statusMessage: "Checking your browser location for nearby road awareness.",
    mapError: "",
  };

  function isValidCoordinates(lat, lng) {
    return (
      typeof lat === "number" &&
      typeof lng === "number" &&
      Number.isFinite(lat) &&
      Number.isFinite(lng) &&
      lat >= -90 &&
      lat <= 90 &&
      lng >= -180 &&
      lng <= 180
    );
  }

  function readMapConfig() {
    var configEl = document.getElementById("sr-map-config");
    if (!configEl || !configEl.textContent) {
      return null;
    }
    try {
      var parsed = JSON.parse(configEl.textContent);
      if (
        !parsed ||
        !isValidCoordinates(Number(parsed.fallbackLat), Number(parsed.fallbackLng))
      ) {
        return null;
      }
      return {
        fallbackLat: Number(parsed.fallbackLat),
        fallbackLng: Number(parsed.fallbackLng),
        fallbackLabel: String(parsed.fallbackLabel || "Fallback location"),
        defaultZoom: Number(parsed.defaultZoom) || 16,
        geolocationTimeoutMs:
          Number(parsed.geolocationTimeoutMs) > 0
            ? Number(parsed.geolocationTimeoutMs)
            : 5000,
        boundsDebounceMs: Number(parsed.boundsDebounceMs) || 400,
      };
    } catch (_err) {
      return null;
    }
  }

  function notifyState() {
    if (typeof stateListener === "function") {
      stateListener(Object.assign({}, mapState));
    }
  }

  function updateState(patch) {
    Object.assign(mapState, patch);
    notifyState();
  }

  var LIVE_MARKER_TITLE = "You are here";
  var FALLBACK_MARKER_TITLE = "Default map location";

  function getMarkerTitle(isLive) {
    return isLive ? LIVE_MARKER_TITLE : FALLBACK_MARKER_TITLE;
  }

  function sanitizeText(text) {
    return String(text || "").replace(/[<>&"]/g, "");
  }

  function buildUserLocationIcon(isLive) {
    var ariaLabel = getMarkerTitle(isLive);
    return window.L.divIcon({
      className: "sr-user-location-marker",
      html:
        '<div class="sr-user-location-pin" aria-label="' +
        ariaLabel +
        '">' +
        '<span class="sr-user-location-halo"></span>' +
        '<span class="sr-user-location-core"></span>' +
        "</div>",
      iconSize: [28, 28],
      iconAnchor: [14, 14],
      popupAnchor: [0, -14],
    });
  }

  function buildPopupHtml(isLive, fallbackLabel) {
    var heading = isLive ? LIVE_MARKER_TITLE : FALLBACK_MARKER_TITLE;
    var safeFallback = sanitizeText(fallbackLabel);
    var subtitle = isLive
      ? "Current browser location (not saved)."
      : safeFallback +
        " — default map area, not your confirmed physical location.";
    return (
      '<div class="text-sm leading-snug">' +
      '<p class="font-bold text-primary-dark">' +
      heading +
      "</p>" +
      '<p class="text-xs text-muted mt-0.5">' +
      subtitle +
      "</p>" +
      "</div>"
    );
  }

  function upsertUserMarker(lat, lng, isLive) {
    if (!mapInstance || !window.L) {
      return;
    }
    var markerTitle = getMarkerTitle(isLive);
    var popupHtml = buildPopupHtml(isLive, currentConfig.fallbackLabel);
    if (userMarker) {
      userMarker.setLatLng([lat, lng]);
      userMarker.options.title = markerTitle;
      userMarker.options.alt = markerTitle;
      userMarker.setIcon(buildUserLocationIcon(isLive));
      var el =
        typeof userMarker.getElement === "function"
          ? userMarker.getElement()
          : null;
      if (el) {
        el.setAttribute("title", markerTitle);
        el.setAttribute("aria-label", markerTitle);
      }
      userMarker.setPopupContent(popupHtml);
      return;
    }
    userMarker = window.L.marker([lat, lng], {
      icon: buildUserLocationIcon(isLive),
      title: markerTitle,
      alt: markerTitle,
      zIndexOffset: 1000,
      isSafeRouteLocationMarker: true,
    });
    userMarker.bindPopup(popupHtml);
    userMarker.addTo(mapInstance);
  }

  function centerMapOn(lat, lng, isLive, animate) {
    if (!mapInstance) {
      return;
    }
    mapInstance.setView([lat, lng], currentConfig.defaultZoom, {
      animate: Boolean(animate),
    });
    upsertUserMarker(lat, lng, isLive);
  }

  function clearActiveTimeout() {
    if (activeTimeoutId !== null) {
      window.clearTimeout(activeTimeoutId);
      activeTimeoutId = null;
    }
  }

  function applyFallback(reasonTitle, reasonMessage, animate) {
    clearActiveTimeout();
    lastLiveLocation = null;
    centerMapOn(
      currentConfig.fallbackLat,
      currentConfig.fallbackLng,
      false,
      animate
    );
    updateState({
      locating: false,
      locationMode: "fallback",
      statusTitle: reasonTitle,
      statusMessage: reasonMessage,
    });
  }

  function describeGeolocationError(err, fallbackLabel) {
    var code = err && typeof err.code === "number" ? err.code : 0;
    if (code === 1) {
      return {
        title: "Showing " + fallbackLabel,
        message:
          "Location access was denied. Using the default area—you can pan, zoom, or press Re-centre on me anytime.",
      };
    }
    if (code === 2) {
      return {
        title: "Showing " + fallbackLabel,
        message:
          "Your device location is currently unavailable. Showing the default area instead.",
      };
    }
    if (code === 3) {
      return {
        title: "Showing " + fallbackLabel,
        message:
          "Location request timed out after 5 seconds. Showing the default area—press Re-centre on me to retry.",
      };
    }
    return {
      title: "Showing " + fallbackLabel,
      message:
        "Could not determine your location. Showing the default area—you can explore the map freely.",
    };
  }

  function requestBrowserLocation(animateOnResult) {
    if (!currentConfig || !mapInstance) {
      return;
    }

    requestSeq += 1;
    var seq = requestSeq;
    clearActiveTimeout();

    if (
      typeof navigator === "undefined" ||
      !navigator.geolocation ||
      typeof navigator.geolocation.getCurrentPosition !== "function"
    ) {
      applyFallback(
        "Showing " + currentConfig.fallbackLabel,
        "Your browser does not support geolocation. Showing the default area instead.",
        animateOnResult
      );
      return;
    }

    updateState({
      locating: true,
      locationMode: "loading",
      statusTitle: "Locating you...",
      statusMessage: "Requesting your current location (5-second timeout).",
    });

    // Defensive safety timer: if location takes longer than 5 seconds (e.g. prompt pending),
    // display the fallback area so the user isn't stuck on a loading screen.
    activeTimeoutId = window.setTimeout(function () {
      if (seq !== requestSeq || mapState.locationMode === "live") {
        return;
      }
      applyFallback(
        "Showing " + currentConfig.fallbackLabel,
        "Location request timed out after 5 seconds. Showing the default area—press Re-centre on me to retry.",
        animateOnResult
      );
    }, currentConfig.geolocationTimeoutMs);

    try {
      navigator.geolocation.getCurrentPosition(
        function (position) {
          if (seq !== requestSeq) {
            return;
          }
          clearActiveTimeout();
          var coords = position && position.coords ? position.coords : null;
          var lat = coords ? Number(coords.latitude) : NaN;
          var lng = coords ? Number(coords.longitude) : NaN;

          if (!isValidCoordinates(lat, lng)) {
            applyFallback(
              "Showing " + currentConfig.fallbackLabel,
              "Received invalid coordinates from your browser. Showing the default area instead.",
              animateOnResult
            );
            return;
          }

          // Store strictly in browser memory only
          lastLiveLocation = { lat: lat, lng: lng };
          centerMapOn(lat, lng, true, true);
          updateState({
            locating: false,
            locationMode: "live",
            statusTitle: "Centered on your location",
            statusMessage:
              "Showing your current area. Your live location stays in your browser and is never saved.",
          });
        },
        function (err) {
          if (seq !== requestSeq) {
            return;
          }
          clearActiveTimeout();
          if (mapState.locationMode === "live") {
            return;
          }
          var info = describeGeolocationError(err, currentConfig.fallbackLabel);
          applyFallback(info.title, info.message, animateOnResult);
        },
        {
          enableHighAccuracy: false,
          timeout: 15000,
          maximumAge: 60000,
        }
      );
    } catch (_geoErr) {
      if (seq === requestSeq) {
        clearActiveTimeout();
        applyFallback(
          "Showing " + currentConfig.fallbackLabel,
          "Could not access browser location. Showing the default area instead.",
          animateOnResult
        );
      }
    }
  }

  function recenter() {
    if (!currentConfig || !mapInstance) {
      return;
    }
    if (lastLiveLocation && isValidCoordinates(lastLiveLocation.lat, lastLiveLocation.lng)) {
      centerMapOn(lastLiveLocation.lat, lastLiveLocation.lng, true, true);
      updateState({
        locating: false,
        locationMode: "live",
        statusTitle: "Centered on your location",
        statusMessage:
          "Showing your current area. Your live location stays in your browser and is never saved.",
      });
    }
    requestBrowserLocation(true);
  }

  function invalidateSize() {
    if (mapInstance && typeof mapInstance.invalidateSize === "function") {
      mapInstance.invalidateSize({ animate: false });
    }
  }

  function bindResizeHandlers(containerEl) {
    if (!resizeBound) {
      resizeBound = true;
      window.addEventListener("resize", function () {
        if (resizeTimer !== null) {
          window.clearTimeout(resizeTimer);
        }
        resizeTimer = window.setTimeout(invalidateSize, 120);
      });
    }
    if (
      typeof window.ResizeObserver === "function" &&
      containerEl &&
      !resizeObserver
    ) {
      resizeObserver = new window.ResizeObserver(function () {
        invalidateSize();
      });
      resizeObserver.observe(containerEl);
    }
  }

  function initMap() {
    if (mapState.initialized && mapInstance) {
      return true;
    }

    var containerEl = document.getElementById("saferoute-map");
    if (!containerEl) {
      return false;
    }

    currentConfig = readMapConfig();
    if (!currentConfig) {
      updateState({
        locating: false,
        locationMode: "error",
        mapError: "Map configuration could not be loaded. Please refresh the page.",
        statusTitle: "Map unavailable",
        statusMessage: "Map configuration is missing or invalid.",
      });
      return false;
    }

    if (typeof window.L === "undefined" || typeof window.L.map !== "function") {
      updateState({
        locating: false,
        locationMode: "error",
        mapError:
          "The map library could not be loaded. Check your connection and try refreshing.",
        statusTitle: "Map unavailable",
        statusMessage: "Leaflet failed to initialize.",
      });
      return false;
    }

    try {
      mapInstance = window.L.map(containerEl, {
        center: [currentConfig.fallbackLat, currentConfig.fallbackLng],
        zoom: currentConfig.defaultZoom,
        zoomControl: false,
      });

      window.L.control.zoom({ position: "topright" }).addTo(mapInstance);

      window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
      }).addTo(mapInstance);

      // Place single marker at approved fallback immediately so map is usable right away
      upsertUserMarker(currentConfig.fallbackLat, currentConfig.fallbackLng, false);
      bindResizeHandlers(containerEl);

      updateState({
        initialized: true,
        mapError: "",
      });

      // Request browser live location with approved 5-second timeout
      requestBrowserLocation(false);
      return true;
    } catch (_initErr) {
      updateState({
        initialized: false,
        locating: false,
        locationMode: "error",
        mapError: "Unable to initialize the interactive map. Please try refreshing.",
        statusTitle: "Map unavailable",
        statusMessage: "Interactive map initialization failed.",
      });
      return false;
    }
  }

  function countUserMarkers() {
    if (!mapInstance) {
      return 0;
    }
    var count = 0;
    mapInstance.eachLayer(function (layer) {
      if (
        layer &&
        layer.options &&
        layer.options.isSafeRouteLocationMarker === true
      ) {
        count += 1;
      }
    });
    return count;
  }

  // Expose controlled API for B4 integration and verification tests
  window.SafeRouteMap = {
    init: initMap,
    recenter: recenter,
    invalidateSize: invalidateSize,
    getMap: function () {
      return mapInstance;
    },
    getUserMarker: function () {
      return userMarker;
    },
    getMarkerCount: countUserMarkers,
    getState: function () {
      return Object.assign({}, mapState);
    },
    isValidCoordinates: isValidCoordinates,
  };

  function createMapShell() {
    return {
      sheetExpanded: false,
      locating: true,
      locationMode: "loading",
      statusTitle: "Locating you...",
      statusMessage: "Checking your browser location for nearby road awareness.",
      mapError: "",

      init: function () {
        var self = this;
        stateListener = function (nextState) {
          self.locating = Boolean(nextState.locating);
          self.locationMode = nextState.locationMode;
          self.statusTitle = nextState.statusTitle;
          self.statusMessage = nextState.statusMessage;
          self.mapError = nextState.mapError;
        };
        initMap();
      },

      recenter: function () {
        recenter();
      },

      toggleSheet: function () {
        this.sheetExpanded = !this.sheetExpanded;
        window.setTimeout(invalidateSize, 150);
      },

      closeSheet: function () {
        this.sheetExpanded = false;
        window.setTimeout(invalidateSize, 150);
      },
    };
  }

  function registerMapShell() {
    if (window.Alpine && typeof window.Alpine.data === "function") {
      window.Alpine.data("srMapShell", createMapShell);
    }
  }

  document.addEventListener("alpine:init", registerMapShell);
  if (window.Alpine) {
    registerMapShell();
  }
})();
