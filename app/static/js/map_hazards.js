/**
 * SafeRoute Hazard Markers, Filters, Bounds Loading & Live SSE Updates (Issue #13 — B4).
 *
 * Security & privacy guarantees:
 * - Connects exclusively to authenticated FastAPI endpoints (/api/reports, /reports/{id})
 *   and the Option R1 server-side SSE bridge (EventSource('/api/reports/stream')).
 * - Never accesses browser geolocation APIs, live user coordinates, auth tokens,
 *   service-role keys, or client-side browser storage.
 * - Renders all untrusted report fields via safe DOM APIs (textContent) only.
 */
(function () {
  "use strict";

  var CATEGORY_META = {
    pothole: "Pothole",
    road_break: "Road break",
    streetlight: "Streetlight",
    footpath: "Footpath",
    other: "Other",
  };

  var STATUS_META = {
    open: "Open",
    escalated: "Escalated",
    fixed: "Fixed",
  };

  var SEVERITY_META = {
    low: { label: "Low", shortLabel: "L", hex: "#2E9E5B", css: "sr-severity-low" },
    medium: { label: "Medium", shortLabel: "M", hex: "#F5A524", css: "sr-severity-medium" },
    high: { label: "High", shortLabel: "H", hex: "#D92D20", css: "sr-severity-high" },
  };

  var CONFIDENCE_META = {
    low: "Low confidence",
    medium: "Medium confidence",
    high: "High confidence",
  };

  var STALE_DAYS_THRESHOLD = 30;
  var MS_PER_DAY = 24 * 60 * 60 * 1000;

  var mapRef = null;
  var clusterGroup = null;
  var usingClusterPlugin = false;
  var moveEndHandler = null;
  var debounceTimer = null;
  var reconnectTimer = null;
  var reconnectAttempts = 0;
  var eventSource = null;
  var hadConnectedOnce = false;
  var abortController = null;
  var fetchSeq = 0;
  var lastQueryKey = "";
  var debounceMs = 400;
  var mounted = false;
  var destroyed = false;

  var reportsById = Object.create(null);
  var markersById = Object.create(null);
  var thumbnailCache = Object.create(null);
  var pendingThumbnailFetches = Object.create(null);
  var uiListeners = [];

  var uiState = {
    loading: true,
    error: "",
    selectedCategories: [],
    selectedStatuses: ["open", "escalated"],
    showFixed: false,
    selectedReportId: null,
    visibleReports: [],
    totalInBounds: 0,
    boundsLimit: 500,
    liveStatus: "connecting", // connecting | connected | reconnecting | offline
  };

  function isValidCoord(lat, lng) {
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

  function isSafeImageUrl(url) {
    if (typeof url !== "string") {
      return false;
    }
    var trimmed = url.trim();
    if (!trimmed) {
      return false;
    }
    return (
      trimmed.indexOf("https://") === 0 ||
      trimmed.indexOf("http://") === 0 ||
      trimmed.indexOf("/") === 0
    );
  }

  function normalizeSeverityBand(report) {
    var raw = String((report && report.severity_band) || "").toLowerCase();
    if (raw === "low" || raw === "medium" || raw === "high") {
      return raw;
    }
    var score = Number((report && report.severity_score) || 0);
    if (score >= 70) {
      return "high";
    }
    if (score >= 40) {
      return "medium";
    }
    return "low";
  }

  function normalizeCategory(category) {
    var key = String(category || "other").toLowerCase();
    return Object.prototype.hasOwnProperty.call(CATEGORY_META, key) ? key : "other";
  }

  function normalizeStatus(status) {
    var key = String(status || "open").toLowerCase();
    return Object.prototype.hasOwnProperty.call(STATUS_META, key) ? key : "open";
  }

  function normalizeConfidence(confidence) {
    var key = String(confidence || "low").toLowerCase();
    return Object.prototype.hasOwnProperty.call(CONFIDENCE_META, key) ? key : "low";
  }

  function formatCategoryLabel(category) {
    return CATEGORY_META[normalizeCategory(category)];
  }

  function formatStatusLabel(status) {
    return STATUS_META[normalizeStatus(status)];
  }

  function formatConfidenceLabel(confidence) {
    return CONFIDENCE_META[normalizeConfidence(confidence)];
  }

  function formatFreshness(isoString, nowMs) {
    if (!isoString) {
      return "Recently reported";
    }
    var ts = Date.parse(isoString);
    if (!Number.isFinite(ts)) {
      return "Recently reported";
    }
    var now = typeof nowMs === "number" ? nowMs : Date.now();
    var diffSec = Math.max(0, Math.floor((now - ts) / 1000));
    if (diffSec < 60) {
      return "Just now";
    }
    var mins = Math.floor(diffSec / 60);
    if (mins < 60) {
      return mins === 1 ? "1 min ago" : mins + " mins ago";
    }
    var hours = Math.floor(mins / 60);
    if (hours < 24) {
      return hours === 1 ? "1 hour ago" : hours + " hours ago";
    }
    var days = Math.floor(hours / 24);
    if (days < 30) {
      return days === 1 ? "1 day ago" : days + " days ago";
    }
    var months = Math.floor(days / 30);
    if (months < 12) {
      return months === 1 ? "1 month ago" : months + " months ago";
    }
    var years = Math.floor(days / 365);
    return years === 1 ? "1 year ago" : years + " years ago";
  }

  function isStaleReport(report, nowMs) {
    if (!report || normalizeStatus(report.status) === "fixed") {
      return false;
    }
    var activityIso = report.updated_at || report.created_at;
    if (!activityIso) {
      return false;
    }
    var ts = Date.parse(activityIso);
    if (!Number.isFinite(ts)) {
      return false;
    }
    var now = typeof nowMs === "number" ? nowMs : Date.now();
    return now - ts >= STALE_DAYS_THRESHOLD * MS_PER_DAY;
  }

  function extractInlineThumbnail(report) {
    if (!report) {
      return "";
    }
    if (isSafeImageUrl(report.thumbnail_url)) {
      return report.thumbnail_url.trim();
    }
    if (Array.isArray(report.images) && report.images.length > 0) {
      for (var i = 0; i < report.images.length; i += 1) {
        var img = report.images[i];
        if (img && isSafeImageUrl(img.signed_url)) {
          return img.signed_url.trim();
        }
      }
    }
    return "";
  }

  function normalizeReport(raw) {
    if (!raw || !raw.id) {
      return null;
    }
    var lat = Number(raw.lat);
    var lng = Number(raw.lng);
    if (!isValidCoord(lat, lng)) {
      return null;
    }
    var id = String(raw.id);
    var existing = reportsById[id];
    var inlineThumb = extractInlineThumbnail(raw);
    if (inlineThumb) {
      thumbnailCache[id] = inlineThumb;
    }
    var band = normalizeSeverityBand(raw);
    var score = Number.isFinite(Number(raw.severity_score))
      ? Number(raw.severity_score)
      : 0;
    return {
      id: id,
      category: normalizeCategory(raw.category),
      categoryLabel: formatCategoryLabel(raw.category),
      description: String(raw.description || ""),
      lat: lat,
      lng: lng,
      address_text: raw.address_text ? String(raw.address_text) : "",
      status: normalizeStatus(raw.status),
      statusLabel: formatStatusLabel(raw.status),
      escalation_recommended: Boolean(raw.escalation_recommended),
      severity_score: score,
      severity_band: band,
      severityLabel: SEVERITY_META[band].label,
      confidence: normalizeConfidence(raw.confidence),
      confidenceLabel: formatConfidenceLabel(raw.confidence),
      confirm_count: Number(raw.confirm_count || 0),
      dispute_count: Number(raw.dispute_count || 0),
      fixed_count: Number(raw.fixed_count || 0),
      created_at: String(raw.created_at || (existing && existing.created_at) || ""),
      updated_at: String(raw.updated_at || raw.created_at || ""),
      freshness: formatFreshness(raw.created_at || (existing && existing.created_at) || ""),
      isStale: isStaleReport(raw),
      thumbnailUrl: thumbnailCache[id] || "",
    };
  }

  function readDebounceMs() {
    var el = document.getElementById("sr-map-config");
    if (!el || !el.textContent) {
      return 400;
    }
    try {
      var cfg = JSON.parse(el.textContent);
      var parsed = Number(cfg && cfg.boundsDebounceMs);
      return Number.isFinite(parsed) && parsed > 0 ? parsed : 400;
    } catch (_err) {
      return 400;
    }
  }

  function emitUiState() {
    var snapshot = Object.assign({}, uiState, {
      selectedCategories: uiState.selectedCategories.slice(),
      selectedStatuses: uiState.selectedStatuses.slice(),
      visibleReports: uiState.visibleReports.slice(),
    });
    for (var i = 0; i < uiListeners.length; i += 1) {
      try {
        uiListeners[i](snapshot);
      } catch (_err) {
        // Isolate listener failures
      }
    }
  }

  function getActiveStatusSet() {
    var active = {};
    for (var i = 0; i < uiState.selectedStatuses.length; i += 1) {
      var st = normalizeStatus(uiState.selectedStatuses[i]);
      if (st !== "fixed" || uiState.showFixed) {
        active[st] = true;
      }
    }
    if (uiState.showFixed) {
      active.fixed = true;
    }
    return active;
  }

  function matchesFilters(report) {
    if (!report) {
      return false;
    }
    if (uiState.selectedCategories.length > 0) {
      if (uiState.selectedCategories.indexOf(report.category) === -1) {
        return false;
      }
    }
    var activeStatuses = getActiveStatusSet();
    if (!activeStatuses[report.status]) {
      return false;
    }
    return true;
  }

  function isInsideCurrentBounds(report) {
    if (!mapRef || !report || !isValidCoord(report.lat, report.lng)) {
      return false;
    }
    var bounds = mapRef.getBounds();
    if (!bounds) {
      return false;
    }
    return bounds.contains([report.lat, report.lng]);
  }

  function sortReports(list) {
    return list.sort(function (a, b) {
      if (b.severity_score !== a.severity_score) {
        return b.severity_score - a.severity_score;
      }
      var ta = Date.parse(a.created_at) || 0;
      var tb = Date.parse(b.created_at) || 0;
      return tb - ta;
    });
  }

  function buildHazardIcon(report, isSelected) {
    var band = report.severity_band;
    var meta = SEVERITY_META[band] || SEVERITY_META.low;
    var classes = "sr-hazard-pin sr-hazard-pin--" + band;
    if (isSelected) {
      classes += " sr-hazard-pin--selected";
    }
    if (report.status === "fixed") {
      classes += " sr-hazard-pin--fixed";
    }
    var aria =
      report.categoryLabel +
      " — " +
      meta.label +
      " severity (" +
      report.statusLabel +
      ")";
    return window.L.divIcon({
      className: "sr-hazard-marker",
      html:
        '<div class="' +
        classes +
        '" aria-label="' +
        aria.replace(/["<>]/g, "") +
        '">' +
        "<span>" +
        meta.shortLabel +
        "</span>" +
        "</div>",
      iconSize: [30, 30],
      iconAnchor: [15, 15],
      popupAnchor: [0, -15],
    });
  }

  function createPlaceholderElement(labelText) {
    var box = document.createElement("div");
    box.className = "sr-popup-placeholder";
    var text = document.createElement("span");
    text.textContent = labelText || "No photo available";
    box.appendChild(text);
    return box;
  }

  function renderThumbnailIntoSlot(slotEl, url) {
    if (!slotEl) {
      return;
    }
    slotEl.textContent = "";
    if (!isSafeImageUrl(url)) {
      slotEl.appendChild(createPlaceholderElement("No photo available"));
      return;
    }
    var img = document.createElement("img");
    img.className = "sr-popup-thumb";
    img.alt = "Reported road hazard photo";
    img.loading = "lazy";
    img.src = url;
    img.addEventListener("error", function () {
      slotEl.textContent = "";
      slotEl.appendChild(createPlaceholderElement("Photo unavailable"));
    });
    slotEl.appendChild(img);
  }

  function loadThumbnailForPopup(reportId, slotEl) {
    if (!reportId || !slotEl) {
      return;
    }
    if (Object.prototype.hasOwnProperty.call(thumbnailCache, reportId)) {
      renderThumbnailIntoSlot(slotEl, thumbnailCache[reportId]);
      return;
    }
    if (pendingThumbnailFetches[reportId]) {
      pendingThumbnailFetches[reportId].then(function (url) {
        renderThumbnailIntoSlot(slotEl, url);
      });
      return;
    }
    slotEl.textContent = "";
    slotEl.appendChild(createPlaceholderElement("Loading photo..."));

    var promise = window
      .fetch("/reports/" + encodeURIComponent(reportId), {
        method: "GET",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      })
      .then(function (resp) {
        if (!resp.ok) {
          return "";
        }
        return resp.json().then(function (data) {
          return extractInlineThumbnail(data);
        });
      })
      .catch(function () {
        return "";
      })
      .then(function (url) {
        thumbnailCache[reportId] = url || "";
        delete pendingThumbnailFetches[reportId];
        if (reportsById[reportId]) {
          reportsById[reportId].thumbnailUrl = thumbnailCache[reportId];
        }
        renderThumbnailIntoSlot(slotEl, thumbnailCache[reportId]);
        return thumbnailCache[reportId];
      });

    pendingThumbnailFetches[reportId] = promise;
  }

  function buildHazardPopupElement(report) {
    var card = document.createElement("div");
    card.className = "sr-popup-card space-y-2";
    card.setAttribute("data-popup-report-id", report.id);

    // Photo thumbnail slot (never exposes raw storage_path)
    var thumbSlot = document.createElement("div");
    thumbSlot.className = "sr-popup-thumb-slot";
    if (Object.prototype.hasOwnProperty.call(thumbnailCache, report.id)) {
      renderThumbnailIntoSlot(thumbSlot, thumbnailCache[report.id]);
    } else if (isSafeImageUrl(report.thumbnailUrl)) {
      renderThumbnailIntoSlot(thumbSlot, report.thumbnailUrl);
    } else {
      thumbSlot.appendChild(createPlaceholderElement("No photo available"));
    }
    card.appendChild(thumbSlot);

    // Header row: Category + Severity pill (colour dot + visible text label)
    var header = document.createElement("div");
    header.className = "flex items-center justify-between gap-2";

    var catEl = document.createElement("p");
    catEl.className = "text-sm font-bold text-primary-dark leading-tight";
    catEl.textContent = report.categoryLabel;
    header.appendChild(catEl);

    var sevMeta = SEVERITY_META[report.severity_band] || SEVERITY_META.low;
    var sevBadge = document.createElement("span");
    sevBadge.className =
      "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold " +
      sevMeta.css;
    var dot = document.createElement("span");
    dot.className = "h-2 w-2 rounded-full sr-severity-" + report.severity_band + "-dot";
    dot.setAttribute("aria-hidden", "true");
    sevBadge.appendChild(dot);
    var sevText = document.createElement("span");
    sevText.textContent = sevMeta.label + " · " + report.severity_score;
    sevBadge.appendChild(sevText);
    header.appendChild(sevBadge);
    card.appendChild(header);

    // Status + Confidence + Freshness row
    var metaRow = document.createElement("div");
    metaRow.className = "flex flex-wrap items-center gap-1.5 text-[11px]";

    var confBadge = document.createElement("span");
    confBadge.className =
      "inline-flex items-center rounded-full bg-primary-soft text-primary-dark px-2 py-0.5 font-semibold";
    confBadge.textContent = report.confidenceLabel;
    metaRow.appendChild(confBadge);

    if (report.status === "escalated") {
      var escBadge = document.createElement("span");
      escBadge.className =
        "inline-flex items-center rounded-full border sr-severity-high px-2 py-0.5 font-semibold";
      escBadge.textContent = "Escalated to higher authority";
      metaRow.appendChild(escBadge);
    } else if (report.status === "fixed") {
      var fixBadge = document.createElement("span");
      fixBadge.className =
        "inline-flex items-center rounded-full border sr-severity-low px-2 py-0.5 font-semibold";
      fixBadge.textContent = "Fixed";
      metaRow.appendChild(fixBadge);
    }

    var freshEl = document.createElement("span");
    freshEl.className = "text-muted font-medium";
    freshEl.textContent = report.freshness;
    metaRow.appendChild(freshEl);
    card.appendChild(metaRow);

    // Stale warning (>= 30 days without activity)
    if (report.isStale) {
      var staleEl = document.createElement("p");
      staleEl.className =
        "rounded-md border sr-severity-medium px-2 py-1 text-[11px] font-semibold";
      staleEl.textContent = "Stale – may no longer be accurate";
      card.appendChild(staleEl);
    }

    // Safe description snippet
    if (report.description) {
      var descEl = document.createElement("p");
      descEl.className = "text-xs text-ink leading-snug line-clamp-2";
      descEl.textContent =
        report.description.length > 140
          ? report.description.slice(0, 137) + "..."
          : report.description;
      card.appendChild(descEl);
    }

    // View details link
    var link = document.createElement("a");
    link.href = "/reports/" + encodeURIComponent(report.id);
    link.className =
      "sr-popup-cta mt-1 inline-flex w-full items-center justify-center rounded-full bg-primary hover:bg-primary-dark text-white text-xs font-semibold px-3 py-2 transition-colors";
    link.textContent = "View details";
    card.appendChild(link);

    return card;
  }

  function removeMarkerById(reportId) {
    var existing = markersById[reportId];
    if (!existing) {
      return;
    }
    if (clusterGroup) {
      clusterGroup.removeLayer(existing);
    }
    delete markersById[reportId];
  }

  function upsertMarker(report) {
    if (!clusterGroup || !window.L) {
      return;
    }
    var isSelected = uiState.selectedReportId === report.id;
    var markerTitle =
      report.categoryLabel +
      " — " +
      report.severityLabel +
      " severity (" +
      report.statusLabel +
      ")";

    var existing = markersById[report.id];
    if (existing) {
      existing.setLatLng([report.lat, report.lng]);
      existing.options.title = markerTitle;
      existing.options.alt = markerTitle;
      existing.options.severityBand = report.severity_band;
      existing.setIcon(buildHazardIcon(report, isSelected));
      existing.setPopupContent(buildHazardPopupElement(report));
      if (usingClusterPlugin && typeof clusterGroup.refreshClusters === "function") {
        clusterGroup.refreshClusters(existing);
      }
      return;
    }

    var marker = window.L.marker([report.lat, report.lng], {
      icon: buildHazardIcon(report, isSelected),
      title: markerTitle,
      alt: markerTitle,
      reportId: report.id,
      severityBand: report.severity_band,
      isSafeRouteHazardMarker: true,
    });
    marker.bindPopup(buildHazardPopupElement(report), {
      maxWidth: 280,
      autoPanPadding: [24, 24],
    });
    marker.on("click", function () {
      setSelectedReport(report.id, false);
    });
    marker.on("popupopen", function (evt) {
      var popupEl = evt && evt.popup ? evt.popup.getElement() : null;
      var slotEl = popupEl ? popupEl.querySelector(".sr-popup-thumb-slot") : null;
      if (slotEl) {
        loadThumbnailForPopup(report.id, slotEl);
      }
    });
    markersById[report.id] = marker;
    clusterGroup.addLayer(marker);
  }

  function syncMarkersAndList() {
    var visible = [];
    var shouldKeep = Object.create(null);
    var ids = Object.keys(reportsById);

    for (var i = 0; i < ids.length; i += 1) {
      var report = reportsById[ids[i]];
      if (report && isInsideCurrentBounds(report) && matchesFilters(report)) {
        shouldKeep[report.id] = true;
        visible.push(report);
      }
    }

    // Remove markers no longer visible or filtered out
    var existingMarkerIds = Object.keys(markersById);
    for (var j = 0; j < existingMarkerIds.length; j += 1) {
      var mId = existingMarkerIds[j];
      if (!shouldKeep[mId]) {
        removeMarkerById(mId);
      }
    }

    sortReports(visible);

    for (var k = 0; k < visible.length; k += 1) {
      upsertMarker(visible[k]);
    }

    if (uiState.selectedReportId && !shouldKeep[uiState.selectedReportId]) {
      uiState.selectedReportId = null;
    }

    uiState.visibleReports = visible;
    uiState.totalInBounds = visible.length;
    emitUiState();
  }

  function setSelectedReport(reportId, focusOnMap) {
    var prevId = uiState.selectedReportId;
    uiState.selectedReportId = reportId || null;

    if (prevId && prevId !== reportId && reportsById[prevId] && markersById[prevId]) {
      upsertMarker(reportsById[prevId]);
    }
    if (reportId && reportsById[reportId] && markersById[reportId]) {
      upsertMarker(reportsById[reportId]);
    }

    emitUiState();

    if (focusOnMap && reportId && markersById[reportId] && mapRef) {
      var marker = markersById[reportId];
      if (usingClusterPlugin && typeof clusterGroup.zoomToShowLayer === "function") {
        clusterGroup.zoomToShowLayer(marker, function () {
          marker.openPopup();
        });
      } else {
        mapRef.panTo(marker.getLatLng(), { animate: true });
        marker.openPopup();
      }
      window.dispatchEvent(new CustomEvent("saferoute:close-sheet"));
    } else if (!focusOnMap && reportId) {
      var itemEl = document.querySelector('[data-report-item="' + reportId + '"]');
      if (itemEl && typeof itemEl.scrollIntoView === "function") {
        itemEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    }
  }

  function buildBoundsApiUrl() {
    if (!mapRef) {
      return null;
    }
    var b = mapRef.getBounds();
    if (!b) {
      return null;
    }
    var sw = b.getSouthWest();
    var ne = b.getNorthEast();
    var minLat = Math.max(-90, Math.min(90, sw.lat)).toFixed(6);
    var minLng = Math.max(-180, Math.min(180, sw.lng)).toFixed(6);
    var maxLat = Math.max(-90, Math.min(90, ne.lat)).toFixed(6);
    var maxLng = Math.max(-180, Math.min(180, ne.lng)).toFixed(6);

    var params = new URLSearchParams();
    params.set("bounds", minLat + "," + minLng + "," + maxLat + "," + maxLng);

    // Pass single category / status directly to existing A8 API contract
    if (uiState.selectedCategories.length === 1) {
      params.set("category", uiState.selectedCategories[0]);
    }
    var activeStatusKeys = Object.keys(getActiveStatusSet());
    if (activeStatusKeys.length === 1) {
      params.set("status", activeStatusKeys[0]);
    }

    return "/api/reports?" + params.toString();
  }

  function loadReportsInBounds(options) {
    if (!mapRef || destroyed) {
      return Promise.resolve();
    }
    var force = Boolean(options && options.force);
    var url = buildBoundsApiUrl();
    if (!url) {
      return Promise.resolve();
    }
    if (!force && url === lastQueryKey && !uiState.error) {
      syncMarkersAndList();
      return Promise.resolve();
    }
    lastQueryKey = url;

    if (abortController && typeof abortController.abort === "function") {
      abortController.abort();
    }
    abortController = typeof window.AbortController === "function"
      ? new window.AbortController()
      : null;

    fetchSeq += 1;
    var seq = fetchSeq;
    uiState.loading = true;
    uiState.error = "";
    emitUiState();

    var fetchOpts = {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    };
    if (abortController) {
      fetchOpts.signal = abortController.signal;
    }

    return window
      .fetch(url, fetchOpts)
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error("HTTP " + resp.status);
        }
        return resp.json();
      })
      .then(function (payload) {
        // Ignore stale out-of-order responses
        if (seq !== fetchSeq || destroyed) {
          return;
        }
        var rows = payload && Array.isArray(payload.reports) ? payload.reports : [];
        uiState.boundsLimit = Number((payload && payload.limit) || 500);

        // Replace in-bounds snapshot cleanly (deduplicated by report.id)
        reportsById = Object.create(null);
        for (var i = 0; i < rows.length; i += 1) {
          var normalized = normalizeReport(rows[i]);
          if (normalized) {
            reportsById[normalized.id] = normalized;
          }
        }

        uiState.loading = false;
        uiState.error = "";
        syncMarkersAndList();
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") {
          return;
        }
        if (seq !== fetchSeq || destroyed) {
          return;
        }
        lastQueryKey = "";
        uiState.loading = false;
        uiState.error = "Unable to load road hazards for this area. Please try again.";
        emitUiState();
      });
  }

  function scheduleBoundsLoad() {
    if (debounceTimer !== null) {
      window.clearTimeout(debounceTimer);
    }
    debounceTimer = window.setTimeout(function () {
      debounceTimer = null;
      loadReportsInBounds({ force: false });
    }, debounceMs);
  }

  function handleLiveEvent(eventPayload) {
    if (!eventPayload || typeof eventPayload !== "object" || destroyed) {
      return;
    }
    var eventType = String(eventPayload.event_type || "").toUpperCase();
    var reportId = String(
      eventPayload.id || (eventPayload.report && eventPayload.report.id) || ""
    );
    if (!reportId) {
      return;
    }

    if (eventType === "DELETE") {
      delete reportsById[reportId];
      removeMarkerById(reportId);
      syncMarkersAndList();
      return;
    }

    if (eventType === "INSERT" || eventType === "UPDATE") {
      var normalized = normalizeReport(eventPayload.report);
      if (!normalized) {
        return;
      }
      if (!isInsideCurrentBounds(normalized)) {
        // Do not add out-of-bounds reports; remove if an existing report moved out
        if (reportsById[normalized.id]) {
          delete reportsById[normalized.id];
          removeMarkerById(normalized.id);
          syncMarkersAndList();
        }
        return;
      }
      reportsById[normalized.id] = normalized;
      syncMarkersAndList();
    }
  }

  function closeEventSource() {
    if (eventSource) {
      try {
        eventSource.close();
      } catch (_err) {
        // Ignore close errors
      }
      eventSource = null;
    }
  }

  function scheduleReconnect() {
    if (destroyed || reconnectTimer !== null) {
      return;
    }
    var delay = Math.min(30000, 3000 * Math.pow(2, reconnectAttempts));
    reconnectAttempts = Math.min(reconnectAttempts + 1, 5);
    reconnectTimer = window.setTimeout(function () {
      reconnectTimer = null;
      connectLiveStream();
    }, delay);
  }

  function connectLiveStream() {
    if (destroyed || typeof window.EventSource !== "function") {
      uiState.liveStatus = "offline";
      emitUiState();
      return;
    }

    closeEventSource();
    uiState.liveStatus = hadConnectedOnce ? "reconnecting" : "connecting";
    emitUiState();

    var es = new window.EventSource("/api/reports/stream");
    eventSource = es;

    es.addEventListener("connected", function () {
      if (destroyed || es !== eventSource) {
        return;
      }
      var wasReconnect = hadConnectedOnce;
      hadConnectedOnce = true;
      reconnectAttempts = 0;
      uiState.liveStatus = "connected";
      emitUiState();
      if (wasReconnect) {
        loadReportsInBounds({ force: true });
      }
    });

    es.addEventListener("hazard_change", function (evt) {
      if (destroyed || es !== eventSource || !evt || !evt.data) {
        return;
      }
      try {
        handleLiveEvent(JSON.parse(evt.data));
      } catch (_parseErr) {
        // Ignore malformed frames safely
      }
    });

    es.addEventListener("reconnect", function () {
      if (destroyed || es !== eventSource) {
        return;
      }
      uiState.liveStatus = "reconnecting";
      emitUiState();
      closeEventSource();
      scheduleReconnect();
    });

    es.addEventListener("error", function () {
      if (destroyed || es !== eventSource) {
        return;
      }
      uiState.liveStatus = "reconnecting";
      emitUiState();
      if (es.readyState === window.EventSource.CLOSED) {
        closeEventSource();
        scheduleReconnect();
      }
    });
  }

  function toggleCategory(category) {
    var norm = normalizeCategory(category);
    var idx = uiState.selectedCategories.indexOf(norm);
    if (idx === -1) {
      uiState.selectedCategories.push(norm);
    } else {
      uiState.selectedCategories.splice(idx, 1);
    }
    syncMarkersAndList();
    loadReportsInBounds({ force: true });
  }

  function toggleStatus(status) {
    var norm = normalizeStatus(status);
    if (norm === "fixed") {
      toggleShowFixed();
      return;
    }
    var idx = uiState.selectedStatuses.indexOf(norm);
    if (idx === -1) {
      uiState.selectedStatuses.push(norm);
    } else {
      uiState.selectedStatuses.splice(idx, 1);
    }
    syncMarkersAndList();
    loadReportsInBounds({ force: true });
  }

  function toggleShowFixed() {
    uiState.showFixed = !uiState.showFixed;
    var fixedIdx = uiState.selectedStatuses.indexOf("fixed");
    if (uiState.showFixed && fixedIdx === -1) {
      uiState.selectedStatuses.push("fixed");
    } else if (!uiState.showFixed && fixedIdx !== -1) {
      uiState.selectedStatuses.splice(fixedIdx, 1);
    }
    syncMarkersAndList();
    loadReportsInBounds({ force: true });
  }

  function resetFilters() {
    uiState.selectedCategories = [];
    uiState.selectedStatuses = ["open", "escalated"];
    uiState.showFixed = false;
    syncMarkersAndList();
    loadReportsInBounds({ force: true });
  }

  function createClusterLayer() {
    if (window.L && typeof window.L.markerClusterGroup === "function") {
      usingClusterPlugin = true;
      return window.L.markerClusterGroup({
        showCoverageOnHover: false,
        maxClusterRadius: 50,
        spiderfyOnMaxZoom: true,
        disableClusteringAtZoom: 17,
        iconCreateFunction: function (cluster) {
          var children = cluster.getAllChildMarkers();
          var maxBand = "low";
          for (var i = 0; i < children.length; i += 1) {
            var band = children[i].options && children[i].options.severityBand;
            if (band === "high") {
              maxBand = "high";
              break;
            }
            if (band === "medium") {
              maxBand = "medium";
            }
          }
          var count = cluster.getChildCount();
          return window.L.divIcon({
            html:
              '<div class="sr-cluster-badge sr-cluster-badge--' +
              maxBand +
              '" aria-label="' +
              count +
              ' clustered hazards">' +
              count +
              "</div>",
            className: "sr-cluster-icon",
            iconSize: [40, 40],
          });
        },
      });
    }
    usingClusterPlugin = false;
    return window.L.layerGroup();
  }

  function mountOnMap() {
    if (mounted || destroyed) {
      return true;
    }
    if (!window.SafeRouteMap || typeof window.SafeRouteMap.getMap !== "function") {
      return false;
    }
    var map = window.SafeRouteMap.getMap();
    if (!map || !window.L) {
      return false;
    }

    mounted = true;
    mapRef = map;
    debounceMs = readDebounceMs();
    clusterGroup = createClusterLayer();
    clusterGroup.addTo(mapRef);

    moveEndHandler = function () {
      scheduleBoundsLoad();
    };
    mapRef.on("moveend", moveEndHandler);

    loadReportsInBounds({ force: true });
    connectLiveStream();
    return true;
  }

  function destroy() {
    destroyed = true;
    if (debounceTimer !== null) {
      window.clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (abortController && typeof abortController.abort === "function") {
      abortController.abort();
      abortController = null;
    }
    closeEventSource();
    if (mapRef && moveEndHandler) {
      mapRef.off("moveend", moveEndHandler);
      moveEndHandler = null;
    }
    if (clusterGroup && mapRef) {
      clusterGroup.clearLayers();
      mapRef.removeLayer(clusterGroup);
    }
    markersById = Object.create(null);
    reportsById = Object.create(null);
  }

  window.addEventListener("pagehide", destroy);
  window.addEventListener("beforeunload", destroy);

  // Public inspection & testing API
  window.SafeRouteHazards = {
    mount: mountOnMap,
    destroy: destroy,
    reload: function () {
      return loadReportsInBounds({ force: true });
    },
    toggleCategory: toggleCategory,
    toggleStatus: toggleStatus,
    toggleShowFixed: toggleShowFixed,
    resetFilters: resetFilters,
    selectReport: setSelectedReport,
    handleLiveEvent: handleLiveEvent,
    getState: function () {
      return Object.assign({}, uiState, {
        selectedCategories: uiState.selectedCategories.slice(),
        selectedStatuses: uiState.selectedStatuses.slice(),
        visibleReports: uiState.visibleReports.slice(),
      });
    },
    getMarkerCount: function () {
      return Object.keys(markersById).length;
    },
    getMarker: function (id) {
      return markersById[id] || null;
    },
    isClustered: function () {
      return usingClusterPlugin;
    },
    formatFreshness: formatFreshness,
    isStaleReport: isStaleReport,
  };

  function createHazardsPanel() {
    return {
      loading: true,
      error: "",
      selectedCategories: [],
      selectedStatuses: ["open", "escalated"],
      showFixed: false,
      selectedReportId: null,
      visibleReports: [],
      totalInBounds: 0,
      liveStatus: "connecting",
      categories: [
        { key: "pothole", label: "Pothole" },
        { key: "road_break", label: "Road break" },
        { key: "streetlight", label: "Streetlight" },
        { key: "footpath", label: "Footpath" },
        { key: "other", label: "Other" },
      ],

      init: function () {
        var self = this;
        uiListeners.push(function (snap) {
          self.loading = snap.loading;
          self.error = snap.error;
          self.selectedCategories = snap.selectedCategories;
          self.selectedStatuses = snap.selectedStatuses;
          self.showFixed = snap.showFixed;
          self.selectedReportId = snap.selectedReportId;
          self.visibleReports = snap.visibleReports;
          self.totalInBounds = snap.totalInBounds;
          self.liveStatus = snap.liveStatus;
        });

        // Wait briefly for B3 map.js init() if Alpine initializes child before parent
        if (!mountOnMap()) {
          var attempts = 0;
          var pollId = window.setInterval(function () {
            attempts += 1;
            if (mountOnMap() || attempts >= 40) {
              window.clearInterval(pollId);
            }
          }, 50);
        }
      },

      hasActiveFilters: function () {
        return (
          this.selectedCategories.length > 0 ||
          this.showFixed ||
          this.selectedStatuses.length !== 2 ||
          this.selectedStatuses.indexOf("open") === -1 ||
          this.selectedStatuses.indexOf("escalated") === -1
        );
      },

      isCategorySelected: function (key) {
        return this.selectedCategories.indexOf(key) !== -1;
      },

      isStatusSelected: function (key) {
        return this.selectedStatuses.indexOf(key) !== -1;
      },

      toggleCategory: function (key) {
        toggleCategory(key);
      },

      toggleStatus: function (key) {
        toggleStatus(key);
      },

      toggleShowFixed: function () {
        toggleShowFixed();
      },

      resetFilters: function () {
        resetFilters();
      },

      retryLoad: function () {
        loadReportsInBounds({ force: true });
      },

      selectReport: function (reportId) {
        setSelectedReport(reportId, true);
      },
    };
  }

  function registerHazardsPanel() {
    if (window.Alpine && typeof window.Alpine.data === "function") {
      window.Alpine.data("srHazardsPanel", createHazardsPanel);
    }
  }

  document.addEventListener("alpine:init", registerHazardsPanel);
  if (window.Alpine) {
    registerHazardsPanel();
  }
})();
