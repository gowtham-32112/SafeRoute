/**
 * SafeRoute Base UI Helpers — Toast Notification Manager & HTMX CSRF Bridge.
 * Zero build tooling; works directly with Tailwind CDN, HTMX, and Alpine.js.
 */
(function () {
  "use strict";

  var VALID_TYPES = {
    success: { label: "Success", duration: 5000, assertive: false },
    error: { label: "Error", duration: 8000, assertive: true },
    warning: { label: "Warning", duration: 7000, assertive: true },
    info: { label: "Notice", duration: 5000, assertive: false },
  };
  var MAX_VISIBLE_TOASTS = 4;

  function normalizeToastType(rawType) {
    var lowered = String(rawType || "info").trim().toLowerCase();
    if (lowered === "danger") {
      return "error";
    }
    return Object.prototype.hasOwnProperty.call(VALID_TYPES, lowered)
      ? lowered
      : "info";
  }

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

  // Attach double-submit CSRF header on non-GET HTMX requests (matches Stage 5 CSRF)
  document.addEventListener("htmx:configRequest", function (event) {
    var detail = event && event.detail;
    if (!detail || !detail.verb) {
      return;
    }
    var verb = String(detail.verb).toUpperCase();
    if (verb === "GET" || verb === "HEAD" || verb === "OPTIONS") {
      return;
    }
    var csrfToken = readCookie("sr_csrf_token");
    if (csrfToken && !detail.headers["X-CSRF-Token"]) {
      detail.headers["X-CSRF-Token"] = csrfToken;
    }
  });

  // Public helper for pushing toasts from any page script or inline handler
  function emitToast(options) {
    var payload =
      typeof options === "string"
        ? { type: "info", message: options }
        : options || {};
    window.dispatchEvent(new CustomEvent("saferoute:toast", { detail: payload }));
  }

  window.SafeRouteToast = {
    show: emitToast,
    success: function (message, title) {
      emitToast({ type: "success", message: message, title: title || "" });
    },
    error: function (message, title) {
      emitToast({ type: "error", message: message, title: title || "" });
    },
    warning: function (message, title) {
      emitToast({ type: "warning", message: message, title: title || "" });
    },
    info: function (message, title) {
      emitToast({ type: "info", message: message, title: title || "" });
    },
    clear: function () {
      window.dispatchEvent(new CustomEvent("saferoute:toast-clear"));
    },
  };

  function createToastManager() {
    return {
      toasts: [],
      nextId: 1,
      timers: {},

      init: function () {
        var self = this;
        var initialEl = document.getElementById("sr-initial-toasts");
        if (initialEl && initialEl.textContent) {
          try {
            var parsed = JSON.parse(initialEl.textContent);
            if (Array.isArray(parsed)) {
              parsed.forEach(function (item) {
                if (item && (item.message || item.text)) {
                  self.add({
                    type: item.type || item.category || item.level || "info",
                    title: item.title || "",
                    message: item.message || item.text || "",
                    duration: item.duration,
                  });
                }
              });
            }
          } catch (_err) {
            // Ignore malformed initial toast payloads safely
          }
        }
      },

      add: function (detail) {
        if (!detail) {
          return;
        }
        var message = String(detail.message || detail.text || "").trim();
        if (!message) {
          return;
        }
        var type = normalizeToastType(
          detail.type || detail.category || detail.level
        );
        var meta = VALID_TYPES[type];
        var duration =
          typeof detail.duration === "number" && detail.duration >= 0
            ? detail.duration
            : meta.duration;

        var id = this.nextId++;
        var toast = {
          id: id,
          type: type,
          label: detail.title ? String(detail.title) : meta.label,
          message: message,
          assertive: meta.assertive,
          duration: duration,
        };

        this.toasts.push(toast);
        if (this.toasts.length > MAX_VISIBLE_TOASTS) {
          var oldest = this.toasts.shift();
          if (oldest) {
            this.clearTimer(oldest.id);
          }
        }
        if (duration > 0) {
          this.startTimer(id, duration);
        }
      },

      startTimer: function (id, duration) {
        var self = this;
        this.clearTimer(id);
        this.timers[id] = window.setTimeout(function () {
          self.dismiss(id);
        }, duration);
      },

      clearTimer: function (id) {
        if (this.timers[id]) {
          window.clearTimeout(this.timers[id]);
          delete this.timers[id];
        }
      },

      pause: function (id) {
        this.clearTimer(id);
      },

      resume: function (toast) {
        if (toast && toast.duration > 0) {
          this.startTimer(toast.id, Math.min(toast.duration, 3500));
        }
      },

      dismiss: function (id) {
        this.clearTimer(id);
        this.toasts = this.toasts.filter(function (t) {
          return t.id !== id;
        });
      },

      dismissAll: function () {
        var self = this;
        Object.keys(this.timers).forEach(function (id) {
          self.clearTimer(id);
        });
        this.toasts = [];
      },

      politeToasts: function () {
        return this.toasts.filter(function (t) {
          return !t.assertive;
        });
      },

      assertiveToasts: function () {
        return this.toasts.filter(function (t) {
          return t.assertive;
        });
      },
    };
  }

  document.addEventListener("alpine:init", function () {
    if (window.Alpine && typeof window.Alpine.data === "function") {
      window.Alpine.data("srToastManager", createToastManager);
    }
  });
})();
