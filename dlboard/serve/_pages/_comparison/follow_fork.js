// Clientside body for `_runs_panel.py`'s fork follower: when an edit has branched the comparison on screen into one of the
// viewer's own (the page the browser holds is another's than the one shown), go to it. The address is the same page's with
// the new id and the rest of it as it was (what the viewer hid, which page of panels), so a link to a comparison stays a
// link to a comparison. Pushed onto the history and announced to Dash's page router, as an in-app link is (see
// `view_navigate.js`), which draws the page again as the copy: a hard load would be undone by the page's own address
// bar mirror (`paging_side_effects.js`), which writes the address as the page last knew it.
// Returns `no_update` always -- side-effect only.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function followFork(pageJson, shownId) {
    // Only a comparison page has a comparison to follow; the page store is on an experiment page too.
    const id = pageJson && shownId !== null ? JSON.parse(pageJson).id : shownId;
    if (shownId !== null && id !== shownId) {
        const url = new URL(window.location.href);
        url.pathname = url.pathname.replace(/(\/comparison\/)\d+/, `$1${id}`);
        window.history.pushState({}, "", url);
        window.dispatchEvent(new CustomEvent("_dashprivate_pushstate"));
    }
    return window.dash_clientside.no_update;
}
