/**
 * Alpine.js component for the slicing review UI.
 */
function slicingGrid(initialData, batchId) {
    return {
        status: initialData.status || 'preparing',
        pageCount: initialData.page_count || 0,
        cuts: [...(initialData.suggested_cuts || [])],
        initialCuts: [...(initialData.suggested_cuts || [])],
        groupTitles: [],
        focusedGutter: 0,
        confirming: false,
        errorMessage: initialData.error || '',

        init() {
            if (this.status === 'ready') {
                this.initializeTitles();
            }
        },

        initializeTitles() {
            const count = this.groupCount;
            this.groupTitles = Array(count).fill('').map((_, i) => `Document ${i + 1}`);
        },

        get groupCount() {
            return this.cuts.length + 1;
        },

        get gridItems() {
            const items = [];
            let currentGroupStart = 1;
            let groupIdx = 0;

            for (let p = 1; p <= this.pageCount; p++) {
                // Before first page of a group, add header
                if (p === currentGroupStart) {
                    let range = '';
                    let end = this.pageCount;
                    // Find next cut
                    const nextCut = this.cuts.find(c => c >= p);
                    if (nextCut) end = nextCut;

                    range = p === end ? `${p}` : `${p}–${end}`;

                    items.push({
                        type: 'group-header',
                        groupIndex: groupIdx,
                        pageRange: range
                    });
                }

                // The page itself
                items.push({
                    type: 'page',
                    pageNum: p
                });

                // If not last page, add a gutter
                if (p < this.pageCount) {
                    items.push({
                        type: 'gutter',
                        afterPage: p,
                        gutterIdx: p - 1 // 0-based index for gutters
                    });

                    // If there was a cut here, the next page starts a new group
                    if (this.cuts.includes(p)) {
                        currentGroupStart = p + 1;
                        groupIdx++;
                    }
                }
            }
            return items;
        },

        hasCut(pageNum) {
            return this.cuts.includes(pageNum);
        },

        toggleCut(pageNum) {
            const idx = this.cuts.indexOf(pageNum);
            if (idx === -1) {
                this.cuts.push(pageNum);
                this.cuts.sort((a, b) => a - b);
            } else {
                this.cuts.splice(idx, 1);
            }
            this.syncTitles();
        },

        syncTitles() {
            // Adjust titles array length while preserving existing names
            const newCount = this.groupCount;
            if (this.groupTitles.length < newCount) {
                for (let i = this.groupTitles.length; i < newCount; i++) {
                    this.groupTitles.push(`Document ${i + 1}`);
                }
            } else if (this.groupTitles.length > newCount) {
                this.groupTitles = this.groupTitles.slice(0, newCount);
            }
        },

        clearCuts() {
            this.cuts = [];
            this.syncTitles();
        },

        resetCuts() {
            this.cuts = [...this.initialCuts];
            this.syncTitles();
        },

        handleKey(e) {
            if (this.status !== 'ready') return;
            if (e.target.tagName === 'INPUT') return;

            const key = e.key.toLowerCase();
            const maxGutter = this.pageCount - 2; // index of last gutter

            if (key === 'arrowdown') {
                e.preventDefault();
                this.focusedGutter = Math.min(this.focusedGutter + 1, this.pageCount - 2);
                this.scrollToFocused();
            } else if (key === 'arrowup') {
                e.preventDefault();
                this.focusedGutter = Math.max(this.focusedGutter - 1, 0);
                this.scrollToFocused();
            } else if (key === 'c') {
                this.toggleCut(this.focusedGutter + 1);
            } else if (key === 'enter') {
                this.confirm();
            } else if (key === 'escape') {
                window.location.href = '/triage';
            }
        },

        scrollToFocused() {
            this.$nextTick(() => {
                const el = document.querySelector('.cursor-focused');
                if (el) {
                    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            });
        },

        refreshIfReady(e) {
            if (e.detail.xhr.status === 200) {
                try {
                    const data = JSON.parse(e.detail.xhr.responseText);
                    if (data.status === 'ready') {
                        window.location.reload();
                    } else if (data.status === 'failed') {
                        this.status = 'failed';
                        this.errorMessage = data.error || 'Preparation failed.';
                    }
                } catch (err) {
                    // status might not be JSON if it's a redirect or similar
                }
            }
        },

        confirm() {
            if (this.confirming) return;
            this.confirming = true;
            document.getElementById('slicing-confirm-form').submit();
        }
    };
}
