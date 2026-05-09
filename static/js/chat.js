/**
 * Alpine.js component for AI chat panel.
 * Registers as: Alpine.data('aiChat', (opts) => ...)
 *
 * opts = { scopeType: 'case'|'document', scopeId: string, suggestedPrompts: string[] }
 */
function registerAiChat() {
  Alpine.data('aiChat', ({ scopeType, scopeId, suggestedPrompts, extraParams }) => ({
    conversationId: null,
    messages: [],
    history: [],
    draft: '',
    streaming: false,
    streamBuffer: '',
    error: null,
    loading: true,
    suggestedPrompts: suggestedPrompts || [],
    currentProceedingId: extraParams?.currentProceedingId || null,
    limitToProceeding: false,

    async init() {
      await this.loadConversation();
      await this.loadHistory();
      this.$nextTick(() => this.scrollToBottom());
    },

    async loadConversation(id = null) {
      this.loading = true;
      this.error = null;
      try {
        const url = id ? `/api/chat/conversations/${id}` : '/api/chat/conversations';
        const method = id ? 'GET' : 'POST';
        const body = id ? null : JSON.stringify({ scope_type: scopeType, scope_id: String(scopeId) });

        const res = await fetch(url, {
          method,
          headers: { 'Content-Type': 'application/json' },
          body,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        this.conversationId = data.id;
        this.messages = data.messages.map(m => ({
          ...m,
          citations: m.context_document_ids
            ? m.context_document_ids.map(id => ({ doc_id: id, case_id: null, title: null }))
            : [],
        }));
      } catch (e) {
        this.error = `Failed to load conversation: ${e.message}`;
      } finally {
        this.loading = false;
      }
    },

    async loadHistory() {
      try {
        const res = await fetch(`/api/chat/conversations?scope_type=${scopeType}&scope_id=${scopeId}`);
        if (res.ok) {
          this.history = await res.json();
        }
      } catch (e) {
        console.warn('Failed to load chat history:', e);
      }
    },

    async newConversation() {
      this.loading = true;
      this.messages = [];
      this.conversationId = null;
      this.error = null;
      try {
        const res = await fetch('/api/chat/conversations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ scope_type: scopeType, scope_id: String(scopeId), force_new: true }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        this.conversationId = data.id;
        await this.loadHistory();
      } catch (e) {
        this.error = `Failed to create conversation: ${e.message}`;
      } finally {
        this.loading = false;
      }
    },

    submitPrompt(text) {
      this.draft = text;
      this.submit();
    },

    async submit() {
      if (!this.draft.trim() || this.streaming) return;
      if (!this.conversationId) {
        await this.loadConversation();
        if (!this.conversationId) return;
      }

      const userMsg = this.draft.trim();
      const isFirstMessage = this.messages.length === 0;
      this.draft = '';
      this.error = null;

      // Optimistic user bubble
      this.messages.push({
        _tmpId: Date.now(),
        role: 'user',
        content: userMsg,
        citations: [],
      });
      this.$nextTick(() => this.scrollToBottom());

      this.streaming = true;
      this.streamBuffer = '';

      try {
        const payload = { content: userMsg };
        if (this.limitToProceeding && this.currentProceedingId) {
          payload.proceeding_id = this.currentProceedingId;
        }

        const res = await fetch(
          `/api/chat/conversations/${this.conversationId}/messages`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          }
        );

        if (!res.ok) {
          const text = await res.text();
          throw new Error(`HTTP ${res.status}: ${text}`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let citations = [];

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n\n');
          buffer = lines.pop(); // keep incomplete chunk

          for (const block of lines) {
            if (!block.startsWith('data: ')) continue;
            let payload;
            try {
              payload = JSON.parse(block.slice(6));
            } catch {
              continue;
            }

            if (payload.type === 'token') {
              this.streamBuffer += payload.t;
              this.$nextTick(() => this.scrollToBottom());
            } else if (payload.type === 'citations') {
              citations = payload.docs || [];
            } else if (payload.type === 'done') {
              this.messages.push({
                _tmpId: Date.now(),
                role: 'assistant',
                content: this.streamBuffer,
                citations,
              });
              this.streamBuffer = '';
              this.$nextTick(() => this.scrollToBottom());

              // Update title if it was first message
              if (isFirstMessage) {
                const title = userMsg.length > 50 ? userMsg.substring(0, 47) + '...' : userMsg;
                await fetch(`/api/chat/conversations/${this.conversationId}/title`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ title }),
                });
                await this.loadHistory();
              }
            }
          }
        }
      } catch (e) {
        this.error = `Stream error: ${e.message}`;
        this.streamBuffer = '';
      } finally {
        this.streaming = false;
      }
    },

    async deleteConversation(id) {
      const res = await fetch(`/api/chat/conversations/${id}`, { method: 'DELETE' });
      if (!res.ok) return;
      this.history = this.history.filter(h => h.id !== id);
      if (this.conversationId === id) {
        const next = this.history[0];
        if (next) {
          try {
            await this.loadConversation(next.id);
          } catch {
            this.conversationId = null;
            this.messages = [];
            this.error = null;
          }
        } else {
          this.conversationId = null;
          this.messages = [];
          this.error = null;
        }
      }
    },

    scrollToBottom() {
      const el = this.$refs.messageList;
      if (el) el.scrollTop = el.scrollHeight;
    },

    renderContent(text) {
      if (!text) return '';

      const escHtml = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      const citationRe = /\[DOC:(\d+)(?:#p=(\d+))?\]/g;
      const processCitations = (s) => s.replace(citationRe, (_, docId, passageIdx) => {
        const label = passageIdx ? `[DOC:${docId}#p=${passageIdx}]` : `[DOC:${docId}]`;
        const hash  = passageIdx ? `#p=${passageIdx}` : '';
        return `<a href="/document/${docId}${hash}" class="inline-block px-1 py-0.5 text-[9px] font-mono rounded hover:underline" style="background:var(--color-primary-container);color:var(--color-primary);">${label}</a>`;
      });

      // Split on complete <think>…</think> blocks before HTML-escaping.
      const parts = [];
      const thinkRe = /<think>([\s\S]*?)<\/think>/g;
      let last = 0, m;
      while ((m = thinkRe.exec(text)) !== null) {
        if (m.index > last) parts.push({ type: 'text', content: text.slice(last, m.index) });
        parts.push({ type: 'think', content: m[1], streaming: false });
        last = thinkRe.lastIndex;
      }

      // Handle unclosed <think> at the tail (mid-stream).
      const tail = text.slice(last);
      const openAt = tail.indexOf('<think>');
      if (openAt !== -1) {
        if (openAt > 0) parts.push({ type: 'text', content: tail.slice(0, openAt) });
        parts.push({ type: 'think', content: tail.slice(openAt + 7), streaming: true });
      } else if (tail) {
        parts.push({ type: 'text', content: tail });
      }

      if (parts.length === 0) parts.push({ type: 'text', content: text });

      return parts.map(p => {
        if (p.type === 'think') {
          const inner = escHtml(p.content.trim());
          const label = p.streaming ? 'Thinking…' : 'Thinking';
          return `<details class="my-1" ${p.streaming ? 'open' : ''}>` +
            `<summary class="cursor-pointer select-none text-[9px] font-bold uppercase tracking-widest opacity-50 hover:opacity-80 transition-opacity" style="color:var(--color-on-surface-variant);">${label}</summary>` +
            `<div class="mt-1 pl-3 border-l-2 text-[11px] leading-relaxed whitespace-pre-wrap opacity-60" style="border-color:var(--color-outline-variant);color:var(--color-on-surface-variant);">${inner}</div>` +
            `</details>`;
        }
        return processCitations(escHtml(p.content));
      }).join('');
    },
  }));
}

if (window.Alpine) {
  registerAiChat();
} else {
  document.addEventListener('alpine:init', () => registerAiChat());
}
