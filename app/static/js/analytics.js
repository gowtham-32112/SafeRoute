/**
 * SafeRoute Hazard Analytics & Heatmap Dashboard Controller (Issue #32 — C5).
 *
 * Security & privacy guarantees:
 * - Queries strictly through authenticated FastAPI endpoints (/api/analytics).
 * - Zero client-side persistence of live user coordinates (no persistent storage).
 * - Renders all dynamic chart legends and table rows via safe Alpine DOM bindings (zero innerHTML).
 * - Light-theme compliant with high-contrast accessibility.
 */
(function () {
  "use strict";

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

  window.srAnalyticsDashboard = function () {
    var initialData = readJsonScript("sr-initial-analytics", {
      total_reports: 0,
      verification_rate: 0.0,
      duplicate_rate: 0.0,
      escalation_count: 0,
      avg_time_to_fix_hours: null,
      by_category: {},
      by_status: {},
      category_breakdown: [],
      status_breakdown: [],
      top_affected_areas: [],
      heatmap_points: [],
      exclude_seed: false,
    });

    var mapConfig = readJsonScript("sr-analytics-map-config", {
      fallbackLat: 17.4849,
      fallbackLng: 78.4138,
      fallbackLabel: "Kukatpally Y Junction",
      defaultZoom: 13,
    });

    return {
      excludeSeed: Boolean(initialData.exclude_seed),
      loading: false,
      data: initialData,

      // Leaflet & Chart references
      map: null,
      heatLayer: null,
      categoryChart: null,
      statusChart: null,

      init: function () {
        var self = this;
        this.$nextTick(function () {
          self.initHeatmap();
          self.initCharts();
        });
      },

      initHeatmap: function () {
        var container = document.getElementById("sr-analytics-heatmap");
        if (!container || !window.L) {
          return;
        }

        try {
          var lat = mapConfig.fallbackLat || 17.4849;
          var lng = mapConfig.fallbackLng || 78.4138;

          this.map = window.L.map(container, {
            center: [lat, lng],
            zoom: mapConfig.defaultZoom || 13,
            zoomControl: true,
            scrollWheelZoom: true,
          });

          window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>',
            maxZoom: 19,
          }).addTo(this.map);

          if (typeof window.L.heatLayer === "function") {
            var points = this.data.heatmap_points || [];
            this.heatLayer = window.L.heatLayer(points, {
              radius: 25,
              blur: 15,
              maxZoom: 17,
              minOpacity: 0.35,
              gradient: {
                0.2: "#3B82F6",
                0.4: "#84CC16",
                0.7: "#F5A524",
                1.0: "#D92D20",
              },
            }).addTo(this.map);
          }

          var self = this;
          window.setTimeout(function () {
            if (self.map) {
              self.map.invalidateSize();
            }
          }, 250);
        } catch (e) {
          // Leaflet initialization fallback
        }
      },

      initCharts: function () {
        if (!window.Chart) {
          return;
        }

        // 1. Category Chart (Bar Chart)
        var catCanvas = document.getElementById("sr-category-chart");
        if (catCanvas) {
          var catLabels = [];
          var catData = [];
          for (var i = 0; i < this.data.category_breakdown.length; i += 1) {
            catLabels.push(this.data.category_breakdown[i].label);
            catData.push(this.data.category_breakdown[i].count);
          }

          this.categoryChart = new window.Chart(catCanvas, {
            type: "bar",
            data: {
              labels: catLabels,
              datasets: [
                {
                  label: "Reports",
                  data: catData,
                  backgroundColor: [
                    "#106EBE",
                    "#0B4F8A",
                    "#F5A524",
                    "#2E9E5B",
                    "#5B6B7C",
                  ],
                  borderRadius: 6,
                },
              ],
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              plugins: {
                legend: { display: false },
              },
              scales: {
                y: {
                  beginAtZero: true,
                  ticks: { precision: 0 },
                  grid: { color: "#E6F1FA" },
                },
                x: {
                  grid: { display: false },
                  ticks: { font: { size: 10 } },
                },
              },
            },
          });
        }

        // 2. Status Chart (Doughnut Chart)
        var statusCanvas = document.getElementById("sr-status-chart");
        if (statusCanvas) {
          var statusLabels = [];
          var statusData = [];
          for (var s = 0; s < this.data.status_breakdown.length; s += 1) {
            statusLabels.push(this.data.status_breakdown[s].label);
            statusData.push(this.data.status_breakdown[s].count);
          }

          this.statusChart = new window.Chart(statusCanvas, {
            type: "doughnut",
            data: {
              labels: statusLabels,
              datasets: [
                {
                  data: statusData,
                  backgroundColor: ["#106EBE", "#F5A524", "#2E9E5B"],
                  borderWidth: 2,
                  borderColor: "#FFFFFF",
                },
              ],
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              plugins: {
                legend: {
                  position: "bottom",
                  labels: { boxWidth: 12, font: { size: 11 } },
                },
              },
              cutout: "68%",
            },
          });
        }
      },

      updateCharts: function () {
        if (this.categoryChart) {
          var catLabels = [];
          var catData = [];
          for (var i = 0; i < this.data.category_breakdown.length; i += 1) {
            catLabels.push(this.data.category_breakdown[i].label);
            catData.push(this.data.category_breakdown[i].count);
          }
          this.categoryChart.data.labels = catLabels;
          this.categoryChart.data.datasets[0].data = catData;
          this.categoryChart.update();
        }

        if (this.statusChart) {
          var statusLabels = [];
          var statusData = [];
          for (var s = 0; s < this.data.status_breakdown.length; s += 1) {
            statusLabels.push(this.data.status_breakdown[s].label);
            statusData.push(this.data.status_breakdown[s].count);
          }
          this.statusChart.data.labels = statusLabels;
          this.statusChart.data.datasets[0].data = statusData;
          this.statusChart.update();
        }
      },

      toggleExcludeSeed: function () {
        this.excludeSeed = !this.excludeSeed;
        this.fetchAnalytics();
      },

      fetchAnalytics: function () {
        var self = this;
        this.loading = true;

        fetch("/api/analytics?exclude_seed=" + (this.excludeSeed ? "true" : "false"), {
          headers: {
            Accept: "application/json",
          },
          credentials: "same-origin",
        })
          .then(function (res) {
            if (!res.ok) {
              throw new Error("HTTP error " + res.status);
            }
            return res.json();
          })
          .then(function (newData) {
            self.loading = false;
            self.data = newData;

            // Update Leaflet heatmap
            if (self.heatLayer && typeof self.heatLayer.setLatLngs === "function") {
              self.heatLayer.setLatLngs(newData.heatmap_points || []);
            }

            // Update Chart.js instances
            self.updateCharts();
          })
          .catch(function (err) {
            self.loading = false;
          });
      },

      formatFixDuration: function (hours) {
        if (hours === null || hours === undefined) {
          return "No resolved reports";
        }
        var h = Number(hours);
        if (h >= 24) {
          var days = (h / 24.0).toFixed(1);
          return days + " days";
        }
        return h.toFixed(1) + " hrs";
      },

      getSeverityColor: function (score) {
        var s = Number(score) || 0;
        if (s >= 70) {
          return "#D92D20";
        }
        if (s >= 40) {
          return "#F5A524";
        }
        return "#2E9E5B";
      },
    };
  };
})();
