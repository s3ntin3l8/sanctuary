// Common Alpine.js utilities
// Include this in base.html to make utilities available globally

function registerGlobalAlpineData() {
    if (Alpine.data('confirmDialog')) return;

    // Confirmation dialog
    Alpine.data('confirmDialog', () => ({
        show: false,
        title: 'Confirm',
        message: 'Are you sure?',
        onConfirm: null,

        open(options = {}) {
            this.title = options.title || 'Confirm';
            this.message = options.message || 'Are you sure?';
            this.onConfirm = options.onConfirm || (() => {});
            this.show = true;
        },

        close() {
            this.show = false;
            this.onConfirm = null;
        },

        confirm() {
            if (this.onConfirm) this.onConfirm();
            this.close();
        }
    }));

    // Toast notifications
    Alpine.data('toast', () => ({
        messages: [],

        show(message, type = 'info', duration = 3000) {
            const id = Date.now();
            this.messages.push({ id, message, type });

            setTimeout(() => {
                this.remove(id);
            }, duration);
        },

        remove(id) {
            this.messages = this.messages.filter(m => m.id !== id);
        },
        success(message) {
            this.show(message, 'success');
        },

        error(message) {
            this.show(message, 'error');
        },

        warning(message) {
            this.show(message, 'warning', 4000);
        }
    }));

    // Collapsible sections
    Alpine.data('collapsible', (options = {}) => ({
        collapsed: options.collapsed || false,

        toggle() {
            this.collapsed = !this.collapsed;
        },

        expand() {
            this.collapsed = false;
        },

        collapse() {
            this.collapsed = true;
        }
    }));

    // Search/filter functionality
    Alpine.data('searchFilter', (options = {}) => ({
        query: '',
        results: [],

        search() {
            if (!this.query) {
                this.results = options.items || [];
                return;
            }

            const q = this.query.toLowerCase();
            this.results = (options.items || []).filter(item => {
                return options.searchFields.some(field => {
                    const value = field.split('.').reduce((obj, key) => obj?.[key], item);
                    return value?.toString().toLowerCase().includes(q);
                });
            });
        },

        clear() {
            this.query = '';
            this.results = options.items || [];
        }
    }));

    // Date range picker
    Alpine.data('dateRange', () => ({
        startDate: '',
        endDate: '',

        setRange(start, end) {
            this.startDate = start;
            this.endDate = end;
        },

        clear() {
            this.startDate = '';
            this.endDate = '';
        },

        get isValid() {
            if (!this.startDate || !this.endDate) return false;
            return new Date(this.startDate) <= new Date(this.endDate);
        }
    }));
}

if (window.Alpine) {
    registerGlobalAlpineData();
} else {
    document.addEventListener('alpine:init', () => registerGlobalAlpineData());
}

// Re-initialize Alpine after any HTMX swap so x-init (Sortable) and x-if/x-show
// directives activate on injected content. Alpine's MutationObserver does not
// reliably fire for programmatic htmx.ajax() outerHTML swaps.
document.addEventListener('htmx:afterSwap', (event) => {
    if (window.Alpine && event.target) {
        window.Alpine.initTree(event.target);
    }
});

document.addEventListener('htmx:oobAfterSwap', (event) => {
    if (window.Alpine && event.target) {
        window.Alpine.initTree(event.target);
    }
});

if (window.Alpine) {
    window.Alpine = Alpine;
} else {
    document.addEventListener('alpine:init', () => {
        window.Alpine = Alpine;
    });
}

