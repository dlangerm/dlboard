// What a viewer has changed in a chart without editing it (which layers are shown, say), and how they have chosen to look at
// it (whether its legend is open), kept in the browser for this tab.
//
// A chart whose type keeps viewer state (`ChartType.viewer_state_type`) names it by its card's `data-dl-view-key`, and
// reads and writes it here. Neither the chart nor the card needs the other: the chart draws what it is told, and the
// card's "View changed" bar (shown while there is a `state`) offers to save it as the chart's own settings, or to drop it.
//
// Each chart has one record, `{state, local}`. The `state` is what "Save as default" saves and what "View changed" is
// about. The `local` part is only how the viewer looks at the chart, which is no change to it: it is never saved and never
// says "View changed". A record is kept per experiment *and view* (`?view=`), so a chart in two views of one experiment
// is two. An edit that branches the page into a view of its own leaves what was changed before it with the view it was
// changed in.
//
// A plain script, and private to dlboard's own charts: the image chart calls `read`, `write` and the rest, and a click
// anywhere on a card's bar is handled here. Records are read from sessionStorage once and kept in memory after.
window.dlChartViewState = (() => {
    const EVENT = "dl-chart-view-state";
    const EMPTY = { state: null, local: {} };

    const scope = () => {
        const view = new URLSearchParams(window.location.search).get("view");
        return view ? `${window.location.pathname}?view=${view}` : window.location.pathname;
    };
    const storageKey = (key) => `dl-chart-view:${scope()}:${key}`;

    const records = new Map();
    const load = (key) => {
        const at = storageKey(key);
        if (!records.has(at)) {
            let found = null;
            try {
                found = JSON.parse(window.sessionStorage.getItem(at));
            } catch (_error) {
                // A browser that keeps nothing keeps nothing: the record starts empty.
            }
            records.set(at, { ...EMPTY, ...found });
        }
        return records.get(at);
    };

    const save = (key, record) => {
        records.set(storageKey(key), record);
        try {
            window.sessionStorage.setItem(storageKey(key), JSON.stringify(record));
        } catch (_error) {
            // A browser that keeps nothing keeps nothing: the change still shows until the chart is drawn again.
        }
    };

    // Sets `data-dl-view-changed` on the cards of `key`, which is what shows their bar. A chart calls this when it draws, as
    // a card drawn again (a panel opened, a page of charts turned, a live update) shows its bar if it has state.
    const mark = (key) => {
        const changed = load(key).state !== null;
        for (const card of document.querySelectorAll(".dl-chart-item[data-dl-view-key]")) {
            if (card.dataset.dlViewKey === key) {
                card.toggleAttribute("data-dl-view-changed", changed);
            }
        }
    };

    const read = (key) => load(key).state;

    const write = (key, state) => {
        save(key, { ...load(key), state });
        mark(key);
    };

    // Forgets the state, and tells the chart to draw what it has saved. How the viewer looks at it is kept.
    const clear = (key) => {
        save(key, { ...load(key), state: null });
        mark(key);
        document.dispatchEvent(new CustomEvent(EVENT, { detail: { key } }));
    };

    const local = (key) => load(key).local;

    const setLocal = (key, change) => {
        save(key, { ...load(key), local: { ...load(key).local, ...change } });
    };

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

    return { read, write, clear, mark, local, setLocal, event: EVENT };
})();
