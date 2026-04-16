// Common Alpine.js utilities
// Include this in base.html to make utilities available globally

document.addEventListener('alpine:init', () => {
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
            this.show(message, 'error', 5000);
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
});

// Alpine CDN build already exposes window.Alpine; no extra global wiring needed here.
