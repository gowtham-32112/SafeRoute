/**
 * SafeRoute Hazard-Aware Route Planner Controller (Issue #30 — C3/C4).
 *
 * Security & privacy guarantees:
 * - Never persists live user coordinates in browser client state (zero persistent storage).
 * - Enforces double-submit CSRF protection on POST /api/routes/plan.
 * - Renders all untrusted hazard labels and descriptions strictly via safe DOM textContent (zero innerHTML).
 * - Light-theme compliant with high-contrast accessibility.
 */
(function () {
  "use strict";

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

  function readJsonScript(id, defaultVal) {
    var el = document.getElementById(id);
    if (el && el.textContent) {
      try {
        return JSON.parse(el.textContent);
      } catch (err) {
        // Fall through
      }
    }
    return defaultVal;
  }

  function notifyToast(type, message, title) {
    if (window.SafeRouteToast && typeof window.SafeRouteToast[type] === "function") {
      window.SafeRouteToast[type](message, title || "");
    }
  }

  window.srRoutePlanner = function () {
    var mapConfig = readJsonScript("sr-routes-config", {
      fallbackLat: 17.4849,
      fallbackLng: 78.4138,
      fallbackLabel: "Kukatpally Y Junction",
      defaultZoom: 14,
      geolocationTimeoutMs: 5000,
    });

    var landmarks = readJsonScript("sr-routes-landmarks", [
      { name: "Kukatpally Y Junction", lat: 17.4849, lng: 78.4138 },
      { name: "KPHB Colony Metro Station", lat: 17.4930, lng: 78.3980 },
      { name: "JNTU Hyderabad Junction", lat: 17.4975, lng: 78.3888 },
      { name: "Ameerpet Metro Junction", lat: 17.4375, lng: 78.4483 },
      { name: "Miyapur Metro Terminal", lat: 17.4968, lng: 78.3614 },
    ]);

    return {
      originLat: mapConfig.fallbackLat || 17.4849,
      originLng: mapConfig.fallbackLng || 78.4138,
      destLat: 17.4930,
      destLng: 78.3980,
      clickMode: null, // null | 'origin' | 'dest'
      locating: false,
      planning: false,
      formError: "",
      landmarks: landmarks,
      routes: [],
      selectedRouteIdx: 0,
      isFallbackRoute: false,
      fallbackNotice: "",

      // Leaflet references
      map: null,
      originMarker: null,
      destMarker: null,
      routePolylinesLayer: null,
      hazardMarkersLayer: null,

      init: function () {
        var self = this;
        this.$nextTick(function () {
          self.initMap();
        });
      },

      initMap: function () {
        var container = document.getElementById("sr-routes-map");
        if (!container || !window.L) {
          return;
        }

        try {
          this.map = window.L.map(container, {
            center: [this.originLat, this.originLng],
            zoom: mapConfig.defaultZoom || 14,
            zoomControl: true,
            scrollWheelZoom: true,
          });

          window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>',
            maxZoom: 19,
          }).addTo(this.map);

          this.routePolylinesLayer = window.L.layerGroup().addTo(this.map);
          this.hazardMarkersLayer = window.L.layerGroup().addTo(this.map);

          var self = this;

          // Origin marker (A)
          var originIcon = window.L.divIcon({
            className: "sr-waypoint-marker",
            html: '<span style="display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:50%;background:#106EBE;color:#FFFFFF;font-weight:800;font-size:12px;box-shadow:0 2px 6px rgba(0,0,0,0.3);border:2px solid #FFFFFF;">A</span>',
            iconSize: [28, 28],
            iconAnchor: [14, 14],
          });

          this.originMarker = window.L.marker([this.originLat, this.originLng], {
            draggable: true,
            icon: originIcon,
            title: "Origin (Drag to move)",
          }).addTo(this.map);

          this.originMarker.on("dragend", function (evt) {
            var pos = evt.target.getLatLng();
            self.originLat = Number(pos.lat.toFixed(6));
            self.originLng = Number(pos.lng.toFixed(6));
          });

          // Destination marker (B)
          var destIcon = window.L.divIcon({
            className: "sr-waypoint-marker",
            html: '<span style="display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:50%;background:#D92D20;color:#FFFFFF;font-weight:800;font-size:12px;box-shadow:0 2px 6px rgba(0,0,0,0.3);border:2px solid #FFFFFF;">B</span>',
            iconSize: [28, 28],
            iconAnchor: [14, 14],
          });

          this.destMarker = window.L.marker([this.destLat, this.destLng], {
            draggable: true,
            icon: destIcon,
            title: "Destination (Drag to move)",
          }).addTo(this.map);

          this.destMarker.on("dragend", function (evt) {
            var pos = evt.target.getLatLng();
            self.destLat = Number(pos.lat.toFixed(6));
            self.destLng = Number(pos.lng.toFixed(6));
          });

          // Map click handler for picking points
          this.map.on("click", function (evt) {
            if (!self.clickMode) {
              return;
            }
            var lat = Number(evt.latlng.lat.toFixed(6));
            var lng = Number(evt.latlng.lng.toFixed(6));

            if (self.clickMode === "origin") {
              self.originLat = lat;
              self.originLng = lng;
              self.originMarker.setLatLng([lat, lng]);
              self.clickMode = "dest";
              notifyToast("info", "Origin set. Now click on the map to set Destination.", "Point A Set");
            } else if (self.clickMode === "dest") {
              self.destLat = lat;
              self.destLng = lng;
              self.destMarker.setLatLng([lat, lng]);
              self.clickMode = null;
              notifyToast("success", "Destination set. Click 'Plan Safe Route' to compute risk.", "Point B Set");
            }
          });

          // Ensure map dimensions settle
          window.setTimeout(function () {
            if (self.map) {
              self.map.invalidateSize();
            }
          }, 250);
        } catch (e) {
          // Fallback if Leaflet fails
        }
      },

      toggleClickMode: function (mode) {
        if (this.clickMode === mode) {
          this.clickMode = null;
        } else {
          this.clickMode = mode;
          var label = mode === "origin" ? "Origin (Point A)" : "Destination (Point B)";
          notifyToast("info", "Click anywhere on the map to place " + label + ".", "Map Pick Mode");
        }
      },

      updateMarkerPosition: function (type) {
        if (type === "origin" && this.originMarker) {
          this.originMarker.setLatLng([this.originLat, this.originLng]);
          if (this.map) {
            this.map.panTo([this.originLat, this.originLng]);
          }
        } else if (type === "dest" && this.destMarker) {
          this.destMarker.setLatLng([this.destLat, this.destLng]);
          if (this.map) {
            this.map.panTo([this.destLat, this.destLng]);
          }
        }
      },

      selectLandmark: function (type, landmarkName) {
        if (!landmarkName) {
          return;
        }
        for (var i = 0; i < this.landmarks.length; i += 1) {
          if (this.landmarks[i].name === landmarkName) {
            var lm = this.landmarks[i];
            if (type === "origin") {
              this.originLat = lm.lat;
              this.originLng = lm.lng;
              this.updateMarkerPosition("origin");
            } else {
              this.destLat = lm.lat;
              this.destLng = lm.lng;
              this.updateMarkerPosition("dest");
            }
            break;
          }
        }
      },

      swapWaypoints: function () {
        var tempLat = this.originLat;
        var tempLng = this.originLng;
        this.originLat = this.destLat;
        this.originLng = this.destLng;
        this.destLat = tempLat;
        this.destLng = tempLng;
        this.updateMarkerPosition("origin");
        this.updateMarkerPosition("dest");
        notifyToast("info", "Origin and Destination swapped.", "Waypoints Updated");
      },

      useMyLocation: function () {
        if (!navigator.geolocation) {
          notifyToast("info", "Browser geolocation not supported on this device.", "Location Notice");
          return;
        }

        var self = this;
        this.locating = true;
        navigator.geolocation.getCurrentPosition(
          function (pos) {
            self.locating = false;
            self.originLat = Number(pos.coords.latitude.toFixed(6));
            self.originLng = Number(pos.coords.longitude.toFixed(6));
            self.updateMarkerPosition("origin");
            notifyToast("success", "Origin set to your current location.", "Location Detected");
          },
          function (err) {
            self.locating = false;
            var reason = "Location access denied or timed out";
            if (err && err.code === 1) {
              reason = "Location permission denied";
            }
            notifyToast("info", reason + ". Using default coordinates.", "Location Notice");
          },
          { timeout: 5000, enableHighAccuracy: true, maximumAge: 30000 }
        );
      },

      planRoutes: function () {
        this.formError = "";

        // Client validation
        if (typeof this.originLat !== "number" || typeof this.originLng !== "number" ||
            typeof this.destLat !== "number" || typeof this.destLng !== "number") {
          this.formError = "Please specify valid numeric coordinates for origin and destination.";
          return;
        }

        var latDiff = Math.abs(this.originLat - this.destLat);
        var lngDiff = Math.abs(this.originLng - this.destLng);
        if (latDiff < 0.0001 && lngDiff < 0.0001) {
          this.formError = "Origin and destination cannot be identical.";
          return;
        }

        this.planning = true;
        var formEl = document.getElementById("sr-route-form");
        var csrfToken = resolveCsrfToken(formEl);

        var payload = {
          origin_lat: this.originLat,
          origin_lng: this.originLng,
          dest_lat: this.destLat,
          dest_lng: this.destLng,
        };

        var self = this;

        fetch("/api/routes/plan", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify(payload),
          credentials: "same-origin",
        })
          .then(function (res) {
            return res.json().then(function (data) {
              return { ok: res.ok, status: res.status, data: data };
            });
          })
          .then(function (result) {
            self.planning = false;
            if (!result.ok) {
              var errMsg = "Failed to plan routes.";
              if (result.data && result.data.detail) {
                errMsg = typeof result.data.detail === "string" ? result.data.detail : JSON.stringify(result.data.detail);
              }
              self.formError = errMsg;
              notifyToast("error", errMsg, "Routing Error");
              return;
            }

            var planData = result.data;
            self.routes = planData.routes || [];
            self.isFallbackRoute = Boolean(planData.is_fallback_route);
            self.fallbackNotice = planData.fallback_notice || "";
            self.selectedRouteIdx = 0;

            if (self.routes.length > 0) {
              self.renderRoutesOnMap();
              notifyToast("success", "Calculated " + self.routes.length + " alternative routes ranked by hazard risk.", "Routes Ranked");
            } else {
              self.formError = "No drivable route found between these locations.";
            }
          })
          .catch(function (err) {
            self.planning = false;
            self.formError = "A network error occurred while connecting to the routing planner.";
            notifyToast("error", "Unable to connect to route planner service.", "Network Error");
          });
      },

      selectRoute: function (idx) {
        if (idx >= 0 && idx < this.routes.length) {
          this.selectedRouteIdx = idx;
          this.renderRoutesOnMap();
        }
      },

      renderRoutesOnMap: function () {
        if (!this.map || !this.routePolylinesLayer || !this.hazardMarkersLayer) {
          return;
        }

        this.routePolylinesLayer.clearLayers();
        this.hazardMarkersLayer.clearLayers();

        var bounds = window.L.latLngBounds();
        bounds.extend([this.originLat, this.originLng]);
        bounds.extend([this.destLat, this.destLng]);

        var self = this;

        // Draw alternative routes (unselected first, selected last on top)
        for (var i = 0; i < this.routes.length; i += 1) {
          var route = this.routes[i];
          var isSelected = (i === this.selectedRouteIdx);
          var geom = route.geojson;

          if (geom && geom.coordinates && geom.coordinates.length > 0) {
            // GeoJSON coordinates are [lng, lat] -> convert to Leaflet [lat, lng]
            var latLngs = [];
            for (var c = 0; c < geom.coordinates.length; c += 1) {
              var pt = geom.coordinates[c];
              latLngs.push([pt[1], pt[0]]);
              if (isSelected) {
                bounds.extend([pt[1], pt[0]]);
              }
            }

            var polyStyle = isSelected
              ? {
                  color: "#106EBE",
                  weight: 6,
                  opacity: 0.95,
                  lineCap: "round",
                  lineJoin: "round",
                }
              : {
                  color: "#5B6B7C",
                  weight: 4,
                  opacity: 0.65,
                  dashArray: "6, 6",
                  lineCap: "round",
                };

            var poly = window.L.polyline(latLngs, polyStyle);
            poly.routeIndex = i;

            // Clicking polyline selects the route
            poly.on("click", (function (routeIndex) {
              return function () {
                self.selectRoute(routeIndex);
              };
            })(i));

            this.routePolylinesLayer.addLayer(poly);
          }
        }

        // Draw hazards along the currently selected route
        var activeRoute = this.routes[this.selectedRouteIdx];
        if (activeRoute && activeRoute.hazards && activeRoute.hazards.length > 0) {
          for (var h = 0; h < activeRoute.hazards.length; h += 1) {
            var hazard = activeRoute.hazards[h];
            var markerColor = "#2E9E5B"; // Low
            if (hazard.severity_band === "high") {
              markerColor = "#D92D20";
            } else if (hazard.severity_band === "medium") {
              markerColor = "#F5A524";
            }

            var circle = window.L.circleMarker([hazard.lat, hazard.lng], {
              radius: 7,
              fillColor: markerColor,
              color: "#FFFFFF",
              weight: 2,
              opacity: 1,
              fillOpacity: 0.9,
            });

            // Build safe DOM popup content without innerHTML
            var popupContent = this.buildHazardPopup(hazard);
            circle.bindPopup(popupContent);
            this.hazardMarkersLayer.addLayer(circle);
          }
        }

        if (bounds.isValid()) {
          this.map.fitBounds(bounds, { padding: [50, 50], maxZoom: 16 });
        }
      },

      buildHazardPopup: function (hazard) {
        var container = document.createElement("div");
        container.className = "space-y-1.5 p-1 text-xs";

        var header = document.createElement("div");
        header.className = "flex items-center justify-between gap-2 border-b pb-1 font-bold";

        var titleSpan = document.createElement("span");
        titleSpan.className = "capitalize text-ink";
        titleSpan.textContent = (hazard.category || "Hazard").replace("_", " ");

        var scoreBadge = document.createElement("span");
        scoreBadge.className = "px-1.5 py-0.5 rounded text-[10px] font-extrabold text-white";
        if (hazard.severity_band === "high") {
          scoreBadge.style.backgroundColor = "#D92D20";
        } else if (hazard.severity_band === "medium") {
          scoreBadge.style.backgroundColor = "#F5A524";
        } else {
          scoreBadge.style.backgroundColor = "#2E9E5B";
        }
        scoreBadge.textContent = "Severity " + hazard.severity_score;

        header.appendChild(titleSpan);
        header.appendChild(scoreBadge);
        container.appendChild(header);

        if (hazard.description) {
          var desc = document.createElement("p");
          desc.className = "text-[11px] text-muted leading-tight";
          desc.textContent = hazard.description;
          container.appendChild(desc);
        }

        var meta = document.createElement("p");
        meta.className = "text-[10px] text-muted";
        meta.textContent = "Distance to route: " + hazard.distance_m + "m (Weight: " + hazard.multiplier + "x)";
        container.appendChild(meta);

        var link = document.createElement("a");
        link.href = "/reports/" + encodeURIComponent(hazard.id);
        link.target = "_blank";
        link.className = "inline-block text-[11px] font-bold text-primary hover:underline pt-0.5";
        link.textContent = "View report details →";
        container.appendChild(link);

        return container;
      },
    };
  };
})();
