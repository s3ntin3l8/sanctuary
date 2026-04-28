/**
 * HUD reader — scroll-spy, passage focus, and shortcut registry.
 * Loaded only on full-screen document page (standalone context).
 */

// ── HUD component registration (waits for Alpine) ─────────────────────────

function registerHudStore() {
  if (!Alpine.store('shortcuts')) {
    Alpine.store('shortcuts', { showHud: false });
  }
}

if (window.Alpine) {
  registerHudStore();
} else {
  document.addEventListener('alpine:init', () => registerHudStore());
}

// ── Direct keydown listener (runs immediately, checks for reader lazily) ─────────────────────────

window.addEventListener('keydown', (e) => {
  const tagName = e.target.tagName;
  if (tagName === 'INPUT' || tagName === 'TEXTAREA' || tagName === 'SELECT') return;

  const reader = window.__hudReader;
  if (!reader) return;

  if (e.key === 'ArrowLeft') {
    e.preventDefault();
    reader.navigatePrev();
  } else if (e.key === 'ArrowRight') {
    e.preventDefault();
    reader.navigateNext();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    reader.movePrevPassage();
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    reader.moveNextPassage();
  } else if (e.key === 'f') {
    e.preventDefault();
    reader.toggleFocusMode();
  } else if (e.key === 'Escape') {
    e.preventDefault();
    if (reader.focusModeActive) {
      reader.toggleFocusMode();
    } else {
      const caseId = reader.$el.dataset.caseId;
      if (caseId) window.location.href = '/cases/' + caseId;
    }
  } else if (e.key === '[') {
    e.preventDefault();
    reader.navigateParent();
  } else if (e.key === ']') {
    e.preventDefault();
    reader.navigateFirstChild();
  } else if (e.key === '{') {
    e.preventDefault();
    reader.navigateBundlePrev();
  } else if (e.key === '}') {
    e.preventDefault();
    reader.navigateBundleNext();
  } else if (e.key === 'o') {
    e.preventDefault();
    const docId = reader.$el.dataset.docId;
    if (docId) window.open('/document/' + docId + '/original', '_blank');
  } else if (e.key === 'n') {
    e.preventDefault();
    reader.createPinAtActive();
  } else if (e.key === 'r') {
    e.preventDefault();
    reader.focusReactionBar();
  } else if (['1', '2', '3', '4'].includes(e.key)) {
    e.preventDefault();
    reader.fireReaction(parseInt(e.key));
  } else if (e.key === '/') {
    e.preventDefault();
    reader.focusAskAi();
  } else if (e.key === '?') {
    e.preventDefault();
    reader.showShortcuts();
  } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    reader.confirmPrimary();
  }
});


// ── HUD reader Alpine component ────────────────────────────────────────────

