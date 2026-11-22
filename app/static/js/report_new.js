/**
 * SafeRoute Hazard Report Form Controller (Issue #14 — B5).
 *
 * Security & privacy guarantees:
 * - Submits strictly to authenticated FastAPI endpoints (/reports, /reports/check-duplicate, /reports/confirm-duplicate).
 * - Never persists live user coordinates in browser client storage.
 * - Renders all untrusted report content via safe text bindings.
 * - Validates evidence photos client-side (max 5 MB, JPEG/PNG/WebP only) before upload.
 */
(function () {
  "use strict";

  var MAX_PHOTO_BYTES = 5 * 1024 * 1024; // 5 MB
  var ALLOWED_MIME_TYPES = ["image/jpeg", "image/png", "image/webp"];

  var CATEGORIES = [
    {
      key: "pothole",
      label: "Pothole",
      description: "Craters, road cavities, missing tarmac, asphalt depressions",
    },
    {
      key: "road_break",
      label: "Road break",
      description: "Major cracks, shoulder collapses, missing pavement blocks",
    },
    {
      key: "streetlight",
      label: "Streetlight",
      description: "Unlit poles, broken light fixtures, dark pedestrian crossings",
    },
    {
      key: "footpath",
      label: "Footpath",
      description: "Broken slabs, missing paver blocks, pedestrian obstructions",
    },
    {
      key: "other",
      label: "Other hazard",
      description: "Uncovered manholes, loose gravel/debris, road hazards",
    },
  ];

  function readCookie(name) {
    var prefix = name + "=";
    var parts = document.cookie ? document.cookie.split(";") : [];
    for (var i = 0; i < parts.length; i += 1) {
      var item = parts[i].trim();
      if (item.indexOf(prefix) === 0) {
        return decodeURIComponent(item.slice(prefix.length));
      }
    }
    return "";
  }

  function resolveCsrfToken(formEl) {
    var cookieToken = readCookie("sr_csrf_token");
    if (cookieToken) {
      return cookieToken;
    }
    if (formEl) {
      var hidden = formEl.querySelector('input[name="csrf_token"]');
      if (hidden && hidden.value) {
        return hidden.value;
      }
    }
    return "";
  }

  function readMapConfig() {
    var configEl = document.getElementById("sr-map-config");
    if (configEl && configEl.textContent) {
      try {
        return JSON.parse(configEl.textContent);
      } catch (err) {
        // Fall through to defaults
      }
    }
    return {
      fallbackLat: 17.4849,
      fallbackLng: 78.4138,
      fallbackLabel: "Kukatpally Y Junction",
      defaultZoom: 16,
      geolocationTimeoutMs: 5000,
    };
  }

  function formatFreshness(isoString) {
    if (!isoString) {
      return "Recently reported";
    }
    var ts = Date.parse(isoString);
    if (!Number.isFinite(ts)) {
      return "Recently reported";
    }
    var diffSec = Math.max(0, Math.floor((Date.now() - ts) / 1000));
    var days = Math.floor(diffSec / 86400);
    if (days === 0) {
      return "Reported today";
    }
    if (days === 1) {
      return "Reported yesterday";
    }
    return "Reported " + days + " days ago";
  }

  function notifyToast(type, message, title) {
    if (window.SafeRouteToast && typeof window.SafeRouteToast[type] === "function") {
      window.SafeRouteToast[type](message, title || "");
    }
  }

  // Active form state holder for external hook (e.g. AI suggestion hook)
  var activeFormInstance = null;

  window.SafeRouteReportForm = {
    setAiSuggestion: function (suggestion) {
      var rootEl = document.querySelector('[x-data*="srReportForm"]') || document.getElementById("sr-report-form");
      if (rootEl && window.Alpine && typeof window.Alpine.$data === "function") {
        var data = window.Alpine.$data(rootEl);
        if (data && typeof data.receiveAiSuggestion === "function") {
          data.receiveAiSuggestion(suggestion);
          return;
        }
      }
      if (activeFormInstance && typeof activeFormInstance.receiveAiSuggestion === "function") {
        activeFormInstance.receiveAiSuggestion(suggestion);
      }
    },
  };

  function createReportForm() {
    var mapConfig = readMapConfig();

    return {
      category: "",
      description: "",
      addressText: "",
      lat: mapConfig.fallbackLat,
      lng: mapConfig.fallbackLng,
      locationStatus: "Default map area: " + mapConfig.fallbackLabel + ". Tap 'Use my location' or drag the marker.",
      locating: false,
      hasUserLocation: false,

      // Evidence photo state
      photoFile: null,
      photoPreviewUrl: "",
      photoFileName: "",
      photoFileSize: "",
      photoError: "",

      // AI suggestion slot state (Issue #14, C2 hook)
      aiSuggestion: null,

      // General form state
      formError: "",
      submitting: false,
      checkingDuplicate: false,

      // Duplicate-found modal state (Spec Section 8)
      duplicateModalOpen: false,
      duplicateMatch: null,
      confirmingDuplicate: false,

      categories: CATEGORIES,

      // Leaflet references
      map: null,
      marker: null,

      init: function () {
        activeFormInstance = this;
        var self = this;
        // Wait until DOM and Leaflet are ready
        this.$nextTick(function () {
          self.initMap();
        });
      },

      initMap: function () {
        var container = document.getElementById("sr-picker-map");
        if (!container || !window.L) {
          return;
        }

        var mapConfig = readMapConfig();
        var initialLat = this.lat || mapConfig.fallbackLat;
        var initialLng = this.lng || mapConfig.fallbackLng;

        try {
          this.map = window.L.map(container, {
            center: [initialLat, initialLng],
            zoom: mapConfig.defaultZoom || 16,
            zoomControl: true,
            scrollWheelZoom: true,
          });

          window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>',
            maxZoom: 19,
          }).addTo(this.map);

          var self = this;

          // Interactive draggable marker
          this.marker = window.L.marker([initialLat, initialLng], {
            draggable: true,
            title: "Selected hazard location (drag to reposition)",
            alt: "Selected hazard location marker",
          }).addTo(this.map);

          this.marker.on("dragend", function (evt) {
            var latLng = evt.target.getLatLng();
            self.updateCoordinates(latLng.lat, latLng.lng, "Marker dragged to new coordinates.");
          });

          this.map.on("click", function (evt) {
            self.marker.setLatLng(evt.latlng);
            self.updateCoordinates(evt.latlng.lat, evt.latlng.lng, "Marker moved to clicked location.");
          });

          // Invalidate size once visible
          window.setTimeout(function () {
            if (self.map) {
              self.map.invalidateSize();
            }
          }, 200);
        } catch (err) {
          // Leaflet initialization fallback
        }
      },

      updateCoordinates: function (newLat, newLng, statusMsg) {
        this.lat = Number(Number(newLat).toFixed(6));
        this.lng = Number(Number(newLng).toFixed(6));
        if (statusMsg) {
          this.locationStatus = statusMsg;
        }
      },

      useMyLocation: function () {
        if (!navigator.geolocation) {
          this.locationStatus = "Browser geolocation is not supported on this device. Using default coordinates.";
          notifyToast("info", "Geolocation not supported. Using default map area.", "Location Notice");
          return;
        }

        var self = this;
        this.locating = true;
        this.locationStatus = "Checking your browser location...";

        var mapConfig = readMapConfig();
        var timeoutMs = mapConfig.geolocationTimeoutMs || 5000;

        navigator.geolocation.getCurrentPosition(
          function (pos) {
            self.locating = false;
            self.hasUserLocation = true;
            var lat = pos.coords.latitude;
            var lng = pos.coords.longitude;
            self.updateCoordinates(lat, lng, "Location detected from browser.");

            if (self.map && self.marker) {
              self.map.setView([lat, lng], 17);
              self.marker.setLatLng([lat, lng]);
            }
            notifyToast("success", "Location detected successfully.", "Location Updated");
          },
          function (err) {
            self.locating = false;
            self.hasUserLocation = false;
            var reason = "Location access denied or timed out";
            if (err && err.code === 1) {
              reason = "Location access was denied";
            } else if (err && err.code === 3) {
              reason = "Location request timed out (5s)";
            }
            self.locationStatus = reason + ". Default area: " + mapConfig.fallbackLabel + ".";
            notifyToast("info", reason + ". Centered on default area.", "Location Notice");
          },
          {
            timeout: timeoutMs,
            enableHighAccuracy: true,
            maximumAge: 30000,
          }
        );
      },

      selectCategory: function (key) {
        this.category = key;
        this.formError = "";
      },

      getCategoryLabel: function (key) {
        for (var i = 0; i < CATEGORIES.length; i += 1) {
          if (CATEGORIES[i].key === key) {
            return CATEGORIES[i].label;
          }
        }
        return key || "";
      },

      triggerPhotoInput: function () {
        var input = document.getElementById("sr-photo-input");
        if (input) {
          input.click();
        }
      },

      handleFileChange: function (evt) {
        var files = evt.target && evt.target.files;
        if (files && files.length > 0) {
          this.processSelectedFile(files[0]);
        }
      },

      handleFileDrop: function (evt) {
        var files = evt.dataTransfer && evt.dataTransfer.files;
        if (files && files.length > 0) {
          this.processSelectedFile(files[0]);
        }
      },

      processSelectedFile: function (file) {
        this.photoError = "";

        if (!file) {
          return;
        }

        // Validate MIME type
        if (ALLOWED_MIME_TYPES.indexOf(file.type) === -1) {
          this.photoError = "Please upload a valid JPEG, PNG, or WebP photograph.";
          this.resetFileInput();
          return;
        }

        // Validate max size (5 MB)
        if (file.size > MAX_PHOTO_BYTES) {
          this.photoError = "Image exceeds the 5 MB maximum upload limit.";
          this.resetFileInput();
          return;
        }

        this.photoFile = file;
        this.photoFileName = file.name || "hazard-evidence.jpg";
        this.photoFileSize = (file.size / (1024 * 1024)).toFixed(2) + " MB";

        // Clean up previous preview URL
        if (this.photoPreviewUrl) {
          try {
            window.URL.revokeObjectURL(this.photoPreviewUrl);
          } catch (e) {
            // Ignore URL cleanup error
          }
        }

        try {
          this.photoPreviewUrl = window.URL.createObjectURL(file);
        } catch (e) {
          this.photoPreviewUrl = "";
        }
      },

      removePhoto: function () {
        if (this.photoPreviewUrl) {
          try {
            window.URL.revokeObjectURL(this.photoPreviewUrl);
          } catch (e) {
            // Ignore URL cleanup error
          }
        }
        this.photoFile = null;
        this.photoPreviewUrl = "";
        this.photoFileName = "";
        this.photoFileSize = "";
        this.photoError = "";
        this.resetFileInput();
      },

      resetFileInput: function () {
        var input = document.getElementById("sr-photo-input");
        if (input) {
          input.value = "";
        }
      },

      // AI Suggestion integration hook (Issue #14 & future C2)
      receiveAiSuggestion: function (suggestion) {
        if (!suggestion || !suggestion.category) {
          return;
        }
        this.aiSuggestion = {
          category: suggestion.category,
          label: this.getCategoryLabel(suggestion.category),
          confidence: Number(suggestion.confidence || 0),
          reason: suggestion.reason || "Vision model suggestion based on uploaded photo.",
        };
      },

      applyAiSuggestion: function () {
        if (this.aiSuggestion && this.aiSuggestion.category) {
          this.selectCategory(this.aiSuggestion.category);
          notifyToast("success", "Applied suggested category: " + this.aiSuggestion.label, "AI Suggestion");
        }
      },

      closeDuplicateModal: function () {
        this.duplicateModalOpen = false;
        this.duplicateMatch = null;
      },

      handleSubmit: function (evt) {
        this.formError = "";

        // 1. Client-Side Field Validation
        if (!this.category) {
          this.formError = "Please select a hazard category.";
          return;
        }

        if (!this.photoFile) {
          this.formError = "An evidence photograph is required to report a hazard.";
          return;
        }

        if (!this.description || !this.description.trim()) {
          this.formError = "Please provide a description of the road hazard.";
          return;
        }

        if (typeof this.lat !== "number" || typeof this.lng !== "number" || !Number.isFinite(this.lat) || !Number.isFinite(this.lng)) {
          this.formError = "Please select a valid location on the map.";
          return;
        }

        // 2. Perform authoritative duplicate check at 30m radius (Spec Section 8)
        this.checkDuplicatesAndProceed();
      },

      checkDuplicatesAndProceed: function () {
        var self = this;
        var formEl = document.getElementById("sr-report-form");
        var csrfToken = resolveCsrfToken(formEl);

        this.checkingDuplicate = true;

        var checkPayload = {
          lat: this.lat,
          lng: this.lng,
          category: this.category,
        };

        window.fetch("/reports/check-duplicate", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify(checkPayload),
        })
          .then(function (resp) {
            if (!resp.ok) {
              // If duplicate check endpoint fails, do not block citizen reporting
              return null;
            }
            return resp.json();
          })
          .catch(function () {
            return null;
          })
          .then(function (data) {
            self.checkingDuplicate = false;

            if (data && data.has_duplicates && Array.isArray(data.matches) && data.matches.length > 0) {
              // Duplicate found! Surface modal to the user per Spec Section 8
              var match = data.matches[0];
              self.duplicateMatch = {
                id: match.id,
                category: match.category,
                categoryLabel: self.getCategoryLabel(match.category),
                description: match.description || "",
                distance: match.distance_m ? Math.round(match.distance_m) : 0,
                distanceText: "Located " + (match.distance_m ? Math.round(match.distance_m) : 0) + " metres away",
                status: (match.status || "open").toUpperCase(),
                freshness: formatFreshness(match.created_at),
              };
              self.duplicateModalOpen = true;
              return;
            }

            // No duplicate found: submit new report directly
            self.submitNewReport();
          });
      },

      confirmExistingDuplicate: function () {
        if (!this.duplicateMatch || !this.duplicateMatch.id) {
          return;
        }

        var self = this;
        var formEl = document.getElementById("sr-report-form");
        var csrfToken = resolveCsrfToken(formEl);

        this.confirmingDuplicate = true;

        var confirmPayload = {
          report_id: this.duplicateMatch.id,
          lat: this.lat,
          lng: this.lng,
          category: this.category,
        };

        window.fetch("/reports/confirm-duplicate", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify(confirmPayload),
        })
          .then(function (resp) {
            self.confirmingDuplicate = false;
            if (!resp.ok) {
              return resp.json().then(function (err) {
                var msg = (err && err.detail) || "Could not confirm existing hazard.";
                self.formError = msg;
                self.closeDuplicateModal();
              });
            }
            return resp.json().then(function () {
              self.closeDuplicateModal();
              notifyToast("success", "Added your confirmation to the existing hazard report.", "Report Confirmed");
              window.setTimeout(function () {
                window.location.href = "/map";
              }, 400);
            });
          })
          .catch(function () {
            self.confirmingDuplicate = false;
            self.formError = "Network error while confirming existing hazard.";
            self.closeDuplicateModal();
          });
      },

      submitAnyway: function () {
        this.closeDuplicateModal();
        this.submitNewReport();
      },

      submitNewReport: function () {
        var self = this;
        var formEl = document.getElementById("sr-report-form");
        var csrfToken = resolveCsrfToken(formEl);

        this.submitting = true;
        this.formError = "";

        var formData = new FormData();
        formData.append("category", this.category);
        formData.append("description", this.description.trim());
        formData.append("lat", String(this.lat));
        formData.append("lng", String(this.lng));
        if (this.addressText && this.addressText.trim()) {
          formData.append("address_text", this.addressText.trim());
        }
        if (this.photoFile) {
          formData.append("image", this.photoFile);
        }
        formData.append("csrf_token", csrfToken);

        window.fetch("/reports", {
          method: "POST",
          headers: {
            "X-CSRF-Token": csrfToken,
          },
          body: formData,
        })
          .then(function (resp) {
            self.submitting = false;
            if (!resp.ok) {
              return resp.json().then(function (err) {
                var msg = (err && err.detail) || "Failed to submit hazard report. Please check the details and try again.";
                self.formError = msg;
              });
            }
            return resp.json().then(function (data) {
              notifyToast("success", "Hazard report submitted successfully!", "Report Created");
              window.setTimeout(function () {
                window.location.href = "/map";
              }, 400);
            });
          })
          .catch(function () {
            self.submitting = false;
            self.formError = "Network connection error while submitting report. Please try again.";
          });
      },
    };
  }

  window.srReportForm = createReportForm;

  function registerReportForm() {
    if (window.Alpine && typeof window.Alpine.data === "function") {
      window.Alpine.data("srReportForm", createReportForm);
    }
  }

  document.addEventListener("alpine:init", registerReportForm);
  if (window.Alpine) {
    registerReportForm();
  }
})();
