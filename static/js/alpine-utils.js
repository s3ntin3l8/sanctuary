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
