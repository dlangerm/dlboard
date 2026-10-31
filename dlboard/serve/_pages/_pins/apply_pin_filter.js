// Clientside body for the pin chips above a run list: show only the runs pinned in any of the chips' colours (all of them with
// none), by the grid's own filter on its pins column, so it composes with the filters on its other columns and is cleared with
// them. Returns `no_update` always -- side-effect only.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function applyPinFilter(colors, gridId) {
    const api = window.dash_ag_grid.getApi(gridId);
    if (!api) {
        return window.dash_clientside.no_update;
    }
    const model = { ...api.getFilterModel() };
    if (colors?.length) {
        model._pins = {
            filterType: "text",
            operator: "OR",
            conditions: colors.map((color) => ({ filterType: "text", type: "contains", filter: color })),
        };
    } else {
        delete model._pins;
    }
    api.setFilterModel(model);
    return window.dash_clientside.no_update;
}
