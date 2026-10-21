// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function chartParamTableRows(_add, _remove) {
    const noUpdate = window.dash_clientside.no_update;
    const clicked = window.dash_clientside.callback_context.triggered_id;
    if (!clicked) {
        return [noUpdate, noUpdate];
    }
    // A pattern-matching id is the component's id string as JSON with its keys sorted.
    const grid = { field: clicked.field, type: "chart-param-table" };
    const api = window.dash_ag_grid.getApi(JSON.stringify(grid, Object.keys(grid).sort()));
    if (!api) {
        return [noUpdate, noUpdate];
    }
    if (clicked.type !== "chart-param-table-add") {
        // A row the chart supplies (drawn whether or not it is saved) stays, however it came to be checked.
        for (const node of api.getSelectedNodes()) {
            if (node.data.fixed) {
                node.setSelected(false);
            }
        }
        // The grid deletes its checked rows when told to.
        return [noUpdate, true];
    }
    // The new row's name is what is wanted next, so its cell opens for it at once.
    const [added] = api.applyTransaction({ add: [{}] }).add;
    api.startEditingCell({ rowIndex: added.rowIndex, colKey: "key" });
    return [noUpdate, noUpdate];
}
