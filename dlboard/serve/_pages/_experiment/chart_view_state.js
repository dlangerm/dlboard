// What a viewer has changed in a chart without editing it (which layers are shown, say), kept in the browser for this tab.
//
// A chart whose type keeps viewer state (`ChartType.viewer_state_type`) names it by its card's `data-dl-view-key`, and
// reads and writes it here. Neither the chart nor the card needs the other: the chart draws what it is told, and the
// card's "View changed" bar (shown while the state is not empty) offers to save it as the chart's own settings, or to drop
// it. State is per experiment (the page's path), so a chart of one config in two experiments is two.
//
// A plain script: the chart's own scripts call `read` and `write`, and a click anywhere on a card's bar is handled here.
window.dlChartViewState = (() => {
    const EVENT = "dl-chart-view-state";
    const storageKey = (key) => `dl-chart-view:${window.location.pathname}:${key}`;

    const read = (key) => {
        try {
            return JSON.parse(window.sessionStorage.getItem(storageKey(key)));
        } catch (_error) {
            return null;
        }
    };

    // Sets `data-dl-view-changed` on the cards of `key`, which is what shows their bar.
    const mark = (key) => {
        const changed = read(key) !== null;
        for (const card of document.querySelectorAll(".dl-chart-item[data-dl-view-key]")) {
            if (card.dataset.dlViewKey === key) {
                card.toggleAttribute("data-dl-view-changed", changed);
            }
        }
    };

    const write = (key, state) => {
        try {
            window.sessionStorage.setItem(storageKey(key), JSON.stringify(state));
        } catch (_error) {
            // A browser that keeps nothing keeps nothing: the change still shows until the chart is drawn again.
        }
        mark(key);
    };

    // Forgets the state, and tells the chart to draw what it has saved.
    const clear = (key) => {
        try {
            window.sessionStorage.removeItem(storageKey(key));
        } catch (_error) {
            // As above.
        }
        mark(key);
        document.dispatchEvent(new CustomEvent(EVENT, { detail: { key } }));
    };

    // A card drawn again (a panel opened, a page of charts turned, a live update) shows its bar if it has state.
    new MutationObserver((records) => {
        for (const { addedNodes } of records) {
            for (const node of addedNodes) {
                if (node.nodeType !== Node.ELEMENT_NODE) {
                    continue;
                }
                const cards = node.matches(".dl-chart-item[data-dl-view-key]")
                    ? [node]
                    : node.querySelectorAll(".dl-chart-item[data-dl-view-key]");
                for (const card of cards) {
                    mark(card.dataset.dlViewKey);
                }
            }
        }
    }).observe(document.documentElement, { childList: true, subtree: true });

    document.addEventListener("click", (event) => {
        const button = event.target.closest(".dl-chart-view-bar button");
        const card = button?.closest(".dl-chart-item[data-dl-view-key]");
        if (!card) {
            return;
        }
        const key = card.dataset.dlViewKey;
        if (button.classList.contains("dl-chart-view-reset")) {
            clear(key);
        } else if (button.classList.contains("dl-chart-view-save")) {
            // `set_props` is a Dash escape hatch outside its declared callbacks, as `_experiment_page_dragdrop.js` uses it.
            window.dash_clientside.set_props(button.closest(".dl-chart-view-bar").dataset.dlSaveStore, {
                data: { chart_id: key, state: read(key), nonce: Date.now() },
            });
        }
    });

    return { read, write, clear, event: EVENT };
})();
