// Case Stream Component - Extracted from case_stream.html
// Provides section navigation and document management

document.addEventListener('alpine:init', () => {
    Alpine.data('caseStream', () => ({
        activeDoc: null,
        activeSection: 'review',
        showParentPicker: null,
        sectionIds: ['review', 'chronology', 'calendar', 'costs', 'entities'],

        scrollToSection(section) {
            this.activeSection = section;
            const target = document.getElementById(`section-${section}`);
            if (target) {
                target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        },

        updateActiveSection() {
            const container = document.getElementById('stream-scroll');
            if (!container) return;

            const containerTop = container.getBoundingClientRect().top + 80;
            let current = this.sectionIds[0];

            this.sectionIds.forEach((section) => {
                const element = document.getElementById(`section-${section}`);
                if (!element) return;

                const sectionTop = element.getBoundingClientRect().top;
                if (sectionTop <= containerTop) {
                    current = section;
                }
            });

            this.activeSection = current;
        },

        openDoc(docId) {
            this.activeDoc = docId;
        },

        closeDoc() {
            this.activeDoc = null;
        },

        toggleParentPicker(docId) {
            this.showParentPicker = this.showParentPicker === docId ? null : docId;
        }
    }));

    // Triage page component
    Alpine.data('triagePage', () => ({
        selectedDocs: new Set(),

        toggleDoc(docId) {
            if (this.selectedDocs.has(docId)) {
                this.selectedDocs.delete(docId);
            } else {
                this.selectedDocs.add(docId);
            }
        },

        isSelected(docId) {
            return this.selectedDocs.has(docId);
        },

        selectAll(docs) {
            docs.forEach(d => this.selectedDocs.add(d.id));
        },

        clearSelection() {
            this.selectedDocs.clear();
        }
    }));
});

// Utility functions for template use
window.SanctuaryUtils = {
    debounce(func, wait) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    },

    formatCurrency(value, locale = 'de-DE', currency = 'EUR') {
        return new Intl.NumberFormat(locale, {
            style: 'currency',
            currency
        }).format(value);
    },

    parseQueryParams() {
        const params = new URLSearchParams(window.location.search);
        const result = {};
        for (const [key, value] of params) {
            result[key] = value;
        }
        return result;
    }
};
