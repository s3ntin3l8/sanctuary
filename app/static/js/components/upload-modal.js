// Upload Modal Component - Reusable file upload with Alpine.js
// Usage: Add x-data="uploadModal()" to parent element

document.addEventListener('alpine:init', () => {
    Alpine.data('uploadModal', (options = {}) => ({
        open: false,
        files: [],
        uploading: false,
        progress: 0,
        error: null,
        defaultCaseId: options.defaultCaseId || '_TRIAGE',

        init() {
            // Listen for upload events from other components
            window.addEventListener('open-upload-modal', (e) => {
                this.open = true;
                if (e.detail?.caseId) {
                    this.defaultCaseId = e.detail.caseId;
                }
            });
        },

        openModal(caseId = null) {
            this.files = [];
            this.error = null;
            this.progress = 0;
            if (caseId) this.defaultCaseId = caseId;
            this.open = true;
        },

        closeModal() {
            this.open = false;
            this.files = [];
            this.error = null;
        },

        handleFileSelect(event) {
            const newFiles = Array.from(event.target.files);
            this.files = [...this.files, ...newFiles];
            event.target.value = ''; // Reset input
        },

        removeFile(index) {
            this.files.splice(index, 1);
        },

        async uploadFiles(caseId = null) {
            if (this.files.length === 0) return;

            this.uploading = true;
            this.error = null;
            this.progress = 0;

            const targetCaseId = caseId || this.defaultCaseId;
            const formData = new FormData();
            formData.append('case_id', targetCaseId);

            this.files.forEach((file, index) => {
                formData.append('files', file);
            });

            try {
                const response = await fetch('/upload', {
                    method: 'POST',
                    body: formData
                });

                if (!response.ok) {
                    const data = await response.json();
                    throw new Error(data.detail || 'Upload failed');
                }

                this.files = [];
                this.progress = 100;

                // Dispatch event for other components to handle
                window.dispatchEvent(new CustomEvent('files-uploaded', {
                    detail: { caseId: targetCaseId }
                }));

                // Reload page after short delay
                setTimeout(() => {
                    window.location.reload();
                }, 1000);

            } catch (err) {
                this.error = err.message;
            } finally {
                this.uploading = false;
            }
        },

        getFileSize(bytes) {
            if (bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
        }
    }));
});

// Document list with filtering
document.addEventListener('alpine:init', () => {
    Alpine.data('documentList', (options = {}) => ({
        searchQuery: '',
        filterStatus: options.filterStatus || 'all',
        sortBy: options.sortBy || 'date',
        sortOrder: 'desc',

        get filteredDocuments() {
            let docs = options.documents || [];

            // Filter by search
            if (this.searchQuery) {
                const query = this.searchQuery.toLowerCase();
                docs = docs.filter(d =>
                    d.title?.toLowerCase().includes(query) ||
                    d.case_id?.toLowerCase().includes(query)
                );
            }

            // Filter by status
            if (this.filterStatus !== 'all') {
                docs = docs.filter(d => d.needs_review === (this.filterStatus === 'pending'));
            }

            // Sort
            docs.sort((a, b) => {
                let comparison = 0;
                switch (this.sortBy) {
                    case 'date':
                        comparison = new Date(a.created_at) - new Date(b.created_at);
                        break;
                    case 'title':
                        comparison = (a.title || '').localeCompare(b.title || '');
                        break;
                    case 'case':
                        comparison = (a.case_id || '').localeCompare(b.case_id || '');
                        break;
                }
                return this.sortOrder === 'desc' ? -comparison : comparison;
            });

            return docs;
        },

        clearFilters() {
            this.searchQuery = '';
            this.filterStatus = 'all';
        }
    }));
});
