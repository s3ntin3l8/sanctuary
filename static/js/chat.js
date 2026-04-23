/**
 * Alpine.js component for AI chat panel.
 * Registers as: Alpine.data('aiChat', (opts) => ...)
 *
 * opts = { scopeType: 'case'|'document', scopeId: string, suggestedPrompts: string[] }
 */
function registerAiChat() {
  Alpine.data('aiChat', ({ scopeType, scopeId, suggestedPrompts }) => ({
    conversationId: null,
    messages: [],
    draft: '',
    streaming: false,
    streamBuffer: '',
    error: null,
    loading: true,
    suggestedPrompts: suggestedPrompts || [],

    async init() {
      await this.loadConversation();
      this.$nextTick(() => this.scrollToBottom());
    },

    async loadConversation() {
      this.loading = true;
      this.error = null;
      try {
        const res = await fetch('/api/chat/conversations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ scope_type: scopeType, scope_id: String(scopeId) }),
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
        const res = await fetch(
          `/api/chat/conversations/${this.conversationId}/messages`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: userMsg }),
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

    scrollToBottom() {
      const el = this.$refs.messageList;
      if (el) el.scrollTop = el.scrollHeight;
    },

    renderContent(text) {
      if (!text) return '';
      // Highlight [DOC:n] citations inline
      return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\[DOC:(\d+)\]/g,
          '<span class="inline-block px-1 py-0.5 text-[9px] font-mono rounded" style="background:var(--color-primary-container);color:var(--color-primary);">[DOC:$1]</span>');
    },
  }));
}

if (typeof Alpine !== 'undefined') {
  document.addEventListener('alpine:init', () => registerAiChat());
} else {
  document.addEventListener('alpine:init', () => registerAiChat());
}
