// Clientside body for a run list's row updates (`_rows.py`): set the fields of the rows a change touched, in place in the grid that
// holds them, so the list is not drawn again and keeps its scroll, sort, selection and filters. Then the grid's filters are run
// again, since a row that no longer matches one (a run unpinned while the pin filter shows only pinned runs) must leave the view.
// Returns `no_update` always -- side-effect only, the same pattern as `sync_view_url.js`.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function applyRowUpdates(update) {
    const api = update ? window.dash_ag_grid.getApi(update.grid) : null;
    if (!api || !update?.rows?.length) {
        return window.dash_clientside.no_update;
    }
    for (const { run_id, values } of update.rows) {
        const node = api.getRowNode(String(run_id));
        for (const [field, value] of Object.entries(values)) {
            node?.setDataValue(field, value);
        }
    }
    api.onFilterChanged();
    return window.dash_clientside.no_update;
}
