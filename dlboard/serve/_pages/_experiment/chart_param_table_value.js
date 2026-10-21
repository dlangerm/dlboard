// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function chartParamTableValue(_rowsChanged, _cellEdited) {
    // A pattern-matching id is the component's id string as JSON with its keys sorted.
    const grid = window.dash_clientside.callback_context.triggered_id;
    const api = window.dash_ag_grid.getApi(JSON.stringify(grid, Object.keys(grid).sort()));
    if (!api) {
        return window.dash_clientside.no_update;
    }
    // The table's rows as the mapping the setting holds: a row's key names its entry, and a cell left
    // blank is left out of it. Read from the grid itself, as `virtualRowData` doesn't follow a cell
    // being edited. `label` and `fixed` are the grid's own (how a row the chart supplies is shown and held in
    // place), not cells of the setting.
    const entries = {};
    api.forEachNode(({ data: { key, label: _label, fixed: _fixed, ...cells } }) => {
        if (key) {
            entries[key] = Object.fromEntries(
                Object.entries(cells).filter(
                    ([, cell]) => cell !== null && cell !== undefined && cell !== "",
                ),
            );
        }
    });
    return entries;
}
