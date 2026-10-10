// Clientside body for `_experiment_page_state.py`'s one paging side-effect callback. Whenever the
// serialized `Paging` (`data`) changes it does two things to the browser, neither of which has a Dash
// prop to write:
//
// 1. Mirror which page of panels and charts is showing into the address bar, so the link can be shared
//    or bookmarked (`Paging` is the server-side reader of these params, via `layout()`). A plain
//    `replaceState` -- not `pushState` -- so paging doesn't flood the back button, and it doesn't re-run
//    the page's `layout()`: the page already shows this state. `data.query` maps every param it manages
//    to its value, or "" when that is the default and belongs out of the URL. Other params (`view`,
//    `chart`, `compare*`) are left alone.
//
// 2. The pagers sit under what they page, so after a flip you are looking at the bottom of the new
//    page: when the page of panels changed, scroll back to the top of the panel area; when a panel's
//    chart page did, to the top of that panel's charts -- but only if that top is off-screen above, so a
//    flip made with the top in view, or a re-render that leaves the page where it was (a rebuild
//    re-creates the store), never moves the page. The page seen last time is kept on `window`.
//
// One callback, not two: Dash derives a duplicate output's id from the callback's inputs and outputs,
// so two callbacks of the same shape collide. Returns `no_update` always -- side-effect only, the same
// pattern as `sync_compare_url.js`.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function pagingSideEffects(data) {
    const paging = JSON.parse(data);

    const url = new URL(window.location.href);
    for (const [name, value] of Object.entries(paging.query)) {
        if (value) {
            url.searchParams.set(name, value);
        } else {
            url.searchParams.delete(name);
        }
    }
    if (url.href !== window.location.href) {
        window.history.replaceState({}, "", url);
    }

    const previous = window.dlboardLastPaging || null;
    window.dlboardLastPaging = paging;
    if (previous !== null) {
        let target = null;
        if (paging.panel_page !== previous.panel_page) {
            target = document.getElementById("panel-area");
        } else {
            for (const [name, chartPaging] of Object.entries(paging.charts)) {
                const before = previous.charts[name] ? previous.charts[name].page : 1;
                if (chartPaging.page !== before) {
                    target = document.querySelector(`[data-panel-name="${CSS.escape(name)}"]`);
                }
            }
        }
        if (target !== null && target.getBoundingClientRect().top < 0) {
            target.scrollIntoView({ block: "start" });
        }
    }
    return window.dash_clientside.no_update;
}
