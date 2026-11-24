/* Live issue activity and broadcast refresh for the authenticated issue page. */
(function () {
  "use strict";

  var notice = document.getElementById("sr-live-refresh-notice");
  var refreshButton = document.getElementById("sr-live-refresh-button");
  var activeBroadcast = document.getElementById("sr-broadcast-active");
  var emptyBroadcast = document.getElementById("sr-broadcast-empty");
  var broadcastCard = document.getElementById("sr-broadcast-card");

  function applySeverity(severity) {
    var cardClasses = {
      info: ["border-primary/25", "bg-primary-soft", "text-ink"],
      warning: ["border-amber-300", "bg-amber-50", "text-amber-950"],
      critical: ["border-red-300", "bg-red-50", "text-red-950"],
    };
    var labelClasses = {
      info: ["bg-primary", "text-white"],
      warning: ["bg-amber-700", "text-white"],
      critical: ["bg-red-700", "text-white"],
    };
    var label = document.getElementById("sr-broadcast-severity");
    var safeSeverity = Object.prototype.hasOwnProperty.call(cardClasses, severity)
      ? severity
      : "info";

    if (!broadcastCard || !label) return;
    broadcastCard.classList.remove(
      "border-primary/25", "bg-primary-soft", "text-ink",
      "border-amber-300", "bg-amber-50", "text-amber-950",
      "border-red-300", "bg-red-50", "text-red-950"
    );
    label.classList.remove(
      "bg-primary", "bg-amber-700", "bg-red-700", "text-white"
    );
    cardClasses[safeSeverity].forEach(function (className) {
      broadcastCard.classList.add(className);
    });
    labelClasses[safeSeverity].forEach(function (className) {
      label.classList.add(className);
    });
    label.textContent = safeSeverity;
    broadcastCard.dataset.severity = safeSeverity;
  }

  async function refreshBroadcast() {
    if (!activeBroadcast || !emptyBroadcast) return;
    try {
      var response = await window.fetch("/api/broadcast", {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!response.ok) return;

      var broadcast = await response.json();
      if (!broadcast || !broadcast.title) {
        activeBroadcast.hidden = true;
        emptyBroadcast.hidden = false;
        return;
      }

      activeBroadcast.hidden = false;
      emptyBroadcast.hidden = true;
      document.getElementById("sr-broadcast-title").textContent = broadcast.title;
      document.getElementById("sr-broadcast-message").textContent = broadcast.message;
      document.getElementById("sr-broadcast-author").textContent = broadcast.created_by || "Admin";
      document.getElementById("sr-broadcast-time").textContent = broadcast.created_at || "";
      applySeverity(broadcast.severity);
    } catch (_) {
      // Preserve the server-rendered broadcast if refresh fails.
    }
  }

  if (refreshButton && notice) {
    refreshButton.addEventListener("click", function () {
      window.location.reload();
    });
  }

  if (activeBroadcast) {
    refreshBroadcast();
    window.setInterval(refreshBroadcast, 30000);
  }

  if (notice && typeof window.EventSource === "function") {
    var stream = new window.EventSource("/api/reports/stream");
    stream.addEventListener("hazard_change", function () {
      notice.hidden = false;
    });
    window.addEventListener("pagehide", function () {
      stream.close();
    }, { once: true });
  }
})();
