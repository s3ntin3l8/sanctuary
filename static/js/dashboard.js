document.addEventListener('alpine:init', () => {
  Alpine.data('caseDashboard', (initial) => ({
    view: initial.view || 'graph',
    filter: initial.filter || 'significant+',
    nodeCounts: initial.nodeCounts || {},
    selectedDocId: null,
    chatOpen: false,
    reviewOpen: false,
    procOpen: false,
    partyFilter: null,   // 'court' | 'opposing' | 'own' | 'third' | null

    init() {
      window._dashOpenDoc = (id) => this.openDoc(id);
      this.$el.addEventListener('set-filter', (e) => { this.filter = e.detail.filter; });
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

    isNodeDimmed(lane) {
      if (!this.partyFilter) return false;
      return lane !== this.partyFilter;
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
      if (e.key === 'f') { e.preventDefault(); this.$dispatch('graph-fit'); return; }
      if (e.key === 'c') { e.preventDefault(); this.$dispatch('graph-center-critical'); return; }
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
      if (e.key === '?') {
        alert('Keyboard shortcuts:\ng – Graph  t – Truth Map  l – Timeline  $ – Financials\nf – Fit Graph  c – Center Critical  a – Action Items  r – Refresh Brief\n/ – AI Chat  Esc – Close panel');
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

});
