/**
 * HUD reader — scroll-spy, passage focus, and shortcut registry.
 * Loaded only on full-screen document page (standalone context).
 */

// ── HUD component registration (waits for Alpine) ─────────────────────────

document.addEventListener('alpine:init', () => {
  Alpine.store('shortcuts', {
    showHud: false,
    handlers: [],
    hudReader: null,
    register(keys, handler) {
      this.handlers.push({ keys: Array.isArray(keys) ? keys : [keys], handler });
    },
    setHudReader(reader) {
      this.hudReader = reader;
    },
    handle(event) {
      const tagName = event.target.tagName;
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tagName)) return;
      if (event.target.isContentEditable) return;
      const key = (event.ctrlKey ? 'ctrl+' : '') + (event.metaKey ? 'meta+' : '') + event.key;
      for (const { keys, handler } of this.handlers) {
        if (keys.includes(event.key) || keys.includes(key)) {
          event.preventDefault();
          handler(event);
          return;
        }
      }
    },
    showModal() {
      this.showHud = true;
    },
  });
});

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
  } else if (e.key === '?') {
    e.preventDefault();
    reader.showShortcuts();
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

      // Register with shortcuts store for direct keydown handling
      if (window.Alpine && Alpine.store('shortcuts')) {
        Alpine.store('shortcuts').setHudReader(this);
      }
      // Also store on window for direct keydown access
      window.__hudReader = this;

      // Deep-link scroll — if URL has #p=<id>, scroll to that mark on load.
      this._handleFragment();

      // IntersectionObserver scroll-spy (standalone + embedded only).
      this._initScrollSpy();

      // Delegated click on highlighted marks in body.
      this._initMarkClicks();

      // Register keyboard shortcuts.
      this._registerShortcuts();

      // Position existing margin pins after layout settles.
      this.$nextTick(() => this._positionPins());

      // Re-position pins when article resizes (e.g. window resize).
      this._observeArticleResize();

      // Re-position after HTMX drops a new pin card into the gutter.
      this.$el.addEventListener('htmx:afterSwap', (e) => {
        const gutterId = 'hud-pin-gutter-' + this.$el.dataset.docId;
        if (e.target && e.target.id === gutterId) this._positionPins();
        if (e.target && e.target.closest && e.target.closest('#' + gutterId)) this._positionPins();
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
      mark.scrollIntoView({ behavior: 'smooth', block: 'center' });
      this._flashMark(pid);
      history.pushState(null, '', `#p=${pid}`);
      this.activePassageId = pid;
      this._syncSpine(pid);
    },

    focusClaim(cid) {
      const anchor = document.getElementById(`claim-${cid}`);
      if (!anchor) return;
      // The mark element immediately follows the claim anchor span.
      const mark = anchor.nextElementSibling;
      const target = (mark && mark.tagName === 'MARK') ? mark : anchor;
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      if (mark && mark.tagName === 'MARK') {
        mark.style.animation = 'none';
        mark.classList.add('hud-mark-flash');
        mark.addEventListener('animationend', () => mark.classList.remove('hud-mark-flash'), { once: true });
      }
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
      gutter.querySelectorAll('.hud-pin-card').forEach(card => {
        const pid = card.dataset.passageId;
        const mark = document.getElementById(`p-${pid}`);
        if (!mark) return;
        const markRect = mark.getBoundingClientRect();
        card.style.top = (markRect.top - articleRect.top + article.scrollTop) + 'px';
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
      if (!passageId) return;
      const docId = this.$el.dataset.docId;
      if (!docId) return;
      const gutterId = 'hud-pin-gutter-' + docId;
      let gutter = document.getElementById(gutterId);
      if (!gutter) {
        const body = this.$el.querySelector('[class*="relative"]');
        if (body) {
          gutter = document.createElement('div');
          gutter.id = gutterId;
          gutter.className = 'absolute left-0 top-0 bottom-0 w-20 pointer-events-none';
          gutter.setAttribute('aria-label', 'Margin pins');
          body.prepend(gutter);
        }
      }
      htmx.ajax('POST', `/document/${docId}/pin`, {
        target: '#' + gutterId,
        swap: 'beforeend',
        values: { passage_id: passageId },
      }).then(() => this._positionPins());
    },

    createPinAtActive() {
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

    showShortcuts() {
      window.dispatchEvent(new CustomEvent('toggle-hud-shortcuts'));
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
      console.log('navigatePrev:', prevId, 'caseId:', caseId);
      if (prevId && caseId) window.location.href = `/cases/${caseId}/document/${prevId}`;
    },

    navigateNext() {
      const nextId = this.$el.dataset.nextDocId;
      const caseId = this.$el.dataset.caseId;
      console.log('navigateNext:', nextId, 'caseId:', caseId);
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

    _registerShortcuts() {
      if (!Alpine.store('shortcuts')) return;
      const store = Alpine.store('shortcuts');
      store.register(['ArrowLeft'], () => this.navigatePrev());
      store.register(['ArrowRight'], () => this.navigateNext());
      store.register(['ArrowUp'], () => this.movePrevPassage());
      store.register(['ArrowDown'], () => this.moveNextPassage());
      store.register(['f'], () => this.toggleFocusMode());
      store.register(['Escape'], () => {
        if (this.focusModeActive) {
          this.toggleFocusMode();
        } else {
          const caseId = this.$el.dataset.caseId;
          if (caseId) window.location.href = `/cases/${caseId}`;
        }
      });
      store.register(['['], () => this.navigateParent());
      store.register([']'], () => this.navigateFirstChild());
      store.register(['{'], () => this.navigateBundlePrev());
      store.register(['}'], () => this.navigateBundleNext());
      store.register(['o'], () => {
        const docId = this.$el.dataset.docId;
        if (docId) window.open(`/document/${docId}/original`, '_blank', 'noopener');
      });
      store.register(['n'], () => this.createPinAtActive());
      store.register(['r'], () => this.focusReactionBar());
      store.register(['?'], () => this.showShortcuts());
      store.register(['ctrl+Enter'], (e) => this.confirmPrimary());
    },

    destroy() {
      if (this.observer) this.observer.disconnect();
      if (this._pinResizeObserver) this._pinResizeObserver.disconnect();
    },
  };
}
