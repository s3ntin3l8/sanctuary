function homeDashboard() {
    return {
        focusedPanel: null,
        focusedIdx: -1,
        showShortcuts: false,
        showCreateCase: false,

        init() {
            this.setupKeyboard();
            console.log("Home Dashboard initialized");
        },

        setupKeyboard() {
            window.addEventListener('keydown', (e) => {
                // Skip if typing in an input
                if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

                if (e.key === 'Escape' && (this.showShortcuts || this.showCreateCase)) {
                    this.showShortcuts = false;
                    this.showCreateCase = false;
                    return;
                }

                const panelKeys = {
                    't': 'today',
                    'i': 'triage',
                    'd': 'delta',
                    's': 'signals',
                    'c': 'cases'
                };

                if (panelKeys[e.key]) {
                    e.preventDefault();
                    this.focusedPanel = panelKeys[e.key];
                    this.focusedIdx = 0;
                    this.scrollToFocus();
                    return;
                }

                if (e.key === 'ArrowDown' || e.key === 'j') {
                    e.preventDefault();
                    this.navigate(1);
                } else if (e.key === 'ArrowUp' || e.key === 'k') {
                    e.preventDefault();
                    this.navigate(-1);
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    this.openFocused();
                } else if (e.key === 'r') {
                    e.preventDefault();
                    this.reviewAll();
                } else if (e.key === '?') {
                    e.preventDefault();
                    this.showShortcuts = !this.showShortcuts;
                }
            });
        },

        navigate(dir) {
            if (!this.focusedPanel) {
                this.focusedPanel = 'today';
                this.focusedIdx = dir > 0 ? 0 : (this.getPanelItemCount('today') - 1);
                this.scrollToFocus();
                return;
            }
            const count = this.getPanelItemCount(this.focusedPanel);
            if (count === 0) return;

            this.focusedIdx = (this.focusedIdx + dir + count) % count;
            this.scrollToFocus();
        },

        getPanelItemCount(panel) {
            return document.querySelectorAll(`[data-panel-item="${panel}"]`).length;
        },

        scrollToFocus() {
            this.$nextTick(() => {
                const el = document.querySelector(`[data-panel-item="${this.focusedPanel}"][data-index="${this.focusedIdx}"]`);
                if (el) {
                    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                }
            });
        },

        openFocused() {
            const el = document.querySelector(`[data-panel-item="${this.focusedPanel}"][data-index="${this.focusedIdx}"]`);
            if (el) {
                const link = el.tagName === 'A' ? el : el.querySelector('a');
                if (link && link.href) {
                    window.location.href = link.href;
                } else {
                    el.click();
                }
            }
        },

        reviewAll() {
            const btn = document.querySelector('button[hx-post="/home/review-all"]');
            if (btn) btn.click();
        }
    };
}
