// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function panelGridColumnsRefill(columns, _blurs, pageJson) {
    // A field left cleared or half-typed once it has lost focus shows its panel's saved column count again: only a whole
    // number is ever applied (`panel_grid_columns.js`) or saved. Read from the page, as the panel's grid may not be on it.
    // On a change as well as a blur, as the field reports a clearing a moment after it is made, which may be after it lost
    // focus.
    const { panel, type } = window.dash_clientside.callback_context.triggered_id;
    // A pattern-matching id is the element's DOM id as JSON with its keys sorted.
    const focused = document.activeElement?.id === JSON.stringify({ panel, type });
    if (Number.isInteger(columns) || focused) {
        return window.dash_clientside.no_update;
    }
    const saved = JSON.parse(pageJson).panels.find((p) => p.name === panel)?.grid_columns;
    return saved ?? window.dash_clientside.no_update;
}
