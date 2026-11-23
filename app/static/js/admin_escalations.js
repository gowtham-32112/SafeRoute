/**
 * SafeRoute Admin Escalation & Municipal Management Controller
 *
 * Implements:
 * - Alpine component window.srAdminEscalations()
 * - Review queue of candidates flagged for escalation
 * - Inspection of all reports across statuses (Open, Escalated, Fixed)
 * - Authoritative "Resolve / Close Issue" modal with resolution note
 * - Commuter role management (promote commuter to admin / demote)
 * - Broadcast system notice publisher
 * - Zero innerHTML, safe DOM text bindings, and CSRF token transmission via X-CSRF-Token
 */

(function () {
  "use strict";

  window.srAdminEscalations = function () {
    return {
      currentTab: "escalations", // "escalations", "all_reports", "commuters", "broadcast"
      candidates: [],
      recentEscalations: [],
      allReports: [],
      commuters: [],
      reportStatusFilter: "all",
      pendingCount: 0,
      highSeverityCount: 0,
      totalEscalatedCount: 0,
      csrfToken: "",

      // Escalation Modal state
      showModal: false,
      activeReport: null,
      authorityName: "",
      note: "",
      submitting: false,
      errorMessage: "",

      // Resolution Modal state
      showResolveModal: false,
      activeResolveReport: null,
      resolutionNote: "",
      resolveSubmitting: false,
      resolveErrorMessage: "",

      // Broadcast form state
      broadcastTitle: "",
      broadcastMessage: "",
      broadcastSeverity: "info",
      broadcastSubmitting: false,
      activeBroadcast: null,

      successToast: "",
      toastTimer: null,

      init: function () {
        const dataEl = document.getElementById("sr-admin-dashboard-data");
        let initialData = null;
        if (dataEl && dataEl.textContent) {
          try {
            initialData = JSON.parse(dataEl.textContent);
          } catch (_) {
            initialData = null;
          }
        }

        if (initialData) {
          this.candidates = Array.isArray(initialData.candidates)
            ? [...initialData.candidates]
            : [];
          this.recentEscalations = Array.isArray(initialData.recent_escalations)
            ? [...initialData.recent_escalations]
            : [];
          this.pendingCount = Number(initialData.pending_count) || this.candidates.length;
          this.highSeverityCount = Number(initialData.high_severity_count) || 0;
          this.totalEscalatedCount = Number(initialData.total_escalated_count) || this.recentEscalations.length;
        }

        const reportsEl = document.getElementById("sr-admin-all-reports-data");
        if (reportsEl && reportsEl.textContent) {
          try {
            this.allReports = JSON.parse(reportsEl.textContent) || [];
          } catch (_) {
            this.allReports = [];
          }
        }

        const commutersEl = document.getElementById("sr-admin-commuters-data");
        if (commutersEl && commutersEl.textContent) {
          try {
            this.commuters = JSON.parse(commutersEl.textContent) || [];
          } catch (_) {
            this.commuters = [];
          }
        }

        const tokenEl = document.getElementById("sr-admin-csrf-token");
        if (tokenEl && tokenEl.value) {
          this.csrfToken = tokenEl.value;
        }

        this.fetchActiveBroadcast();
      },

      filteredReports: function () {
        if (!this.allReports || this.allReports.length === 0) return [];
        if (this.reportStatusFilter === "all") return this.allReports;
        return this.allReports.filter((r) => r.status === this.reportStatusFilter);
      },

      openModal: function (report) {
        if (!report) return;
        this.activeReport = report;
        this.authorityName = "";
        this.note = "";
        this.errorMessage = "";
        this.showModal = true;

        this.$nextTick(() => {
          const input = document.getElementById("sr-authority-input");
          if (input) {
            input.focus();
          }
        });
      },

      closeModal: function () {
        this.showModal = false;
        this.activeReport = null;
        this.authorityName = "";
        this.note = "";
        this.errorMessage = "";
        this.submitting = false;
      },

      submitEscalation: async function () {
        if (!this.activeReport) return;

        const trimmedAuth = (this.authorityName || "").trim();
        const trimmedNote = (this.note || "").trim();

        if (!trimmedAuth) {
          this.errorMessage = "Authority name is required (e.g., GHMC Road Maintenance Circle).";
          return;
        }
        if (trimmedAuth.length > 150) {
          this.errorMessage = "Authority name must be at most 150 characters.";
          return;
        }
        if (!trimmedNote) {
          this.errorMessage = "A justification note is required for the municipal audit trail.";
          return;
        }
        if (trimmedNote.length > 1000) {
          this.errorMessage = "Justification note must be at most 1000 characters.";
          return;
        }

        this.submitting = true;
        this.errorMessage = "";

        const reportId = this.activeReport.id;
        try {
          const res = await fetch("/reports/" + encodeURIComponent(reportId) + "/escalate", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Accept: "application/json",
              "X-CSRF-Token": this.csrfToken,
            },
            body: JSON.stringify({
              authority_name: trimmedAuth,
              note: trimmedNote,
            }),
          });

          if (!res.ok) {
            let errorDetail = "Failed to escalate hazard report.";
            try {
              const errJson = await res.json();
              if (errJson && errJson.detail) {
                errorDetail = errJson.detail;
              }
            } catch (_) {}
            this.errorMessage = errorDetail;
            this.submitting = false;
            return;
          }

          const data = await res.json();

          const escalatedItem = {
            id: data.escalation_id,
            report_id: reportId,
            category: this.activeReport.category,
            description: this.activeReport.description,
            address_text: this.activeReport.address_text,
            severity_score: this.activeReport.severity_score,
            severity_band: this.activeReport.severity_band,
            authority_name: data.authority_name || trimmedAuth,
            note: data.note || trimmedNote,
            escalated_by: data.escalated_by,
            escalated_at: data.escalated_at || new Date().toISOString(),
          };

          this.recentEscalations.unshift(escalatedItem);
          this.totalEscalatedCount += 1;

          const wasHigh = this.activeReport.severity_score >= 70;
          this.candidates = this.candidates.filter((c) => c.id !== reportId);
          this.pendingCount = Math.max(0, this.pendingCount - 1);
          if (wasHigh) {
            this.highSeverityCount = Math.max(0, this.highSeverityCount - 1);
          }

          // Update status in allReports
          const existingInAll = this.allReports.find((r) => r.id === reportId);
          if (existingInAll) {
            existingInAll.status = "escalated";
          }

          this.showToast("Hazard escalated successfully to " + trimmedAuth + ".");
          this.closeModal();
        } catch (err) {
          this.errorMessage = "Network error: Unable to reach the server. Please verify your connection.";
        } finally {
          this.submitting = false;
        }
      },

      openResolveModal: function (report) {
        if (!report) return;
        this.activeResolveReport = report;
        this.resolutionNote = "";
        this.resolveErrorMessage = "";
        this.showResolveModal = true;
      },

      closeResolveModal: function () {
        this.showResolveModal = false;
        this.activeResolveReport = null;
        this.resolutionNote = "";
        this.resolveErrorMessage = "";
        this.resolveSubmitting = false;
      },

      submitResolution: async function () {
        if (!this.activeResolveReport) return;
        const note = (this.resolutionNote || "").trim();
        if (!note) {
          this.resolveErrorMessage = "Please provide an administrative resolution note.";
          return;
        }

        this.resolveSubmitting = true;
        this.resolveErrorMessage = "";
        const reportId = this.activeResolveReport.id;

        try {
          const res = await fetch("/admin/reports/" + encodeURIComponent(reportId) + "/resolve", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Accept: "application/json",
              "X-CSRF-Token": this.csrfToken,
            },
            body: JSON.stringify({ resolution_note: note }),
          });

          if (!res.ok) {
            let errorDetail = "Failed to resolve hazard report.";
            try {
              const errJson = await res.json();
              if (errJson && errJson.detail) errorDetail = errJson.detail;
            } catch (_) {}
            this.resolveErrorMessage = errorDetail;
            this.resolveSubmitting = false;
            return;
          }

          // Update local models
          const targetInAll = this.allReports.find((r) => r.id === reportId);
          if (targetInAll) {
            targetInAll.status = "fixed";
            targetInAll.fixed_at = new Date().toISOString();
          }

          this.candidates = this.candidates.filter((c) => c.id !== reportId);
          this.pendingCount = Math.max(0, this.pendingCount - 1);

          this.showToast("Hazard marked as resolved & closed by Municipal Administration!");
          this.closeResolveModal();
        } catch (err) {
          this.resolveErrorMessage = "Network error: Unable to reach the server.";
        } finally {
          this.resolveSubmitting = false;
        }
      },

      updateUserRole: async function (userId, newRole) {
        try {
          const res = await fetch("/admin/users/" + encodeURIComponent(userId) + "/role", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Accept: "application/json",
              "X-CSRF-Token": this.csrfToken,
            },
            body: JSON.stringify({ role: newRole }),
          });

          if (!res.ok) {
            let msg = "Could not update role.";
            try {
              const err = await res.json();
              if (err && err.detail) msg = err.detail;
            } catch (_) {}
            this.showToast("Error: " + msg);
            return;
          }

          const updated = await res.json();
          const target = this.commuters.find((u) => u.id === userId);
          if (target) {
            target.role = updated.role;
          }
          this.showToast(
            updated.role === "admin"
              ? "User successfully promoted to Municipal Admin!"
              : "User role updated to Commuter."
          );
        } catch (err) {
          this.showToast("Network error while updating user role.");
        }
      },

      fetchActiveBroadcast: async function () {
        try {
          const res = await fetch("/api/broadcast", { headers: { Accept: "application/json" } });
          if (res.ok) {
            this.activeBroadcast = await res.json();
          }
        } catch (_) {}
      },

      submitBroadcast: async function () {
        const title = (this.broadcastTitle || "").trim();
        const msg = (this.broadcastMessage || "").trim();
        if (!title || !msg) {
          this.showToast("Broadcast requires both a title and a message.");
          return;
        }

        this.broadcastSubmitting = true;
        try {
          const res = await fetch("/admin/broadcast", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Accept: "application/json",
              "X-CSRF-Token": this.csrfToken,
            },
            body: JSON.stringify({
              title: title,
              message: msg,
              severity: this.broadcastSeverity,
            }),
          });

          if (!res.ok) {
            this.showToast("Failed to publish broadcast.");
            return;
          }

          this.activeBroadcast = await res.json();
          this.broadcastTitle = "";
          this.broadcastMessage = "";
          this.showToast("Broadcast announcement sent to all commuters and users!");
        } catch (err) {
          this.showToast("Network error while sending broadcast.");
        } finally {
          this.broadcastSubmitting = false;
        }
      },

      dismissBroadcast: async function () {
        try {
          const res = await fetch("/admin/broadcast", {
            method: "DELETE",
            headers: {
              Accept: "application/json",
              "X-CSRF-Token": this.csrfToken,
            },
          });
          if (res.ok) {
            this.activeBroadcast = null;
            this.showToast("Active broadcast announcement cleared.");
          }
        } catch (err) {
          this.showToast("Network error while clearing broadcast.");
        }
      },

      showToast: function (message) {
        this.successToast = message;
        if (this.toastTimer) {
          clearTimeout(this.toastTimer);
        }
        const self = this;
        this.toastTimer = setTimeout(() => {
          self.successToast = "";
        }, 5000);
      },

      formatDate: function (isoStr) {
        if (!isoStr) return "Recently";
        try {
          const d = new Date(isoStr);
          if (isNaN(d.getTime())) return isoStr;
          return d.toLocaleDateString("en-US", {
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          });
        } catch (_) {
          return isoStr;
        }
      },

      categoryLabel: function (cat) {
        if (!cat) return "Hazard";
        return cat
          .replace(/_/g, " ")
          .replace(/\b\w/g, (c) => c.toUpperCase());
      },
    };
  };
})();
