/**
 * Correspondence Graph Dashboard Logic
 * Includes CaseGraphRenderer class and Alpine.js caseDashboard component.
 */

// ── CaseGraphRenderer ──────────────────────────────────────────────────────────

class CaseGraphRenderer {
  constructor(containerOrId, viewportOrId) {
    this.container = typeof containerOrId === 'string' ? document.getElementById(containerOrId) : containerOrId;
    this.viewport = typeof viewportOrId === 'string' ? document.getElementById(viewportOrId) : viewportOrId;

    if (!this.container || !this.viewport) {
      console.warn('CaseGraphRenderer: Container or viewport not found', { containerOrId, viewportOrId });
      return;
    }

    this.axisViewport = this.container.querySelector('#axis-viewport');
    this.legendViewport = this.container.querySelector('#legend-viewport');
    this.svg = this.container.querySelector('svg');

    // Camera state
    this.scale = 1.0;
    this.tx = 0;
    this.ty = 0;

    // Interaction state
    this.isDragging = false;
    this.lastMouseX = 0;
    this.lastMouseY = 0;

    this.init();
  }

  init() {
    this.container.addEventListener('mousedown', this.handleMouseDown.bind(this));
    window.addEventListener('mousemove', this.handleMouseMove.bind(this));
    window.addEventListener('mouseup', this.handleMouseUp.bind(this));
    this.container.addEventListener('wheel', this.handleWheel.bind(this), { passive: false });
    this.applyTransform();
  }

  handleMouseDown(e) {
    if (e.target.closest('.graph-node') || e.target.closest('.graph-edge')) return;
    if (e.button !== 0) return;
    this.isDragging = true;
    this.lastMouseX = e.clientX;
    this.lastMouseY = e.clientY;
    this.container.style.cursor = 'grabbing';
    e.preventDefault();
  }

  handleMouseMove(e) {
    if (!this.isDragging) return;
    const dx = (e.clientX - this.lastMouseX);
    const dy = (e.clientY - this.lastMouseY);
    this.tx += dx;
    this.ty += dy;
    this.lastMouseX = e.clientX;
    this.lastMouseY = e.clientY;
    this.applyTransform();
  }

  handleMouseUp() {
    this.isDragging = false;
    this.container.style.cursor = '';
  }

  handleWheel(e) {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      e.stopPropagation();
      const delta = -e.deltaY;
      const factor = Math.pow(1.1, delta / 100);
      const oldScale = this.scale;
      this.scale = Math.max(0.1, Math.min(5, this.scale * factor));
      const rect = this.svg.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const mouseY = e.clientY - rect.top;
      this.tx = mouseX - (mouseX - this.tx) * (this.scale / oldScale);
      this.ty = mouseY - (mouseY - this.ty) * (this.scale / oldScale);
      this.applyTransform();
    }
  }

  applyTransform() {
    if (!this.viewport) return;
    requestAnimationFrame(() => {
      this.viewport.setAttribute('transform', `translate(${this.tx}, ${this.ty}) scale(${this.scale})`);
      if (this.axisViewport) {
        // Sticky axis: ignore horizontal pan (tx), but follow vertical pan (ty) and zoom (scale)
        this.axisViewport.setAttribute('transform', `translate(0, ${this.ty}) scale(${this.scale})`);
      }
      if (this.legendViewport) {
        // Pinned legend: stay at top-right of viewport regardless of pan/scroll/zoom
        const w = this.container.clientWidth;
        const scrollX = this.container.scrollLeft;
        const scrollY = this.container.scrollTop;

        // Target coordinates in the raw SVG space that counteract scale and translation
        const targetX = (scrollX + w - 190 - this.tx) / this.scale;
        const targetY = (scrollY + 16 - this.ty) / this.scale;

        this.legendViewport.setAttribute('transform', `translate(${targetX * this.scale + this.tx}, ${targetY * this.scale + this.ty})`);
      }
    });
  }

  focusOn(x, y, zoom = 1.0) {
    if (!this.container) return;
    const rect = this.container.getBoundingClientRect();
    this.scale = zoom;
    this.tx = rect.width/2 - x * this.scale;
    this.ty = rect.height/2 - y * this.scale;
    this.applyTransform();
  }

  fit() {
    if (!this.viewport || !this.container) return;

    let attempts = 0;
    const MAX_ATTEMPTS = 10;

    const tryFit = () => {
      const bbox = this.viewport.getBBox();
      const rect = this.container.getBoundingClientRect();

      // If SVG hasn't laid out yet, retry in the next frame
      if ((bbox.width === 0 || bbox.height === 0) && attempts < MAX_ATTEMPTS) {
        attempts++;
        requestAnimationFrame(tryFit);
        return;
      }

      if (bbox.width === 0 || bbox.height === 0) return;

      const padding = 60;
      const availableW = rect.width - padding * 2;

      const scaleW = availableW / bbox.width;
      this.scale = Math.max(0.6, Math.min(scaleW, 1.1));

      this.tx = (rect.width - bbox.width * this.scale) / 2 - bbox.x * this.scale;
      this.ty = 32 - bbox.y * this.scale;

      this.applyTransform();
    };

    tryFit();
  }

  setHighlight(nodeId) {
    if (!this.svg) return;
    if (!nodeId) {
      this.svg.classList.remove('is-highlighting');
      this.svg.querySelectorAll('.highlighted').forEach(el => el.classList.remove('highlighted'));
      return;
    }
    this.svg.classList.add('is-highlighting');
    const targetNode = this.svg.querySelector(`.graph-node[data-id="${nodeId}"]`);
    if (targetNode) targetNode.classList.add('highlighted');

    const edges = this.svg.querySelectorAll(`.graph-edge[data-from="${nodeId}"], .graph-edge[data-to="${nodeId}"], .graph-edge[data-edge-to="${nodeId}"]`);
    edges.forEach(edge => {
      edge.classList.add('highlighted');
      const fromId = edge.getAttribute('data-from');
      const toId = edge.getAttribute('data-to') || edge.getAttribute('data-edge-to');
      this.svg.querySelector(`.graph-node[data-id="${fromId}"]`)?.classList.add('highlighted');
      this.svg.querySelector(`.graph-node[data-id="${toId}"]`)?.classList.add('highlighted');
    });
  }
}

