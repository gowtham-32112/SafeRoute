/**
 * SafeRoute Admin Escalation Dashboard Controller (Issue #26)
 *
 * Implements:
 * - Alpine component window.srAdminEscalations()
 * - Review queue of candidates flagged for escalation (confirm_count >= 5 OR severity_score >= 70)
 * - Accessible escalation modal with live authority name & note validation
 * - Live optimistic / real-time updates to KPI strip and tables without full-page reloads
 * - Zero innerHTML, safe DOM text bindings, and CSRF token transmission via X-CSRF-Token
 */

(function () {
  "use strict";

  window.srAdminEscalations = function () {
    return {
      candidates: [],
      recentEscalations: [],
      pendingCount: 0,
      highSeverityCount: 0,
      totalEscalatedCount: 0,
      csrfToken: "",

      // Modal state
      showModal: false,
      activeReport: null,
      authorityName: "",
      note: "",
      submitting: false,
      errorMessage: "",
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

        const tokenEl = document.getElementById("sr-admin-csrf-token");
        if (tokenEl && tokenEl.value) {
          this.csrfToken = tokenEl.value;
        }
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
          this.errorMessage = "Escalation note is required for audit trail.";
          return;
        }
        if (trimmedNote.length > 1000) {
          this.errorMessage = "Escalation note must be at most 1000 characters.";
          return;
        }

        this.submitting = true;
        this.errorMessage = "";

        const reportId = this.activeReport.id;
        const targetUrl = "/reports/" + encodeURIComponent(reportId) + "/escalate";

        try {
          const response = await fetch(targetUrl, {
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

          let data = {};
          try {
            data = await response.json();
          } catch (_) {
            data = {};
          }

          if (!response.ok) {
            const detail = data.detail || "Failed to record escalation. Please check inputs and retry.";
            this.errorMessage = detail;
            return;
          }

          // Live state update without page reload
          const escalatedItem = {
            id: data.escalation_id || "esc-" + Date.now(),
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

          this.showToast("Hazard escalated successfully to " + trimmedAuth + ".");
          this.closeModal();
        } catch (err) {
          this.errorMessage = "Network error: Unable to reach the server. Please verify your connection.";
        } finally {
          this.submitting = false;
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
