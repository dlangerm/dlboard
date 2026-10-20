// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function chartViewStateSaved(chartId) {
    // The viewer's change is now the chart's own settings, so there is nothing left to keep: the card is drawn again from
    // what was saved.
    if (!chartId) {
        return window.dash_clientside.no_update;
    }
    window.dlChartViewState.clear(chartId);
    return chartId;
}