window.CaseGraphRenderer = CaseGraphRenderer;

// ── Alpine Component ─────────────────────────────────────────────────────────

function registerCaseDashboardComponent() {
  if (Alpine.data('caseDashboard')) return;
  registerCaseDashboard();
}

if (window.Alpine) {
  registerCaseDashboardComponent();
} else {
  document.addEventListener('alpine:init', () => registerCaseDashboardComponent());
}

function registerCaseDashboard() {
  Alpine.data('caseDashboard', (initial) => ({
    view: initial.view || 'graph',
    filter: initial.filter || 'significant+',
    nodeCounts: initial.nodeCounts || {},
    selectedDocId: null,
    chatOpen: false,
    docChatOpen: false,
    reviewOpen: false,
    procOpen: false,
    partyFilter: null,
    actionItemsCollapsed: false,

    renderer: null,
    contextMenu: { open: false, x: 0, y: 0, docId: null },
    hoveredNodeId: null,

    init() {
      window._dashOpenDoc = (id) => this.openDoc(id);
      this.$el.addEventListener('set-filter', (e) => { this.filter = e.detail.filter; });

      this.actionItemsCollapsed = localStorage.getItem('sanctuary:actionItemsCollapsed') === '1';

      // Handle deep-linking to specific views or claims via URL
      const urlParams = new URLSearchParams(window.location.search);
      const viewParam = urlParams.get('view');
      if (viewParam) {
        this.view = viewParam;
      }

      // Re-sync the active view when the user navigates with the browser
      // back/forward buttons. setView pushes new history entries; without
      // this listener the URL would update but the Alpine state would not.
      window.addEventListener('popstate', () => {
        const v = new URLSearchParams(window.location.search).get('view') || 'graph';
        if (v !== this.view) this.view = v;
      });

      this.$nextTick(() => {
        if (this.view === 'graph') {
          this.initRenderer();
          this.scrollToBottom(false);
        } else if (this.view === 'truth') {
          const hash = window.location.hash;
          if (hash.startsWith('#claim-')) {
            const claimId = hash.replace('#claim-', '');
            const el = document.getElementById(`claim-card-${claimId}`);
            if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
        }
      });
      this.$watch('view', (v) => {
        if (v === 'graph') {
          this.$nextTick(() => {
            this.initRenderer();
            this.scrollToBottom(true);
          });
        }
      });
    },

    initRenderer() {
      if (!this.renderer) {
        try {
          const container = this.$el.querySelector('#correspondence-graph-container');
          const viewport = this.$el.querySelector('#viewport-content');
          if (container && viewport) {
            this.renderer = new CaseGraphRenderer(container, viewport);
            if (initial.graph && initial.graph.nodes && initial.graph.nodes.length > 0) {
              this.renderer.fit();
            }
          }
        } catch (err) {
          console.error('Failed to init CaseGraphRenderer:', err);
        }
      }
    },

    scrollToBottom(smooth = true) {
      const container = this.$el.querySelector('#correspondence-graph-container');
      if (!container) return;

      let lastHeight = container.scrollHeight;
      let sameCount = 0;
      const MAX_POLLS = 30; // 3 seconds max

      const poll = () => {
        const currentHeight = container.scrollHeight;

        // Push to bottom as we go
        container.scrollTop = currentHeight;

        // If height hasn't changed, increment counter
        if (currentHeight > 0 && Math.abs(currentHeight - lastHeight) < 1) {
          sameCount++;
        } else {
          sameCount = 0;
          lastHeight = currentHeight;
        }

        // We need 5 consecutive frames of stability for a large SVG
        if (sameCount >= 5) {
          if (smooth && currentHeight > 0) {
            container.scrollTo({ top: currentHeight, behavior: 'smooth' });
          }
          return;
        }

        if (sameCount + sameCount > MAX_POLLS * 2) return;

        requestAnimationFrame(poll);
      };

      requestAnimationFrame(poll);
    },
    fit() {
      if (this.renderer) {
        this.renderer.fit();
        this.scrollToBottom(true);
      } else {
        this.initRenderer();
      }
    },

    centerCritical() {
      const crit = document.querySelector('.text-critical');
      if (crit && this.renderer) {
        const x = parseFloat(crit.parentNode.getAttribute('x') || 0);
        const y = parseFloat(crit.parentNode.getAttribute('y') || 0);
        this.renderer.focusOn(x + 90, y + 25, 1.2);
      } else if (crit) {
        crit.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
      }
    },

    setView(v) {
      if (v === this.view) return;
      this.view = v;
      // Persist the active view in the URL so a refresh keeps the user on
      // the same tab. Default `/cases/<id>` (no query) still falls back to
      // graph on the server side, which is the intended initial-visit UX.
      const url = new URL(window.location.href);
      url.searchParams.set('view', v);
      window.history.pushState({ view: v }, '', url);
    },

    openDoc(id) {
      if (this.selectedDocId === id) {
        this.closeDoc();
        return;
      }
      const wasOpen = this.selectedDocId !== null;
      this.selectedDocId = id;
      if (wasOpen) document.body.classList.add('hud-swapping');
      htmx.ajax('GET', `/cases/${initial.caseId}/document/${id}/hud`, {
        target: '#case-dashboard-hud',
        swap: 'innerHTML',
      });
      if (wasOpen) {
        requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.remove('hud-swapping')));
      }
    },

    closeDoc() {
      this.selectedDocId = null;
      const el = document.getElementById('case-dashboard-hud');
      if (el) el.innerHTML = '';
    },

    onNodeHover(id) {
      this.hoveredNodeId = id;
      if (this.renderer) this.renderer.setHighlight(id);
    },

    onNodeLeave() {
      this.hoveredNodeId = null;
      if (this.renderer) this.renderer.setHighlight(null);
    },

    openContextMenu(e, id) {
      this.contextMenu = { open: true, x: e.clientX, y: e.clientY, docId: id };
    },

    closeContextMenu() { this.contextMenu.open = false; },

    togglePartyFilter(key) { this.partyFilter = this.partyFilter === key ? null : key; },

    toggleActionItems() {
      this.actionItemsCollapsed = !this.actionItemsCollapsed;
      localStorage.setItem('sanctuary:actionItemsCollapsed', this.actionItemsCollapsed ? '1' : '0');
    },

    isNodeHidden(tier, role) {
      if (this.filter === 'critical') return tier !== 'critical';
      if (this.filter === 'significant+') return tier === 'administrative' && role !== 'cover_letter';
      return false;
    },

    hiddenCount() {
      const c = this.nodeCounts;
      if (!c) return 0;
      if (this.filter === 'all') return 0;
      if (this.filter === 'significant+') return c.administrative_standalone || 0;
      if (this.filter === 'critical') {
        const total = (c.critical || 0) + (c.significant || 0) + (c.informational || 0)
                    + (c.administrative_standalone || 0) + (c.administrative_relay || 0);
        return total - (c.critical || 0);
      }
      return 0;
    },

    isNodeDimmed(lane) { if (!this.partyFilter) return false; return lane !== this.partyFilter; },

    onKey(e) {
      if (['INPUT', 'TEXTAREA'].includes(e.target.tagName)) return;
      if (e.key === 'Escape') {
        if (this.contextMenu.open) { this.closeContextMenu(); return; }
        if (this.chatOpen || this.docChatOpen) { this.chatOpen = false; this.docChatOpen = false; return; }
        this.closeDoc();
        this.reviewOpen = false; this.procOpen = false;
        return;
      }
      if (e.key === '/') {
        e.preventDefault();
        if (this.selectedDocId) {
          this.docChatOpen = !this.docChatOpen;
          if (this.docChatOpen) window.dispatchEvent(new CustomEvent('hud-focus-chat'));
        } else {
          this.chatOpen = !this.chatOpen;
        }
        return;
      }
      if (e.key === 'g') this.setView('graph');
      if (e.key === 't') this.setView('truth');
      if (e.key === 'l') this.setView('timeline');
      if (e.key === '$') this.setView('fin');
      if (e.key === 'f') { e.preventDefault(); this.fit(); return; }
      if (e.key === 'c') { e.preventDefault(); this.centerCritical(); return; }
      if (e.key === 'a') {
        e.preventDefault();
        document.getElementById('action-items-anchor')?.scrollIntoView({ behavior: 'smooth' });
        return;
      }
      if (e.key === 'r') {
        e.preventDefault();
        const btn = document.getElementById('refresh-brief-btn');
        if (btn) htmx.trigger(btn, 'click');
        return;
      }
    },

  }));
}

// initCaseDashboard called above via if(window.Alpine) check
