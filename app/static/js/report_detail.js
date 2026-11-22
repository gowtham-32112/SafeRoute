/**
 * SafeRoute — Stage 7: Report Detail & Verification Controller (Issue #24 / B6)
 *
 * Implements reactive Alpine.js controller for hazard report details:
 * - Real-time voting (Confirm / Dispute / Mark Fixed) with live counter updates
 * - Optional after-photo validation and upload on Fixed votes
 * - Safe text rendering for comments (zero innerHTML on untrusted inputs)
 * - Self-voting prevention and fixed-state guards
 * - CSRF double-submit token verification
 */

(function () {
  "use strict";

  var MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024; // 5 MB
  var ALLOWED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"];

  function parseConfig() {
    var configEl = document.getElementById("sr-report-detail-config");
    if (!configEl) {
      return {};
    }
    try {
      return JSON.parse(configEl.textContent || "{}");
    } catch (e) {
      return {};
    }
  }

  window.srReportDetail = function () {
    var config = parseConfig();

    return {
      reportId: config.reportId || "",
      isOwnReport: Boolean(config.isOwnReport),
      status: config.status || "open",
      confirmCount: Number(config.confirmCount || 0),
      disputeCount: Number(config.disputeCount || 0),
      fixedCount: Number(config.fixedCount || 0),
      severityScore: Number(config.severityScore || 0),
      severityBand: (config.severityBand || "low").toLowerCase(),
      confidence: (config.confidence || "low").toLowerCase(),
      escalationRecommended: Boolean(config.escalationRecommended),
      fixedAt: config.fixedAt || null,
      escalatedAt: config.escalatedAt || null,
      csrfToken: config.csrfToken || "",

      // UI States
      voteSubmitting: false,
      voteSuccessMessage: "",
      voteErrorMessage: "",
      showFixedModal: false,

      // Fixed After-Photo
      afterPhotoFile: null,
      afterPhotoPreview: null,
      afterPhotoError: "",

      // Comments Thread
      comments: config.comments || [],
      newCommentText: "",
      newCommentHideName: false,
      commentSubmitting: false,
      commentSuccessMessage: "",
      commentErrorMessage: "",

      get isFixed() {
        return this.status === "fixed";
      },

      get isEscalated() {
        return this.status === "escalated";
      },

      get severityBandLabel() {
        if (this.severityBand === "high") return "High";
        if (this.severityBand === "medium") return "Medium";
        return "Low";
      },

      get severityColor() {
        if (this.severityBand === "high") return "#D92D20";
        if (this.severityBand === "medium") return "#F5A524";
        return "#2E9E5B";
      },

      get confidenceLabel() {
        if (this.confidence === "high") return "High confidence";
        if (this.confidence === "medium") return "Medium confidence";
        return "Low confidence";
      },

      // --- Voting Methods ---

      async castVote(voteType) {
        if (this.voteSubmitting) return;
        if (this.isOwnReport) {
          this.voteErrorMessage = "You reported this hazard. Verification is reserved for other community members.";
          return;
        }
        if (this.isFixed && voteType !== "confirm") {
          this.voteErrorMessage = "This hazard has already been marked as fixed by the community.";
          return;
        }

        // If clicking 'fixed', open modal to allow optional after-photo
        if (voteType === "fixed") {
          this.openFixedModal();
          return;
        }

        this.voteSubmitting = true;
        this.voteErrorMessage = "";
        this.voteSuccessMessage = "";

        try {
          var resp = await fetch("/reports/" + encodeURIComponent(this.reportId) + "/vote", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": this.csrfToken,
            },
            body: JSON.stringify({ vote: voteType }),
          });

          var data = await resp.json();

          if (!resp.ok) {
            this.voteErrorMessage = data.detail || "Unable to record your vote. Please try again.";
            return;
          }

          this.updateMetrics(data);
          this.voteSuccessMessage =
            voteType === "confirm"
              ? "Thank you! You confirmed this hazard is still present."
              : "Thank you! Your dispute has been recorded.";
        } catch (err) {
          this.voteErrorMessage = "Network error. Please check your connection and try again.";
        } finally {
          this.voteSubmitting = false;
        }
      },

      openFixedModal() {
        this.voteErrorMessage = "";
        this.voteSuccessMessage = "";
        this.afterPhotoError = "";
        this.showFixedModal = true;
      },

      closeFixedModal() {
        this.showFixedModal = false;
        this.clearAfterPhoto();
      },

      handleAfterPhotoSelect(event) {
        this.afterPhotoError = "";
        var file = event.target.files && event.target.files[0];
        if (!file) return;

        if (ALLOWED_IMAGE_TYPES.indexOf(file.type) === -1) {
          this.afterPhotoError = "Only JPEG, PNG, or WebP photographs are supported.";
          event.target.value = "";
          return;
        }

        if (file.size > MAX_IMAGE_SIZE_BYTES) {
          this.afterPhotoError = "Photograph exceeds the 5 MB limit. Please select a smaller photo.";
          event.target.value = "";
          return;
        }

        this.afterPhotoFile = file;
        var reader = new FileReader();
        var self = this;
        reader.onload = function (e) {
          self.afterPhotoPreview = e.target.result;
        };
        reader.readAsDataURL(file);
      },

      clearAfterPhoto() {
        this.afterPhotoFile = null;
        this.afterPhotoPreview = null;
        this.afterPhotoError = "";
        var input = document.getElementById("sr-after-photo-input");
        if (input) input.value = "";
      },

      async submitFixedVote() {
        if (this.voteSubmitting) return;
        this.voteSubmitting = true;
        this.voteErrorMessage = "";
        this.voteSuccessMessage = "";

        try {
          var resp;
          if (this.afterPhotoFile) {
            var formData = new FormData();
            formData.append("vote", "fixed");
            formData.append("after_photo", this.afterPhotoFile);

            resp = await fetch("/reports/" + encodeURIComponent(this.reportId) + "/vote", {
              method: "POST",
              headers: {
                "X-CSRF-Token": this.csrfToken,
              },
              body: formData,
            });
          } else {
            resp = await fetch("/reports/" + encodeURIComponent(this.reportId) + "/vote", {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": this.csrfToken,
              },
              body: JSON.stringify({ vote: "fixed" }),
            });
          }

          var data = await resp.json();

          if (!resp.ok) {
            this.voteErrorMessage = data.detail || "Unable to submit your fixed report.";
            this.closeFixedModal();
            return;
          }

          this.updateMetrics(data);
          this.closeFixedModal();
          this.voteSuccessMessage =
            data.status === "fixed"
              ? "Hazard marked as fixed! Two community members verified the repair."
              : "Thank you! Your vote that this hazard is fixed has been recorded (1 more vote needed).";
        } catch (err) {
          this.voteErrorMessage = "Network error. Please try again.";
          this.closeFixedModal();
        } finally {
          this.voteSubmitting = false;
        }
      },

      updateMetrics(data) {
        if (!data) return;
        if (data.confirm_count !== undefined) this.confirmCount = data.confirm_count;
        if (data.dispute_count !== undefined) this.disputeCount = data.dispute_count;
        if (data.fixed_count !== undefined) this.fixedCount = data.fixed_count;
        if (data.severity_score !== undefined) this.severityScore = data.severity_score;
        if (data.severity_band) this.severityBand = data.severity_band.toLowerCase();
        if (data.confidence) this.confidence = data.confidence.toLowerCase();
        if (data.status) this.status = data.status;
        if (data.fixed_at) this.fixedAt = data.fixed_at;
        if (data.escalation_recommended !== undefined) {
          this.escalationRecommended = data.escalation_recommended;
        }
      },

      // --- Comments Methods ---

      async submitComment() {
        if (this.commentSubmitting) return;
        var text = (this.newCommentText || "").trim();
        if (!text) {
          this.commentErrorMessage = "Comment cannot be empty.";
          return;
        }
        if (text.length > 1000) {
          this.commentErrorMessage = "Comment must not exceed 1000 characters.";
          return;
        }

        this.commentSubmitting = true;
        this.commentErrorMessage = "";
        this.commentSuccessMessage = "";

        try {
          var resp = await fetch("/reports/" + encodeURIComponent(this.reportId) + "/comments", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": this.csrfToken,
            },
            body: JSON.stringify({
              body: text,
              hide_name: Boolean(this.newCommentHideName),
            }),
          });

          var data = await resp.json();

          if (!resp.ok) {
            this.commentErrorMessage = data.detail || "Unable to post comment. Please try again.";
            return;
          }

          // Append new comment live to list
          this.comments.push(data);
          this.newCommentText = "";
          this.commentSuccessMessage = "Comment posted successfully.";
        } catch (err) {
          this.commentErrorMessage = "Network error. Unable to post comment.";
        } finally {
          this.commentSubmitting = false;
        }
      },

      formatDate(isoString) {
        if (!isoString) return "";
        try {
          var d = new Date(isoString);
          return d.toLocaleDateString(undefined, {
            year: "numeric",
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          });
        } catch (e) {
          return isoString;
        }
      },
    };
  };
})();
