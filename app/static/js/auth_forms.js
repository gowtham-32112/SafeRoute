/**
 * SafeRoute Auth Page Alpine.js Form Controllers (Issue #11 — B2).
 *
 * Submits strictly to existing Stage 5 endpoints (POST /auth/login, POST /auth/signup)
 * using same-origin HttpOnly cookies and double-submit CSRF protection (X-CSRF-Token).
 * Never reads or persists authentication tokens in client-side browser storage.
 */
(function () {
  "use strict";

  var EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

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

  function extractErrorDetail(payload, fallback) {
    if (payload && typeof payload.detail === "string" && payload.detail.trim()) {
      return payload.detail.trim();
    }
    if (payload && typeof payload.message === "string" && payload.message.trim()) {
      return payload.message.trim();
    }
    return fallback;
  }

  function notifyToast(type, message, title) {
    if (window.SafeRouteToast && typeof window.SafeRouteToast[type] === "function") {
      window.SafeRouteToast[type](message, title || "");
    }
  }

  function createLoginForm() {
    return {
      email: "",
      password: "",
      showPassword: false,
      submitting: false,
      formError: "",
      errors: {
        email: "",
        password: "",
      },

      validate: function () {
        this.errors = { email: "", password: "" };
        this.formError = "";

        var trimmedEmail = String(this.email || "").trim().toLowerCase();
        if (!trimmedEmail) {
          this.errors.email = "Please enter your email address.";
        } else if (!EMAIL_RE.test(trimmedEmail)) {
          this.errors.email = "Please enter a valid email address.";
        }

        if (!this.password) {
          this.errors.password = "Please enter your password.";
        }

        return !this.errors.email && !this.errors.password;
      },

      submit: async function (event) {
        if (this.submitting) {
          return;
        }
        if (!this.validate()) {
          this.formError = "Please fix the highlighted fields and try again.";
          return;
        }

        var formEl = event && event.target ? event.target : null;
        var csrfToken = resolveCsrfToken(formEl);

        this.submitting = true;
        this.formError = "";

        try {
          var response = await window.fetch("/auth/login", {
            method: "POST",
            credentials: "same-origin",
            headers: {
              "Content-Type": "application/json",
              Accept: "application/json",
              "X-CSRF-Token": csrfToken,
            },
            body: JSON.stringify({
              email: String(this.email).trim().toLowerCase(),
              password: String(this.password),
            }),
          });

          var data = null;
          try {
            data = await response.json();
          } catch (_parseErr) {
            data = null;
          }

          if (!response.ok) {
            var errMsg = extractErrorDetail(
              data,
              "Unable to sign in. Please check your credentials and try again."
            );
            this.formError = errMsg;
            notifyToast("error", errMsg, "Sign in failed");
            return;
          }

          notifyToast("success", "Signed in successfully. Opening map...", "Welcome back");
          window.location.assign("/map");
        } catch (_netErr) {
          var netMsg = "Network error while signing in. Please check your connection and retry.";
          this.formError = netMsg;
          notifyToast("error", netMsg, "Connection error");
        } finally {
          this.submitting = false;
        }
      },
    };
  }

  function createSignupForm() {
    return {
      fullName: "",
      email: "",
      password: "",
      confirmPassword: "",
      showPassword: false,
      showConfirmPassword: false,
      submitting: false,
      formError: "",
      emailConfirmationSent: false,
      confirmationMessage: "",
      errors: {
        fullName: "",
        email: "",
        password: "",
        confirmPassword: "",
      },

      validate: function () {
        this.errors = {
          fullName: "",
          email: "",
          password: "",
          confirmPassword: "",
        };
        this.formError = "";

        var trimmedName = String(this.fullName || "").trim();
        if (trimmedName.length > 120) {
          this.errors.fullName = "Full name must be 120 characters or fewer.";
        }

        var trimmedEmail = String(this.email || "").trim().toLowerCase();
        if (!trimmedEmail) {
          this.errors.email = "Please enter your email address.";
        } else if (!EMAIL_RE.test(trimmedEmail)) {
          this.errors.email = "Please enter a valid email address.";
        }

        if (!this.password) {
          this.errors.password = "Please create a password.";
        } else if (this.password.length < 8) {
          this.errors.password = "Password must be at least 8 characters long.";
        } else if (this.password.length > 128) {
          this.errors.password = "Password must be 128 characters or fewer.";
        }

        if (!this.confirmPassword) {
          this.errors.confirmPassword = "Please confirm your password.";
        } else if (this.password && this.confirmPassword !== this.password) {
          this.errors.confirmPassword = "Passwords do not match.";
        }

        return (
          !this.errors.fullName &&
          !this.errors.email &&
          !this.errors.password &&
          !this.errors.confirmPassword
        );
      },

      submit: async function (event) {
        if (this.submitting) {
          return;
        }
        if (!this.validate()) {
          this.formError = "Please fix the highlighted fields before creating your account.";
          return;
        }

        var formEl = event && event.target ? event.target : null;
        var csrfToken = resolveCsrfToken(formEl);

        this.submitting = true;
        this.formError = "";

        try {
          // Send only the exact fields accepted by Stage 5 SignupPayload
          var response = await window.fetch("/auth/signup", {
            method: "POST",
            credentials: "same-origin",
            headers: {
              "Content-Type": "application/json",
              Accept: "application/json",
              "X-CSRF-Token": csrfToken,
            },
            body: JSON.stringify({
              full_name: String(this.fullName || "").trim(),
              email: String(this.email).trim().toLowerCase(),
              password: String(this.password),
            }),
          });

          var data = null;
          try {
            data = await response.json();
          } catch (_parseErr) {
            data = null;
          }

          if (!response.ok) {
            var errMsg = extractErrorDetail(
              data,
              "Unable to create your account right now. Please check your details and try again."
            );
            this.formError = errMsg;
            notifyToast("error", errMsg, "Sign up failed");
            return;
          }

          if (data && data.email_confirmation_required) {
            this.emailConfirmationSent = true;
            this.confirmationMessage =
              data.message ||
              "Account created. Please check your email to confirm your account.";
            this.password = "";
            this.confirmPassword = "";
            notifyToast("success", this.confirmationMessage, "Check your email");
            return;
          }

          notifyToast("success", "Account created! Opening the live hazard map...", "Welcome");
          window.location.assign("/map");
        } catch (_netErr) {
          var netMsg =
            "Network error while creating your account. Please check your connection and retry.";
          this.formError = netMsg;
          notifyToast("error", netMsg, "Connection error");
        } finally {
          this.submitting = false;
        }
      },
    };
  }

  function registerAuthForms() {
    if (window.Alpine && typeof window.Alpine.data === "function") {
      window.Alpine.data("srLoginForm", createLoginForm);
      window.Alpine.data("srSignupForm", createSignupForm);
    }
  }

  document.addEventListener("alpine:init", registerAuthForms);
  if (window.Alpine) {
    registerAuthForms();
  }
})();