function hudReader() {
  return {
    focusModeActive: false,
    activePassageId: null,
    observer: null,
    docChatOpen: false,

    init() {
      const root = this.$el;
      const context = root.dataset.hudContext;
      if (!context || context === 'overlay') return;

      window.__hudReader = this;

      // Deep-link scroll — if URL has #p=<id>, scroll to that mark on load.
      this._handleFragment();

      // IntersectionObserver scroll-spy (standalone + embedded only).
      this._initScrollSpy();

      // Delegated click on highlighted marks in body.
      this._initMarkClicks();

      // Position existing margin pins after layout settles.
      this.$nextTick(() => this._positionPins());

      // Re-position pins when article resizes (e.g. window resize).
      this._observeArticleResize();

      // Pin DELETE: capture the doomed pin's passage_id BEFORE the swap, so
      // afterSwap can decrement the spine counter once the element is gone.
      this.$el.addEventListener('htmx:beforeSwap', (e) => {
        const cfg = e.detail && e.detail.requestConfig;
        if (!cfg || cfg.verb !== 'delete') return;
        if (!/\/pin\/\d+$/.test(cfg.path || '')) return;
        const tgt = e.detail.target;
        if (tgt && tgt.dataset && tgt.dataset.passageId) {
          this._pendingDeletePassageId = tgt.dataset.passageId;
        }
      });

      // After HTMX swaps a pin card into the gutter (create) or removes one
      // (delete), update the affected UI: un-hide / re-hide the gutter,
      // toggle the article's left margin, bump or drop the spine counter,
      // and reposition every pin to clear of its passage mark.
      this.$el.addEventListener('htmx:afterSwap', (e) => {
        const gutterId = 'hud-pin-gutter-' + this.$el.dataset.docId;
        const gutter = document.getElementById(gutterId);
        if (!gutter) return;
        const target = e.target;
        const involvesGutter =
          target && (target.id === gutterId
                     || (target.closest && target.closest('#' + gutterId)));
        if (!involvesGutter) return;

        const cfg = e.detail && e.detail.requestConfig;
        const path = (cfg && cfg.path) || '';
        const isCreate = cfg && cfg.verb === 'post'
          && /\/document\/\d+\/pin$/.test(path);
        const isDelete = cfg && cfg.verb === 'delete'
          && /\/pin\/\d+$/.test(path);

        if (isCreate) {
          if (gutter.classList.contains('hidden')) {
            gutter.classList.remove('hidden');
            const article = this.$el.querySelector('article');
            if (article) article.classList.add('ml-20', 'xl:ml-52');
          }
          // beforeend swap → new pin card is the gutter's last child.
          const newPin = gutter.lastElementChild;
          const pid = newPin && newPin.dataset && newPin.dataset.passageId;
          if (pid) this._incrementSpinePinCount(pid);
        }
        if (isDelete) {
          const pid = this._pendingDeletePassageId;
          this._pendingDeletePassageId = null;
          if (pid) this._decrementSpinePinCount(pid);
          // Last pin removed → hide the gutter and reclaim the article width.
          if (gutter.children.length === 0) {
            gutter.classList.add('hidden');
            const article = this.$el.querySelector('article');
            if (article) article.classList.remove('ml-20', 'xl:ml-52');
          }
        }
        this._positionPins();
      });
    },

    _handleFragment() {
      const hash = window.location.hash;
      const match = hash.match(/^#p=(.+)$/);
      if (!match) return;
      const pid = match[1];
      const mark = document.getElementById(`p-${pid}`);
      if (mark) {
        setTimeout(() => {
          mark.scrollIntoView({ behavior: 'smooth', block: 'center' });
          this._flashMark(pid);
          this.activePassageId = pid;
        }, 200);
      }
    },

    _initMarkClicks() {
      this.$el.addEventListener('click', (e) => {
        const mark = e.target.closest('[data-passage-id]');
        if (mark) this.focusPassage(mark.dataset.passageId);
      });
    },

    _initScrollSpy() {
      const marks = document.querySelectorAll('[data-passage-id]');
      if (!marks.length) return;

      this.observer = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const pid = entry.target.dataset.passageId;
            this.activePassageId = pid;
            this._syncSpine(pid);
            break;
          }
        }
      }, { threshold: 0.5 });

      marks.forEach(mark => this.observer.observe(mark));
    },

    _syncSpine(pid) {
      document.querySelectorAll('[data-spine-passage]').forEach(row => {
        const isActive = row.dataset.spinePassage === pid;
        row.classList.toggle('bg-surface-container-high', isActive);
        row.classList.toggle('border-l-2', isActive);
        row.classList.toggle('border-primary', isActive);
        if (isActive) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      });
    },

    focusPassage(pid) {
      const mark = document.getElementById(`p-${pid}`);
      if (!mark) return;
      const unmatched = mark.classList.contains('passage-anchor-unmatched');
      // Unmatched anchors live at the top of the body; scroll to the article
      // top so the click isn't a silent dead-end. Spine row already shows ⚠.
      const target = unmatched ? (this.$el.querySelector('article') || mark) : mark;
      target.scrollIntoView({ behavior: 'smooth', block: unmatched ? 'start' : 'center' });
      if (!unmatched) this._flashMark(pid);
      this._flashSpineRow(pid, unmatched);
      history.pushState(null, '', `#p=${pid}`);
      this.activePassageId = pid;
      this._syncSpine(pid);
    },

    focusClaim(cid) {
      const el = document.getElementById(`claim-${cid}`);
      if (!el) return;
      const unmatched = el.classList.contains('claim-anchor-unmatched');
      // el is either a <mark> (independent claim highlight) or a <span> anchor
      // whose next sibling is the passage <mark>.
      const mark = (el.tagName === 'MARK')
        ? el
        : (el.nextElementSibling?.tagName === 'MARK' ? el.nextElementSibling : null);
      const target = (unmatched ? (this.$el.querySelector('article') || el)
                                : (mark || el));
      target.scrollIntoView({ behavior: 'smooth', block: unmatched ? 'start' : 'center' });
      if (mark && !unmatched) {
        mark.style.animation = 'none';
        mark.classList.add('hud-mark-flash');
        mark.addEventListener('animationend', () => mark.classList.remove('hud-mark-flash'), { once: true });
      }
    },

    _flashSpineRow(pid, unmatched) {
      const row = document.querySelector(`[data-spine-passage="${pid}"]`);
      if (!row) return;
      const cls = unmatched ? 'hud-spine-flash-warn' : 'hud-spine-flash';
      row.classList.remove(cls);
      // Force reflow so re-adding the class restarts the animation.
      void row.offsetWidth;
      row.classList.add(cls);
      row.addEventListener('animationend', () => row.classList.remove(cls), { once: true });
    },

    _flashMark(pid) {
      const mark = document.getElementById(`p-${pid}`);
      if (!mark) return;
      mark.style.animation = 'none';
      mark.classList.add('hud-mark-flash');
      mark.addEventListener('animationend', () => mark.classList.remove('hud-mark-flash'), { once: true });
    },

    _positionPins() {
      const docId = this.$el.dataset.docId;
      const gutter = document.getElementById('hud-pin-gutter-' + docId);
      if (!gutter) return;
      const article = this.$el.querySelector('article');
      if (!article) return;
      const articleRect = article.getBoundingClientRect();

      // Compute initial tops anchored to each passage mark. Pins anchored to
      // an unmatched passage (zero-height anchor at top of body) are pinned
      // to the top of the article and tagged so styling and the leader-line
      // pass can flag them as approximate.
      const cards = [];
      gutter.querySelectorAll('.hud-pin-card').forEach(card => {
        const pid = card.dataset.passageId;
        const mark = document.getElementById(`p-${pid}`);
        if (!mark) return;
        const unmatched = mark.classList.contains('passage-anchor-unmatched');
        card.classList.toggle('hud-pin-card--unmatched', unmatched);
        const top = unmatched
          ? 0
          : (mark.getBoundingClientRect().top - articleRect.top + article.scrollTop);
        cards.push({ card, top });
      });

      // Sort by top, then push down any cards that would overlap the previous one.
      cards.sort((a, b) => a.top - b.top);
      for (let i = 1; i < cards.length; i++) {
        const prev = cards[i - 1];
        const minTop = prev.top + prev.card.offsetHeight + 8;
        if (cards[i].top < minTop) cards[i].top = minTop;
      }

      cards.forEach(({ card, top }) => { card.style.top = top + 'px'; });

      // Draw leader lines after positions are applied (getBoundingClientRect reads new layout).
      this._drawLeaders();
    },

    _drawLeaders() {
      const docId = this.$el.dataset.docId;
      const svg = document.getElementById('hud-leader-lines-' + docId);
      if (!svg) return;
      svg.innerHTML = '';

      const gutter = document.getElementById('hud-pin-gutter-' + docId);
      if (!gutter) return;

      const container = svg.parentElement;
      if (!container) return;
      const containerRect = container.getBoundingClientRect();

      gutter.querySelectorAll('.hud-pin-card').forEach(card => {
        const pid = card.dataset.passageId;
        const mark = document.getElementById(`p-${pid}`);
        if (!mark) return;
        // Don't draw a line to a hidden 0-height anchor at the top of the
        // body — that's misleading. The pin card itself flags "unmatched"
        // styling, the absent line tells the user this is approximate.
        if (mark.classList.contains('passage-anchor-unmatched')) return;

        const cardRect = card.getBoundingClientRect();
        const markRect = mark.getBoundingClientRect();

        const x1 = cardRect.right - containerRect.left;
        const y1 = cardRect.top + cardRect.height / 2 - containerRect.top;
        const x2 = markRect.left - containerRect.left;
        const y2 = markRect.top + markRect.height / 2 - containerRect.top;

        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', x1);
        line.setAttribute('y1', y1);
        line.setAttribute('x2', x2);
        line.setAttribute('y2', y2);
        const leaderColor = getComputedStyle(document.documentElement).getPropertyValue('--color-leader-line').trim() || 'rgb(245 158 11 / 0.4)';
        line.setAttribute('stroke', leaderColor);
        line.setAttribute('stroke-width', '2');
        svg.appendChild(line);
      });
    },

    _observeArticleResize() {
      const article = this.$el.querySelector('article');
      if (!article || !window.ResizeObserver) return;
      const ro = new ResizeObserver(() => this._positionPins());
      ro.observe(article);
      this._pinResizeObserver = ro;
    },

    createPinAt(passageId) {
      // Used by the `n` keyboard shortcut. The spine button uses hx-post
      // directly on the always-rendered gutter; the htmx:afterSwap listener
      // un-hides the gutter, adds the article margin, and bumps the spine
      // counter — same flow whether the swap originated from a click or here.
      if (!passageId) return;
      const docId = this.$el.dataset.docId;
      if (!docId) return;
      const gutterId = 'hud-pin-gutter-' + docId;
      htmx.ajax('POST', `/document/${docId}/pin`, {
        target: '#' + gutterId,
        swap: 'beforeend',
        values: { passage_id: passageId },
      });
    },

    _incrementSpinePinCount(passageId) {
      const existing = this.$el.querySelector(`[data-spine-passage-ref="${passageId}"]`);
      if (existing) {
        const count = parseInt(existing.dataset.spinePinCount || '0') + 1;
        existing.dataset.spinePinCount = count;
        existing.title = `${count} pin(s)`;
        existing.textContent = `📌 ${count}`;
      } else {
        const pinBtn = this.$el.querySelector(`[data-pin-button="${passageId}"]`);
        if (pinBtn) {
          const chip = document.createElement('span');
          chip.className = 'text-[9px] font-bold text-amber';
          chip.dataset.spinePinCount = '1';
          chip.dataset.spinePassageRef = passageId;
          chip.title = '1 pin(s)';
          chip.textContent = '📌 1';
          pinBtn.before(chip);
        }
      }
    },

    _decrementSpinePinCount(passageId) {
      const chip = this.$el.querySelector(`[data-spine-passage-ref="${passageId}"]`);
      if (!chip) return;
      const count = parseInt(chip.dataset.spinePinCount || '0') - 1;
      if (count <= 0) {
        chip.remove();
      } else {
        chip.dataset.spinePinCount = count;
        chip.title = `${count} pin(s)`;
        chip.textContent = `📌 ${count}`;
      }
    },

    createPinAtActive() {
      // Prefer a text selection inside a <mark data-passage-id> anchor.
      const sel = window.getSelection();
      if (sel && sel.anchorNode) {
        const node = sel.anchorNode;
        const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
        const mark = el && el.closest('[data-passage-id]');
        if (mark && mark.dataset.passageId) {
          this.createPinAt(mark.dataset.passageId);
          return;
        }
      }
      if (this.activePassageId) this.createPinAt(this.activePassageId);
    },

    focusReactionBar() {
      const docId = this.$el.dataset.docId;
      if (!docId) return;
      const bar = document.getElementById(`hud-reaction-bar-${docId}`);
      if (bar) {
        bar.scrollIntoView({ behavior: 'smooth', block: 'center' });
        const firstBtn = bar.querySelector('button');
        if (firstBtn) firstBtn.focus();
      }
    },

    fireReaction(n) {
      const reactionKeys = ['lies', 'true', 'needs_proof', 'precedent'];
      const rval = reactionKeys[n - 1];
      if (!rval) return;
      const docId = this.$el.dataset.docId;
      if (!docId) return;
      const bar = document.getElementById(`hud-reaction-bar-${docId}`);
      if (!bar) return;
      const btn = bar.querySelector(`[data-reaction-key="${rval}"]`);
      if (btn) btn.click();
    },

    focusAskAi() {
      this.docChatOpen = true;
      window.dispatchEvent(new CustomEvent('hud-focus-chat'));
    },

    focusAskAiWithPassage(pid, passageText) {
      this.docChatOpen = true;
      const prompt = passageText ? `Regarding the passage: "${passageText}"\n\n` : '';
      window.dispatchEvent(new CustomEvent('hud-prefill-chat', { detail: { prompt } }));
    },

    showShortcuts() {
      if (Alpine.store('shortcuts')) Alpine.store('shortcuts').showHud = true;
    },

    confirmPrimary() {
      const focused = document.activeElement;
      if (focused && (focused.tagName === 'BUTTON' || focused.tagName === 'INPUT')) {
        focused.click();
      } else {
        const docId = this.$el.dataset.docId;
        const bar = document.getElementById(`hud-reaction-bar-${docId}`);
        if (bar) {
          const activeBtn = bar.querySelector('button.bg-primary\\/15, button.bg-originator-own\\/15');
          if (activeBtn) activeBtn.click();
        }
      }
    },

    toggleFocusMode() {
      this.focusModeActive = !this.focusModeActive;
      const railId = 'hud-rail-' + this.$el.dataset.docId;
      const rail = document.getElementById(railId);
      if (rail) rail.classList.toggle('hidden', this.focusModeActive);
    },

    navigatePrev() {
      const prevId = this.$el.dataset.prevDocId;
      const caseId = this.$el.dataset.caseId;
      if (prevId && caseId) window.location.href = `/cases/${caseId}/document/${prevId}`;
    },

    navigateNext() {
      const nextId = this.$el.dataset.nextDocId;
      const caseId = this.$el.dataset.caseId;
      if (nextId && caseId) window.location.href = `/cases/${caseId}/document/${nextId}`;
    },

    navigateParent() {
      const parentId = this.$el.dataset.parentDocId;
      const caseId = this.$el.dataset.caseId;
      if (parentId && caseId) window.location.href = `/cases/${caseId}/document/${parentId}`;
    },

    navigateFirstChild() {
      const childId = this.$el.dataset.firstChildDocId;
      const caseId = this.$el.dataset.caseId;
      if (childId && caseId) window.location.href = `/cases/${caseId}/document/${childId}`;
    },

    navigateBundlePrev() {
      const bundlePrevId = this.$el.dataset.bundlePrev;
      const caseId = this.$el.dataset.caseId;
      if (bundlePrevId && caseId) window.location.href = `/cases/${caseId}/document/${bundlePrevId}`;
    },

    navigateBundleNext() {
      const bundleNextId = this.$el.dataset.bundleNext;
      const caseId = this.$el.dataset.caseId;
      if (bundleNextId && caseId) window.location.href = `/cases/${caseId}/document/${bundleNextId}`;
    },

    movePrevPassage() {
      const spineRows = Array.from(document.querySelectorAll('[data-spine-passage]'));
      if (!spineRows.length) return;
      const idx = spineRows.findIndex(r => r.dataset.spinePassage === this.activePassageId);
      const target = idx > 0 ? spineRows[idx - 1] : spineRows[spineRows.length - 1];
      this.focusPassage(target.dataset.spinePassage);
    },

    moveNextPassage() {
      const spineRows = Array.from(document.querySelectorAll('[data-spine-passage]'));
      if (!spineRows.length) return;
      const idx = spineRows.findIndex(r => r.dataset.spinePassage === this.activePassageId);
      const target = idx < spineRows.length - 1 ? spineRows[idx + 1] : spineRows[0];
      this.focusPassage(target.dataset.spinePassage);
    },

    destroy() {
      if (this.observer) this.observer.disconnect();
      if (this._pinResizeObserver) this._pinResizeObserver.disconnect();
    },
  };
}
