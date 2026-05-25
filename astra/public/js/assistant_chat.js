(function () {
  if (!window.frappe || window.__astra_loaded) {
    return;
  }

  window.__astra_loaded = true;

  const STORAGE_KEY = "astra.active_session";
  const state = {
    open: false,
    loading: false,
    sessionId: getStoredSessionId(),
    sessions: [],
    messages: [],
    activeRequestId: null,
  };

  function boot() {
    if (!document.body || document.querySelector(".local-ai-assistant")) {
      return;
    }

    const root = document.createElement("div");
    root.className = "local-ai-assistant";
    root.innerHTML = `
      <button class="local-ai-toggle" type="button" aria-label="Open AI assistant">
        <span class="local-ai-toggle-icon" aria-hidden="true">
          <svg viewBox="0 0 48 48" focusable="false">
            <path class="local-ai-mark-orbit" d="M8 26c6.5-12.5 25.5-12.5 32 0" />
            <path class="local-ai-mark-a" d="M15 36 24 12l9 24m-14.5-9h11" />
            <path class="local-ai-mark-star" d="M36 9l1.6 4.4L42 15l-4.4 1.6L36 21l-1.6-4.4L30 15l4.4-1.6L36 9Z" />
          </svg>
        </span>
      </button>
      <section class="local-ai-panel" aria-label="Astra">
        <header class="local-ai-header">
          <div>
            <div class="local-ai-title">Astra</div>
            <div class="local-ai-subtitle">ERPNext assistant</div>
          </div>
          <div class="local-ai-header-actions">
            <button class="local-ai-admin" type="button" aria-label="Astra admin">Admin</button>
            <button class="local-ai-new" type="button" aria-label="New chat">+</button>
            <button class="local-ai-close" type="button" aria-label="Close">x</button>
          </div>
        </header>
        <div class="local-ai-session-bar">
          <select class="local-ai-session-select" aria-label="Chat sessions"></select>
          <button class="local-ai-rename" type="button" aria-label="Rename chat">Rename</button>
          <button class="local-ai-close-session" type="button" aria-label="Close chat">Close</button>
        </div>
        <div class="local-ai-status" role="status"></div>
        <div class="local-ai-messages" role="log" aria-live="polite"></div>
        <form class="local-ai-form">
          <button class="local-ai-attach" type="button" aria-label="Attach file">Attach</button>
          <textarea class="local-ai-input" rows="1" placeholder="Ask about ERPNext..." maxlength="2000"></textarea>
          <button class="local-ai-send" type="submit" aria-label="Send">
            <span class="local-ai-send-text">Send</span>
            <span class="local-ai-spinner" aria-hidden="true"></span>
          </button>
        </form>
      </section>
    `;

    document.body.appendChild(root);

    const toggle = root.querySelector(".local-ai-toggle");
    const close = root.querySelector(".local-ai-close");
    const newChat = root.querySelector(".local-ai-new");
    const adminButton = root.querySelector(".local-ai-admin");
    const sessionSelect = root.querySelector(".local-ai-session-select");
    const statusBar = root.querySelector(".local-ai-status");
    const renameChat = root.querySelector(".local-ai-rename");
    const closeSession = root.querySelector(".local-ai-close-session");
    const form = root.querySelector(".local-ai-form");
    const input = root.querySelector(".local-ai-input");
    const attachButton = root.querySelector(".local-ai-attach");
    let tipTimer = null;

    toggle.addEventListener("click", () => setOpen(true));
    close.addEventListener("click", () => setOpen(false));
    newChat.addEventListener("click", resetChat);
    adminButton.addEventListener("click", openAdminDialog);
    sessionSelect.addEventListener("change", () => loadSelectedSession(sessionSelect.value));
    renameChat.addEventListener("click", renameCurrentSession);
    closeSession.addEventListener("click", closeCurrentSession);
    attachButton.addEventListener("click", attachFile);
    form.addEventListener("submit", onSubmit);
    input.addEventListener("input", resizeInput);
    root.addEventListener("click", onRootClick);
    loadSessionList();
    loadChatHistory();
    loadAssistantStatus();
    startContextTips();

    function setOpen(open) {
      state.open = open;
      root.classList.toggle("is-open", open);
      toggle.setAttribute("aria-label", open ? "Close AI assistant" : "Open AI assistant");
      if (open) {
        setTimeout(() => input.focus(), 80);
      }
    }

    function resizeInput() {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 132) + "px";
    }

    async function onSubmit(event) {
      event.preventDefault();
      const text = input.value.trim();
      if (!text || state.loading) {
        return;
      }

      addMessage("user", text);
      input.value = "";
      resizeInput();
      setLoading(true);

      try {
        const history = state.messages
          .slice(0, -1)
          .slice(-12)
          .map((message) => ({ role: message.role, content: message.content }));

        const result = await frappe.call({
          method: "astra.api.start_chat_response_stream",
          args: {
            user_message: text,
            history: history,
            session_id: state.sessionId,
            current_context: getCurrentDeskContext(),
          },
          freeze: false,
        });

        const payload = result && result.message ? result.message : {};
        if (payload.streaming) {
          if (payload.session_id) {
            state.sessionId = payload.session_id;
            storeSessionId(payload.session_id);
          }
          beginStream(payload.request_id);
          return;
        }

        const fallback = payload.response || payload;
        if (fallback.session_id) {
          state.sessionId = fallback.session_id;
          storeSessionId(fallback.session_id);
        }
        addMessage("assistant", fallback.message || "I could not produce a response.", false, {
          tools: fallback.tools || [],
          confirmations: fallback.confirmations || [],
          sources: fallback.source_cards || fallback.sources || [],
          visualization: fallback.visualization,
          guidance: fallback.guidance || [],
          plan: fallback.plan,
        });
        loadSessionList();
      } catch (error) {
        addMessage("assistant", friendlyError(error), true);
      } finally {
        if (!state.activeRequestId) {
          setLoading(false);
        }
      }
    }

    function beginStream(requestId) {
      state.activeRequestId = requestId;
      addMessage("assistant", "", false, { streaming: true });
      setStatus("Astra is responding...");
      const eventName = "astra_stream_" + requestId;

      frappe.realtime.on(eventName, (event) => {
        if (state.activeRequestId !== requestId) {
          return;
        }

        if (event.type === "token") {
          appendToLastAssistant(event.token || "");
          return;
        }

        if (event.type === "status") {
          setStatus(event.message || "");
          return;
        }

        if (event.type === "tool") {
          mergeLastAssistantMeta({
            tools: event.tool ? [event.tool] : [],
            sources: event.source_cards || event.sources || [],
            visualization: event.visualization,
          });
          setStatus("");
          return;
        }

        if (event.type === "meta") {
          mergeLastAssistantMeta({
            confirmations: event.confirmations || [],
            tools: event.tools || [],
            sources: event.source_cards || event.sources || [],
            visualization: event.visualization,
          });
          return;
        }

        if (event.type === "done") {
          state.activeRequestId = null;
          if (event.session_id) {
            state.sessionId = event.session_id;
            storeSessionId(event.session_id);
          }
          updateLastAssistantMeta({
            sources: event.source_cards || event.sources || [],
            guidance: event.guidance || [],
            plan: event.plan,
            tools: event.tools || [],
            confirmations: event.confirmations || [],
            visualization: event.visualization,
          });
          setLoading(false);
          setStatus("");
          loadSessionList();
          return;
        }

        if (event.type === "error") {
          state.activeRequestId = null;
          updateLastAssistant(event.message || "Astra could not complete the streamed response.", true);
          setLoading(false);
          setStatus("");
        }
      });
    }

    function appendToLastAssistant(text) {
      const last = state.messages[state.messages.length - 1];
      if (!last || last.role !== "assistant") {
        addMessage("assistant", text);
        return;
      }
      last.content = (last.content || "") + text;
      renderMessages();
      const messages = root.querySelector(".local-ai-messages");
      messages.scrollTop = messages.scrollHeight;
    }

    function updateLastAssistant(text, isError) {
      const last = state.messages[state.messages.length - 1];
      if (!last || last.role !== "assistant") {
        addMessage("assistant", text, isError);
        return;
      }
      last.content = text;
      renderMessages();
      if (isError) {
        root.querySelector(".local-ai-message:last-child").classList.add("is-error");
      }
    }

    function updateLastAssistantMeta(meta) {
      const last = state.messages[state.messages.length - 1];
      if (!last || last.role !== "assistant") {
        return;
      }
      last.meta = Object.assign({}, last.meta || {}, meta || {});
      renderMessages();
      const messages = root.querySelector(".local-ai-messages");
      messages.scrollTop = messages.scrollHeight;
    }

    function mergeLastAssistantMeta(meta) {
      const last = state.messages[state.messages.length - 1];
      if (!last || last.role !== "assistant") {
        return;
      }
      const current = last.meta || {};
      last.meta = Object.assign({}, current, meta || {});
      if (Array.isArray(current.tools) || Array.isArray(meta.tools)) {
        last.meta.tools = (current.tools || []).concat(meta.tools || []);
      }
      if (Array.isArray(current.confirmations) || Array.isArray(meta.confirmations)) {
        last.meta.confirmations = (current.confirmations || []).concat(meta.confirmations || []);
      }
      if (Array.isArray(current.sources) || Array.isArray(meta.sources)) {
        last.meta.sources = dedupeSources((current.sources || []).concat(meta.sources || []));
      }
      renderMessages();
      const messages = root.querySelector(".local-ai-messages");
      messages.scrollTop = messages.scrollHeight;
    }

    async function loadAssistantStatus() {
      try {
        const result = await frappe.call({
          method: "astra.api.get_assistant_status",
          freeze: false,
        });
        const payload = result && result.message ? result.message : {};
        const warnings = [];
        if (payload.ollama && !payload.ollama.ok) {
          warnings.push(payload.ollama.message);
        } else if (payload.ollama && payload.ollama.model_found === false) {
          warnings.push(payload.ollama.message);
        }
        if (payload.ollama && payload.ollama.model_health && payload.ollama.model_health.advice) {
          warnings.push(payload.ollama.model_health.advice[0]);
        }
        if (payload.fac && payload.fac.enabled && payload.fac.message) {
          warnings.push(payload.fac.message);
        }
        if (payload.rag && payload.rag.enabled && payload.rag.message) {
          warnings.push(payload.rag.message);
        }
        setStatus(warnings.filter(Boolean).slice(0, 2).join(" "));
      } catch (_error) {
        setStatus("");
      }
    }

    function openAdminDialog() {
      if (!frappe.ui || !frappe.ui.Dialog) {
        return;
      }

      const dialog = new frappe.ui.Dialog({
        title: "Astra Admin",
        fields: [
          { fieldname: "url", fieldtype: "Data", label: "Documentation URL" },
          { fieldname: "category", fieldtype: "Data", label: "Category" },
          { fieldname: "source_group", fieldtype: "Data", label: "Source Group" },
          { fieldname: "doc_version", fieldtype: "Data", label: "Documentation Version" },
          { fieldname: "markdown", fieldtype: "Long Text", label: "Markdown / Text" },
        ],
        primary_action_label: "Ingest",
        primary_action: async (values) => {
          if (values.url) {
            await frappe.call({
              method: "astra.rag.ingest_knowledge_base_url",
              args: {
                url: values.url,
                category: values.category,
                source_group: values.source_group,
                doc_version: values.doc_version,
              },
              freeze: true,
            });
          } else if (values.markdown) {
            await frappe.call({
              method: "astra.rag.ingest_knowledge_base_text",
              args: {
                title: values.category || "Astra Knowledge",
                category: values.category,
                source_group: values.source_group,
                doc_version: values.doc_version,
                content: values.markdown,
              },
              freeze: true,
            });
          }
          dialog.hide();
          loadAssistantStatus();
        },
      });

      dialog.add_custom_action("Rebuild Embeddings", async () => {
        await frappe.call({
          method: "astra.rag.rebuild_knowledge_base_embeddings",
          freeze: true,
        });
        loadAssistantStatus();
      });
      dialog.add_custom_action("Seed Packs", async () => {
        await frappe.call({
          method: "astra.api.seed_workflow_packs",
          freeze: true,
        });
        loadAssistantStatus();
      });
      dialog.add_custom_action("Observability", async () => {
        const result = await frappe.call({
          method: "astra.api.get_observability_summary",
          freeze: false,
        });
        frappe.msgprint("<pre>" + escapeHtml(JSON.stringify(result.message || {}, null, 2)) + "</pre>");
      });
      dialog.add_custom_action("Action History", async () => {
        const result = await frappe.call({
          method: "astra.api.list_action_history",
          args: { session_id: state.sessionId, limit: 10 },
          freeze: false,
        });
        const payload = result && result.message ? result.message : {};
        addMessage("assistant", "Recent Astra actions.", false, {
          actions: payload.actions || [],
        });
      });
      dialog.add_custom_action("Bench Diagnostics", async () => {
        const result = await frappe.call({
          method: "astra.api.get_bench_diagnostics",
          freeze: false,
        });
        frappe.msgprint("<pre>" + escapeHtml(JSON.stringify(result.message || {}, null, 2)) + "</pre>");
      });
      dialog.add_custom_action("Install Doctor", async () => {
        const result = await frappe.call({
          method: "astra.install_doctor.run",
          freeze: true,
        });
        frappe.msgprint("<pre>" + escapeHtml(JSON.stringify(result.message || {}, null, 2)) + "</pre>");
      });
      dialog.add_custom_action("Permission Matrix", async () => {
        const result = await frappe.call({
          method: "astra.api.run_permission_matrix",
          freeze: false,
        });
        frappe.msgprint("<pre>" + escapeHtml(JSON.stringify(result.message || {}, null, 2)) + "</pre>");
      });
      dialog.add_custom_action("Transcript", async () => {
        if (!state.sessionId) {
          frappe.msgprint("Open a chat session first.");
          return;
        }
        const result = await frappe.call({
          method: "astra.api.get_tool_transcript",
          args: { session_id: state.sessionId },
          freeze: false,
        });
        frappe.msgprint("<pre>" + escapeHtml(JSON.stringify(result.message || {}, null, 2)) + "</pre>");
      });
      dialog.add_custom_action("Clear Schema Cache", async () => {
        await frappe.call({
          method: "astra.api.clear_schema_cache",
          freeze: true,
        });
        setStatus("Schema cache cleared.");
        setTimeout(() => setStatus(""), 1800);
      });
      dialog.add_custom_action("Sync FAC Contracts", async () => {
        const result = await frappe.call({
          method: "astra.api.sync_fac_tool_contracts",
          freeze: true,
        });
        frappe.msgprint("<pre>" + escapeHtml(JSON.stringify(result.message || {}, null, 2)) + "</pre>");
      });
      dialog.add_custom_action("Run Evaluations", async () => {
        const result = await frappe.call({
          method: "astra.api.run_evaluation_cases",
          args: { limit: 20, run_model: 0 },
          freeze: true,
        });
        frappe.msgprint("<pre>" + escapeHtml(JSON.stringify(result.message || {}, null, 2)) + "</pre>");
      });
      dialog.add_custom_action("Document Wizard", () => openDocumentWizard());
      dialog.add_custom_action("Report Wizard", () => openReportWizard());
      dialog.show();
    }

    function setStatus(message) {
      statusBar.textContent = message || "";
      root.classList.toggle("has-status", Boolean(message));
    }

    function friendlyError(error) {
      const message = String((error && error.message) || "");
      loadContextTips(message);
      const lower = message.toLowerCase();
      if (lower.includes("permission") || lower.includes("not permitted")) {
        return "You do not have permission for that Astra action or ERPNext data.";
      }
      if (lower.includes("ollama") || lower.includes("connection") || lower.includes("timed out")) {
        return message || "Ollama is not reachable. Start Ollama and check Ollama Settings.";
      }
      if (lower.includes("model")) {
        return "The selected Ollama model may be missing. Pull it locally or update Ollama Settings.";
      }
      return message || "Astra could not complete the request.";
    }

    function startContextTips() {
      loadContextTips();
      tipTimer = window.setInterval(() => loadContextTips(), 45000);
      window.addEventListener("beforeunload", () => {
        if (tipTimer) window.clearInterval(tipTimer);
      });
    }

    async function loadContextTips(lastError) {
      try {
        const result = await frappe.call({
          method: "astra.api.get_contextual_tips",
          args: {
            current_context: getCurrentDeskContext(),
            last_error: lastError || "",
          },
          freeze: false,
        });
        const payload = result && result.message ? result.message : {};
        const tip = payload.tips && payload.tips[0];
        if (tip && state.open) {
          setStatus(`${tip.title}: ${tip.message}`);
        }
      } catch (_error) {
        // Tips are opportunistic and should never interrupt Desk work.
      }
    }

    function setLoading(loading) {
      state.loading = loading;
      root.classList.toggle("is-loading", loading);
      input.disabled = loading;
      root.querySelector(".local-ai-attach").disabled = loading;
      root.querySelector(".local-ai-send").disabled = loading;
    }

    function attachFile() {
      if (!frappe.ui || !frappe.ui.FileUploader || state.loading) {
        return;
      }
      new frappe.ui.FileUploader({
        allow_multiple: false,
        on_success: async (file) => {
          setLoading(true);
          try {
            const registered = await frappe.call({
              method: "astra.api.register_attachment",
              args: {
                file_url: file.file_url,
                session_id: state.sessionId,
              },
              freeze: false,
            });
            const attachment = registered && registered.message ? registered.message : {};
            const analyzed = await frappe.call({
              method: "astra.api.analyze_attachment",
              args: {
                attachment_id: attachment.name,
                target_doctype: getCurrentDeskContext().doctype,
              },
              freeze: false,
            });
            const payload = analyzed && analyzed.message ? analyzed.message : {};
            addMessage("assistant", payload.summary || "Attachment analyzed.", false, {
              visualization: buildAttachmentVisualization(payload),
            });
          } catch (error) {
            addMessage("assistant", friendlyError(error), true);
          } finally {
            setLoading(false);
          }
        },
      });
    }

    async function loadChatHistory() {
      if (!state.sessionId) {
        return;
      }

      try {
        const result = await frappe.call({
          method: "astra.api.get_chat_history",
          args: {
            session_id: state.sessionId,
          },
          freeze: false,
        });
        const payload = result && result.message ? result.message : {};
        if (!payload.session_id) {
          resetChat();
          return;
        }

        state.sessionId = payload.session_id;
        storeSessionId(payload.session_id);
        state.messages = (payload.messages || []).map((message) => ({
          role: message.role,
          content: message.content,
          meta: {
            tools: message.tools || [],
            sources: message.sources || [],
            confirmations: message.confirmations || [],
            actions: message.actions || [],
          },
        }));
        renderMessages();
        const messages = root.querySelector(".local-ai-messages");
        messages.scrollTop = messages.scrollHeight;
      } catch (_error) {
        resetChat();
      }
    }

    async function loadSessionList() {
      try {
        const result = await frappe.call({
          method: "astra.api.list_chat_sessions",
          args: { limit: 30 },
          freeze: false,
        });
        const payload = result && result.message ? result.message : {};
        state.sessions = payload.sessions || [];
        renderSessionList();
      } catch (_error) {
        state.sessions = [];
        renderSessionList();
      }
    }

    function renderSessionList() {
      const options = ['<option value="">New chat</option>']
        .concat(
          state.sessions.map((session) => {
            const selected = session.name === state.sessionId ? " selected" : "";
            return `<option value="${escapeHtml(session.name)}"${selected}>${escapeHtml(
              session.title || session.name
            )}</option>`;
          })
        )
        .join("");
      sessionSelect.innerHTML = options;
    }

    async function loadSelectedSession(sessionId) {
      if (!sessionId) {
        resetChat();
        return;
      }

      state.sessionId = sessionId;
      storeSessionId(sessionId);
      await loadChatHistory();
      renderSessionList();
    }

    async function renameCurrentSession() {
      if (!state.sessionId || !window.frappe || !frappe.prompt) {
        return;
      }

      frappe.prompt(
        [{ fieldname: "title", fieldtype: "Data", label: "Title", reqd: 1 }],
        async (values) => {
          await frappe.call({
            method: "astra.api.rename_chat_session",
            args: {
              session_id: state.sessionId,
              title: values.title,
            },
            freeze: false,
          });
          await loadSessionList();
        },
        "Rename Chat",
        "Rename"
      );
    }

    async function closeCurrentSession() {
      if (!state.sessionId) {
        return;
      }

      await frappe.call({
        method: "astra.api.close_chat_session",
        args: { session_id: state.sessionId },
        freeze: false,
      });
      resetChat();
      await loadSessionList();
    }

    function resetChat() {
      state.sessionId = null;
      state.messages = [];
      clearStoredSessionId();
      renderMessages();
      renderSessionList();
      input.focus();
    }

    async function onRootClick(event) {
      const actionButton = event.target.closest("[data-confirmation-action]");
      const feedbackButton = event.target.closest("[data-feedback-rating]");
      const guideButton = event.target.closest("[data-guide-target]");
      const sourceButton = event.target.closest("[data-source-route]");
      const undoButton = event.target.closest("[data-undo-action]");
      const docCreateButton = event.target.closest("[data-doc-create]");
      const reportRunButton = event.target.closest("[data-report-run]");
      if (docCreateButton) {
        await submitDocumentWizard(docCreateButton);
        return;
      }
      if (reportRunButton) {
        await submitReportWizard(reportRunButton);
        return;
      }
      if (undoButton) {
        await prepareUndo(undoButton.getAttribute("data-undo-action"));
        return;
      }
      if (sourceButton) {
        openSourceRoute(
          sourceButton.getAttribute("data-source-route"),
          sourceButton.getAttribute("data-source-filters")
        );
        return;
      }
      if (guideButton) {
        highlightDeskTarget(guideButton.getAttribute("data-guide-target"));
        return;
      }
      if (feedbackButton) {
        await submitFeedback(feedbackButton.getAttribute("data-feedback-rating"));
        return;
      }
      if (!actionButton || state.loading) {
        return;
      }

      const confirmationId = actionButton.getAttribute("data-confirmation-id");
      const action = actionButton.getAttribute("data-confirmation-action");
      if (!confirmationId || !action) {
        return;
      }

      setLoading(true);
      try {
        const result = await frappe.call({
          method: "astra.api.resolve_tool_confirmation",
          args: {
            confirmation_id: confirmationId,
            action: action,
          },
          freeze: false,
        });
        const payload = result && result.message ? result.message : {};
        addMessage("assistant", payload.message || "Action updated.", false, {
          tools: payload.tools || [],
        });
        await loadChatHistory();
      } catch (error) {
        const serverMessage =
          error && error.message ? error.message : "Unable to resolve the action.";
        addMessage("assistant", serverMessage, true);
      } finally {
        setLoading(false);
      }
    }

    async function submitFeedback(rating) {
      if (!rating) return;
      await frappe.call({
        method: "astra.api.submit_message_feedback",
        args: {
          session_id: state.sessionId,
          rating: rating,
        },
        freeze: false,
      });
      setStatus("Feedback saved.");
      setTimeout(() => setStatus(""), 1800);
    }

    async function prepareUndo(actionId) {
      if (!actionId || state.loading) return;
      setLoading(true);
      try {
        const result = await frappe.call({
          method: "astra.api.prepare_undo_action",
          args: { action_history_id: actionId },
          freeze: false,
        });
        const payload = result && result.message ? result.message : {};
        addMessage("assistant", payload.message || "Undo prepared.", false, {
          confirmations: payload.confirmation_id
            ? [{ confirmation_id: payload.confirmation_id, name: "undo action" }]
            : [],
        });
      } catch (error) {
        addMessage("assistant", friendlyError(error), true);
      } finally {
        setLoading(false);
      }
    }

    function addMessage(role, content, isError, meta) {
      state.messages.push({ role, content, meta: meta || {} });
      renderMessages();
      const messages = root.querySelector(".local-ai-messages");
      messages.scrollTop = messages.scrollHeight;
      if (isError) {
        root.querySelector(".local-ai-message:last-child").classList.add("is-error");
      }
    }

    function renderMessages() {
      const messages = root.querySelector(".local-ai-messages");
      messages.innerHTML = state.messages
        .map((message) => {
          const roleLabel = message.role === "user" ? "You" : "AI";
          return `
            <article class="local-ai-message local-ai-message-${message.role}">
              <div class="local-ai-message-label">${roleLabel}</div>
              <div class="local-ai-message-body">${renderMarkdown(message.content)}</div>
              ${renderPlan(message.meta && message.meta.plan)}
              ${renderSources(message.meta && message.meta.sources)}
              ${renderVisualization(message.meta && message.meta.visualization)}
              ${renderGuidance(message.meta && message.meta.guidance)}
              ${renderToolTrace(message.meta && message.meta.tools)}
              ${renderConfirmations(message.meta && message.meta.confirmations)}
              ${renderActions(message.meta && message.meta.actions)}
              ${renderDocWizard(message.meta && message.meta.doc_wizard)}
              ${renderReportWizard(message.meta && message.meta.report_wizard)}
              ${message.role === "assistant" ? renderFeedback() : ""}
            </article>
          `;
        })
        .join("");
    }

    function openDocumentWizard() {
      frappe.prompt(
        [{ fieldname: "doctype", fieldtype: "Link", options: "DocType", label: "DocType", reqd: 1 }],
        async (values) => {
          const result = await frappe.call({
            method: "astra.api.prepare_document_creation",
            args: { doctype: values.doctype, values: {} },
            freeze: false,
          });
          addMessage("assistant", "Fill the required fields, then Astra will prepare an approval preview.", false, {
            doc_wizard: result.message || {},
          });
        },
        "Document Wizard",
        "Start"
      );
    }

    function openReportWizard() {
      frappe.prompt(
        [{ fieldname: "report_name", fieldtype: "Data", label: "Report", reqd: 1 }],
        async (values) => {
          const result = await frappe.call({
            method: "astra.api.prepare_report_wizard",
            args: { report_name: values.report_name },
            freeze: false,
          });
          addMessage("assistant", "Add report filters and run the report.", false, {
            report_wizard: result.message || {},
          });
        },
        "Report Wizard",
        "Start"
      );
    }

    async function submitDocumentWizard(button) {
      if (state.loading) return;
      const wrapper = button.closest(".local-ai-doc-wizard");
      if (!wrapper) return;
      const doctype = wrapper.getAttribute("data-doc-doctype");
      const values = collectWizardValues(wrapper, "[data-doc-field]", "docField");
      setLoading(true);
      try {
        const result = await frappe.call({
          method: "astra.api.create_document_confirmation",
          args: { doctype: doctype, values: values, session_id: state.sessionId },
          freeze: false,
        });
        const payload = result && result.message ? result.message : {};
        if (!payload.ready) {
          addMessage("assistant", "More required fields are needed before Astra can prepare this document.", false, {
            doc_wizard: payload.wizard || {},
          });
          return;
        }
        addMessage("assistant", "Document creation is ready for approval.", false, {
          confirmations: [
            {
              confirmation_id: payload.confirmation_id,
              name: "create " + doctype,
              preview: payload.preview,
            },
          ],
        });
      } catch (error) {
        addMessage("assistant", friendlyError(error), true);
      } finally {
        setLoading(false);
      }
    }

    async function submitReportWizard(button) {
      if (state.loading) return;
      const wrapper = button.closest(".local-ai-report-wizard");
      if (!wrapper) return;
      const reportName = wrapper.getAttribute("data-report-name");
      const filters = collectWizardValues(wrapper, "[data-report-filter]", "reportFilter");
      setLoading(true);
      try {
        const result = await frappe.call({
          method: "astra.api.run_report_wizard",
          args: { report_name: reportName, filters: filters },
          freeze: false,
        });
        const payload = result && result.message ? result.message : {};
        addMessage("assistant", "Report result preview.", false, {
          visualization: payload.visualization,
          sources: payload.source_cards || [],
        });
      } catch (error) {
        addMessage("assistant", friendlyError(error), true);
      } finally {
        setLoading(false);
      }
    }

    function collectWizardValues(wrapper, selector, dataKey) {
      const values = {};
      wrapper.querySelectorAll(selector).forEach((input) => {
        const fieldname = input.dataset[dataKey];
        if (!fieldname) return;
        const value = input.value;
        if (value !== "") {
          values[fieldname] = value;
        }
      });
      wrapper.querySelectorAll("[data-child-table]").forEach((textarea) => {
        const fieldname = textarea.getAttribute("data-child-table");
        if (!fieldname || !textarea.value.trim()) return;
        try {
          values[fieldname] = JSON.parse(textarea.value);
        } catch (_error) {
          values[fieldname] = [];
        }
      });
      return values;
    }
  }

  function renderToolTrace(tools) {
    if (!Array.isArray(tools) || !tools.length) {
      return "";
    }

    const items = tools
      .map((tool) => {
        const name = escapeHtml(tool.name || "tool");
        const status = tool.status === "error" ? "error" : "success";
        const repaired = tool.repaired ? '<span class="local-ai-tool-repaired">fixed</span>' : "";
        return `
          <span class="local-ai-tool-chip is-${status}">
            <span class="local-ai-tool-dot"></span>
            ${name}
            ${repaired}
          </span>
        `;
      })
      .join("");

    return `<div class="local-ai-tool-trace">${items}</div>`;
  }

  function renderPlan(plan) {
    if (!plan || !Array.isArray(plan.steps) || !plan.steps.length) return "";
    const steps = plan.steps
      .map((step) => {
        const confirmation = step.requires_confirmation ? " approval" : "";
        return `<li>${escapeHtml(step.label || "Step")}${confirmation}</li>`;
      })
      .join("");
    return `
      <div class="local-ai-plan">
        <div class="local-ai-plan-title">${escapeHtml(plan.title || "Astra plan")}</div>
        <ol>${steps}</ol>
      </div>
    `;
  }

  function renderConfirmations(confirmations) {
    if (!Array.isArray(confirmations) || !confirmations.length) {
      return "";
    }

    const items = confirmations
      .map((confirmation) => {
        const id = escapeHtml(confirmation.confirmation_id || "");
        const name = escapeHtml(confirmation.name || "action");
        if (!id) {
          return "";
        }
        return `
          <div class="local-ai-confirmation">
            <div class="local-ai-confirmation-title">Approve ${name}</div>
            ${renderConfirmationPreview(confirmation.preview)}
            <div class="local-ai-confirmation-actions">
              <button type="button" data-confirmation-id="${id}" data-confirmation-action="approve">Approve</button>
              <button type="button" data-confirmation-id="${id}" data-confirmation-action="reject">Reject</button>
            </div>
          </div>
        `;
      })
      .join("");

    return `<div class="local-ai-confirmations">${items}</div>`;
  }

  function renderConfirmationPreview(preview) {
    if (!preview || !Array.isArray(preview.changes)) return "";
    const header = [preview.doctype, preview.document_name].filter(Boolean).join(" ");
    const rows = preview.changes.slice(0, 6).map((change) => {
      return `<li>${escapeHtml(change.fieldname)}: ${formatPreviewValue(change.before)} -> ${formatPreviewValue(
        change.after
      )}</li>`;
    });
    return `
      <div class="local-ai-confirmation-preview">
        ${header ? `<div>${escapeHtml(header)}</div>` : ""}
        ${rows.length ? `<ul>${rows.join("")}</ul>` : ""}
        <div>Reverse guidance will be based on the before values shown here.</div>
      </div>
    `;
  }

  function formatPreviewValue(value) {
    if (value === null || value === undefined || value === "") return "empty";
    return escapeHtml(value);
  }

  function renderSources(sources) {
    if (!Array.isArray(sources) || !sources.length) return "";
    return `<div class="local-ai-sources">${sources
      .map((source) => {
        const title = escapeHtml(source.title || "Source");
        const url = source.url ? escapeHtml(source.url) : "";
        const route = buildSourceRoute(source);
        if (url) {
          return `<a href="${url}" target="_blank" rel="noopener noreferrer">${title}</a>`;
        }
        if (route) {
          const filters = source.filters ? escapeHtml(JSON.stringify(source.filters)) : "";
          return `<button type="button" data-source-route="${escapeHtml(route)}" data-source-filters="${filters}">${title}</button>`;
        }
        return `<span>${title}</span>`;
      })
      .join("")}</div>`;
  }

  function dedupeSources(sources) {
    const seen = {};
    return sources.filter((source) => {
      const key = [source.type, source.title, source.url, source.doctype, source.name, source.report].join("|");
      if (seen[key]) return false;
      seen[key] = true;
      return true;
    });
  }

  function buildSourceRoute(source) {
    if (source.doctype && source.name) return "Form/" + source.doctype + "/" + source.name;
    if (source.report) return "query-report/" + source.report;
    if (source.doctype) return "List/" + source.doctype;
    return "";
  }

  function openSourceRoute(route, filtersJson) {
    if (route && frappe.set_route) {
      if (filtersJson) {
        try {
          frappe.route_options = JSON.parse(filtersJson);
        } catch (_error) {
          frappe.route_options = null;
        }
      }
      frappe.set_route.apply(frappe, route.split("/"));
    }
  }

  function renderDocWizard(wizard) {
    if (!wizard || !wizard.doctype) return "";
    const missing = Array.isArray(wizard.missing_required_fields) ? wizard.missing_required_fields : [];
    const preview = Array.isArray(wizard.field_preview) ? wizard.field_preview : [];
    const childTables = Array.isArray(wizard.child_tables) ? wizard.child_tables : [];
    const linkIssues = Array.isArray(wizard.link_issues) ? wizard.link_issues : [];
    const draft = wizard.draft_preview || {};
    const fields = missing.length ? missing : preview;
    const rows = fields
      .slice(0, 12)
      .map((field) => {
        const fieldname = escapeHtml(field.fieldname || "");
        const label = escapeHtml(field.label || field.fieldname || "Field");
        const type = escapeHtml(field.fieldtype || "Data");
        const value = field.value === undefined || field.value === null ? "" : escapeHtml(field.value);
        return `
          <label class="local-ai-wizard-field">
            <span>${label}</span>
            <input type="text" data-doc-field="${fieldname}" placeholder="${type}" value="${value}">
          </label>
        `;
      })
      .join("");
    const children = childTables
      .slice(0, 4)
      .map((table) => {
        const sample = {};
        (table.fields || []).forEach((field) => {
          sample[field.fieldname] = "";
        });
        return `
          <label class="local-ai-wizard-field">
            <span>${escapeHtml(table.label || table.fieldname)} rows JSON</span>
            <textarea data-child-table="${escapeHtml(table.fieldname)}" rows="3" placeholder='${escapeHtml(
              JSON.stringify([sample])
            )}'></textarea>
          </label>
        `;
      })
      .join("");
    const issues = linkIssues
      .map((issue) => {
        const matches = (issue.matches || []).map((match) => match.name).join(", ");
        return `<div class="local-ai-wizard-note">${escapeHtml(issue.label)}: ${escapeHtml(issue.message)}${
          matches ? " Matches: " + escapeHtml(matches) : ""
        }</div>`;
      })
      .join("");
    const draftPreview = draft.doctype
      ? `<div class="local-ai-draft-preview">
          <div>Missing required: ${escapeHtml(draft.missing_required_count || 0)}</div>
          <div>Link issues: ${escapeHtml(draft.link_issue_count || 0)}</div>
          <div>Submit eligible: ${draft.submit_eligible ? "Yes" : "No"}</div>
        </div>`
      : "";
    return `
      <div class="local-ai-wizard local-ai-doc-wizard" data-doc-doctype="${escapeHtml(wizard.doctype)}">
        <div class="local-ai-wizard-title">Create ${escapeHtml(wizard.doctype)}</div>
        ${rows || '<div class="local-ai-wizard-note">No required fields detected.</div>'}
        ${children}
        ${issues}
        ${draftPreview}
        <button type="button" data-doc-create="1">Prepare Approval Preview</button>
      </div>
    `;
  }

  function renderReportWizard(wizard) {
    if (!wizard || !wizard.report_name) return "";
    const filters = Array.isArray(wizard.filters) ? wizard.filters : [];
    const rows = filters
      .slice(0, 12)
      .map((filter) => {
        const fieldname = escapeHtml(filter.fieldname || filter.name || filter.label || "");
        const label = escapeHtml(filter.label || filter.fieldname || filter.name || "Filter");
        const type = escapeHtml(filter.fieldtype || filter.type || "Data");
        const value = filter.default === undefined || filter.default === null ? "" : escapeHtml(filter.default);
        return `
          <label class="local-ai-wizard-field">
            <span>${label}</span>
            <input type="text" data-report-filter="${fieldname}" placeholder="${type}" value="${value}">
          </label>
        `;
      })
      .join("");
    return `
      <div class="local-ai-wizard local-ai-report-wizard" data-report-name="${escapeHtml(wizard.report_name)}">
        <div class="local-ai-wizard-title">${escapeHtml(wizard.report_name)}</div>
        ${rows || '<div class="local-ai-wizard-note">No required filters detected.</div>'}
        <button type="button" data-report-run="1">Run Report</button>
      </div>
    `;
  }

  function renderActions(actions) {
    if (!Array.isArray(actions) || !actions.length) return "";
    return `<div class="local-ai-actions">${actions
      .map((action) => {
        const title = escapeHtml(
          [action.tool_name, action.doctype_name, action.document_name].filter(Boolean).join(" ")
        );
        const undo = action.name ? `<button type="button" data-undo-action="${escapeHtml(action.name)}">Prepare Undo</button>` : "";
        return `<div class="local-ai-action-row"><span>${title || "Astra action"}</span>${undo}</div>`;
      })
      .join("")}</div>`;
  }

  function renderVisualization(visualization) {
    if (!visualization) return "";
    if (Array.isArray(visualization.columns) && Array.isArray(visualization.rows) && visualization.rows.length) {
      const head = visualization.columns
        .map((column) => `<th>${escapeHtml(column)}</th>`)
        .join("");
      const body = visualization.rows
        .map((row, index) => {
          const attrs =
            visualization.row_sources && visualization.row_sources[index]
              ? buildRowRouteAttrs(visualization.row_sources[index])
              : "";
          return `<tr${attrs}>${row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`;
        })
        .join("");
      return `
        <div class="local-ai-visualization">
          <div class="local-ai-visualization-title">${escapeHtml(visualization.title || "Result preview")}</div>
          <div class="local-ai-table-wrap">
            <table>
              <thead><tr>${head}</tr></thead>
              <tbody>${body}</tbody>
            </table>
          </div>
        </div>
      `;
    }
    return `<div class="local-ai-visualization">${escapeHtml(visualization.title || "Report result")}</div>`;
  }

  function buildRowRouteAttrs(source) {
    if (!source || !source.doctype || !source.name) return "";
    return ` data-source-route="${escapeHtml("Form/" + source.doctype + "/" + source.name)}"`;
  }

  function buildAttachmentVisualization(payload) {
    if (!payload || !payload.preview || !Array.isArray(payload.preview.rows)) {
      return null;
    }
    const columns = (payload.preview.columns || []).slice(0, 8);
    const rows = payload.preview.rows.slice(0, 8).map((row) => columns.map((column) => row[column]));
    return {
      type: "table",
      title: payload.type === "csv" ? "CSV preview" : "Attachment preview",
      columns: columns.slice(0, 8),
      rows: rows,
    };
  }

  function renderGuidance(guidance) {
    if (!Array.isArray(guidance) || !guidance.length) return "";
    return `<div class="local-ai-guidance">${guidance
      .map((step) => {
        const label = escapeHtml(step.label || "Guide");
        const target = escapeHtml(step.target || "");
        return target
          ? `<button type="button" data-guide-target="${target}">${label}</button>`
          : `<span>${label}</span>`;
      })
      .join("")}</div>`;
  }

  function highlightDeskTarget(target) {
    if (!target) return;

    document.querySelectorAll(".astra-guide-highlight").forEach((element) => {
      element.classList.remove("astra-guide-highlight");
    });

    let element = null;
    if (target === "primary-action") {
      element = document.querySelector(".primary-action");
    } else if (target === "form-layout") {
      element = document.querySelector(".form-layout") || document.querySelector(".layout-main-section");
    } else if (window.cur_frm && cur_frm.get_field) {
      const field = cur_frm.get_field(target);
      element = field && field.$wrapper && field.$wrapper.get ? field.$wrapper.get(0) : null;
    }

    if (!element) return;
    element.classList.add("astra-guide-highlight");
    element.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => element.classList.remove("astra-guide-highlight"), 3200);
  }

  function renderFeedback() {
    return `
      <div class="local-ai-feedback">
        <button type="button" data-feedback-rating="Up">Good</button>
        <button type="button" data-feedback-rating="Down">Needs work</button>
      </div>
    `;
  }

  function renderMarkdown(markdown) {
    let html = escapeHtml(markdown || "");

    html = html.replace(/```([\s\S]*?)```/g, function (_match, code) {
      return "<pre><code>" + code.trim() + "</code></pre>";
    });
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/^### (.*)$/gm, "<h4>$1</h4>");
    html = html.replace(/^## (.*)$/gm, "<h3>$1</h3>");
    html = html.replace(/^# (.*)$/gm, "<h2>$1</h2>");
    html = html.replace(/^\s*[-*]\s+(.*)$/gm, "<li>$1</li>");
    html = html.replace(/(<li>.*<\/li>)/gs, "<ul>$1</ul>");
    html = html.replace(
      /(https?:\/\/[^\s<]+)/g,
      '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>'
    );
    html = html.replace(/\n{2,}/g, "</p><p>");
    html = html.replace(/\n/g, "<br>");

    return "<p>" + html + "</p>";
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function getStoredSessionId() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch (_error) {
      return null;
    }
  }

  function storeSessionId(sessionId) {
    try {
      window.localStorage.setItem(STORAGE_KEY, sessionId);
    } catch (_error) {
      // Browser storage can be disabled; Astra still works without persistence in the widget.
    }
  }

  function clearStoredSessionId() {
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch (_error) {
      // Browser storage can be disabled; Astra still works without persistence in the widget.
    }
  }

  function getCurrentDeskContext() {
    if (!window.cur_frm || !cur_frm.doctype) {
      return {};
    }
    return {
      doctype: cur_frm.doctype,
      docname: cur_frm.doc && cur_frm.doc.name,
      is_dirty: Boolean(cur_frm.is_dirty && cur_frm.is_dirty()),
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