window.initTriageSortable = function (rootEl, batchId, bundleKey) {
    if (typeof Sortable === 'undefined' || !rootEl) return;
    rootEl.querySelectorAll('[data-sortable-container]').forEach(function (el) {
        if (el._sortable) { el._sortable.destroy(); }
        el._sortable = Sortable.create(el, {
            group: 'subgroups-' + bundleKey,
            animation: 150,
            handle: '.drag-handle',
            ghostClass: 'ring-1 ring-primary/40 opacity-50',
            chosenClass: 'opacity-70',
            onEnd: function (evt) {
                if (!batchId) return;
                var containers = evt.from === evt.to ? [evt.from] : [evt.from, evt.to];
                containers.forEach(function (container) {
                    var subGroupId = container.dataset.subGroupId || '';
                    var leadDocId = container.dataset.leadDocId || '';
                    var docIds = Array.from(container.querySelectorAll('[data-doc-id]'))
                        .map(function (node) { return node.dataset.docId; })
                        .join(',');
                    htmx.ajax('POST', '/triage/bundle/' + batchId + '/reorder', {
                        target: '#triage-tree-' + bundleKey,
                        swap: 'outerHTML',
                        values: { sub_group_id: subGroupId, lead_doc_id: leadDocId, doc_ids: docIds },
                    });
                });
            },
        });
    });
};

function slicingGrid(slicingData, batchId) {
    return {
        batchId,
        status: slicingData.status || 'preparing',
        pageCount: slicingData.page_count || 0,
        errorMessage: slicingData.error || '',
        cuts: [],
        groupTitles: [],
        focusedGutter: 0,
        confirming: false,

        init() {
            if (slicingData.proposed_cuts) {
                this.cuts = slicingData.proposed_cuts.map(c => c.page - 1).filter(p => p > 0);
            }
            this._rebuildGroupTitles();
        },

        get groupCount() { return this.cuts.length + 1; },
        hasCut(afterPage) { return this.cuts.includes(afterPage); },

        toggleCut(afterPage) {
            const idx = this.cuts.indexOf(afterPage);
            if (idx >= 0) this.cuts.splice(idx, 1);
            else { this.cuts.push(afterPage); this.cuts.sort((a, b) => a - b); }
            this._rebuildGroupTitles();
        },

        clearCuts() { this.cuts = []; this._rebuildGroupTitles(); },

        resetCuts() {
            this.cuts = slicingData.proposed_cuts
                ? slicingData.proposed_cuts.map(c => c.page - 1).filter(p => p > 0)
                : [];
            this._rebuildGroupTitles();
        },

        _rebuildGroupTitles() {
            const n = this.cuts.length + 1;
            const existing = this.groupTitles.slice(0, n);
            while (existing.length < n) existing.push('');
            this.groupTitles = existing;
        },

        get gridItems() {
            if (this.status !== 'ready' || !this.pageCount) return [];
            const items = [];
            let groupIdx = 0, groupStart = 1, gutterIdx = 0;
            for (let p = 1; p <= this.pageCount; p++) {
                if (p === groupStart) {
                    const groupEnd = this.cuts.find(c => c >= p) || this.pageCount;
                    items.push({ type: 'group-header', groupIndex: groupIdx,
                                 pageRange: p === groupEnd ? `p${p}` : `pp${p}–${groupEnd}` });
                }
                items.push({ type: 'page', pageNum: p });
                if (p < this.pageCount) {
                    gutterIdx++;
                    items.push({ type: 'gutter', afterPage: p, gutterIdx });
                    if (this.hasCut(p)) { groupIdx++; groupStart = p + 1; }
                }
            }
            return items;
        },

        handleKey(e) {
            const tag = (e.target && e.target.tagName || '').toLowerCase();
            if (['input', 'textarea', 'select'].includes(tag)) return;
            const gutterCount = this.pageCount - 1;
            if (e.key === 'ArrowDown') { e.preventDefault(); this.focusedGutter = Math.min(this.focusedGutter + 1, gutterCount); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); this.focusedGutter = Math.max(this.focusedGutter - 1, 1); }
            else if (e.key === 'c' || e.key === 'C') { if (this.focusedGutter > 0) this.toggleCut(this.focusedGutter); }
            else if (e.key === 'Enter') { this.confirm(); }
            else if (e.key === 'Escape') { window.location = '/triage'; }
        },

        confirm() { this.confirming = true; document.getElementById('slicing-confirm-form').submit(); },

        refreshIfReady(event) {
            try {
                const data = JSON.parse(event.detail.xhr.responseText);
                if (data.status && data.status !== 'preparing') window.location.reload();
            } catch (_) {}
        },
    };
}
