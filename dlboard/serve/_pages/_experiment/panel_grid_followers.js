// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function panelGridFollowers(columns, ids) {
    const changed = window.dash_clientside.callback_context.triggered_id;
    const count = columns[ids.findIndex((id) => id.panel === changed?.panel)];
    // A pattern-matching id is the element's DOM id as JSON with its keys sorted.
    const grid = document.getElementById(JSON.stringify({ panel: changed?.panel, type: "panel-grid" }));
    if (!grid || !Number.isInteger(count)) {
        return;
    }
    // A chart that lays out a grid of its own by the panel's column count (the image series' runs) marks
    // it, and follows the count the instant it changes, like the panel's own grid, without a rebuild.
    for (const follower of grid.querySelectorAll("[data-dl-follow-panel-columns]")) {
        follower.style.setProperty("--sg-cols", Math.min(count, follower.childElementCount));
    }
}
