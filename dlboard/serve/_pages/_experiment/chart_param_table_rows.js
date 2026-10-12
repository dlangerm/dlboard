// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function chartParamTableRows(_add, _remove) {
    const noUpdate = window.dash_clientside.no_update;
    const clicked = window.dash_clientside.callback_context.triggered_id;
    if (!clicked) {
        return [noUpdate, noUpdate];
    }
    // The grid takes an added row as a transaction, and deletes its checked rows when told to.
    return clicked.type === "chart-param-table-add" ? [{ add: [{}] }, noUpdate] : [noUpdate, true];
}
