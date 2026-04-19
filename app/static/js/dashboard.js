document.addEventListener('alpine:init', () => {
  Alpine.data('caseDashboard', (initial) => ({
    view: initial.view || 'graph',
    selectedDocId: null,
    chatOpen: false,
    reviewOpen: false,
    procOpen: false,
    partyFilter: null,   // 'court' | 'opposing' | 'own' | 'third' | null

    init() {
      // expose openDoc globally so HTMX afterSwap handlers and graph nodes can call it
      window._dashOpenDoc = (id) => this.openDoc(id);
    },

    setView(v) {
      this.view = v;
      this.persistView(v);
    },

    openDoc(id) {
      this.selectedDocId = id;
      htmx.ajax('GET', `/cases/${initial.caseId}/document/${id}/hud`, {
        target: '#case-dashboard-hud',
        swap: 'innerHTML',
      });
    },

    closeDoc() {
      this.selectedDocId = null;
      const el = document.getElementById('case-dashboard-hud');
      if (el) el.innerHTML = '';
    },

    togglePartyFilter(key) {
      this.partyFilter = this.partyFilter === key ? null : key;
    },

    onKey(e) {
      if (['INPUT', 'TEXTAREA'].includes(e.target.tagName)) return;
      if (e.key === 'Escape') {
        this.closeDoc();
        this.chatOpen = false;
        this.reviewOpen = false;
        this.procOpen = false;
        return;
      }
      if (e.key === '/') { e.preventDefault(); this.chatOpen = true; return; }
      if (e.key === 'g') this.setView('graph');
      if (e.key === 't') this.setView('truth');
      if (e.key === 'l') this.setView('timeline');
      if (e.key === '$') this.setView('fin');
      if (e.key === '?') {
        alert('Keyboard shortcuts:\ng – Graph  t – Truth Map  l – Timeline  $ – Financials\n/ – AI Chat  Esc – Close panel');
      }
    },

    persistView(v) {
      fetch('/api/user-settings/dashboard-view', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ view: v }),
      });
    },
  }));

  // -----------------------------------------------------------
  Alpine.data('correspondenceGraph', (initial) => ({
    filter: initial.filter || 'significant+',

    setFilter(f) {
      this.filter = f;
    },

    isNodeHidden(tier, role) {
      if (this.filter === 'critical') return tier !== 'critical';
      if (this.filter === 'significant+') return tier === 'administrative' && role !== 'cover_letter';
      return false;
    },

    isNodeDimmed(lane, partyFilter) {
      if (!partyFilter) return false;
      return lane !== partyFilter;
    },
  }));
});
