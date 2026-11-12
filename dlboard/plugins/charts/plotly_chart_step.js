// The step a Plotly chart shows is its slider's (`plotly_chart.py`): kept apart from the slider, since a chart that logged
// one step has no slider to read it from.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function plotlyChartStep(value) {
    return value ?? window.dash_clientside.no_update;
}
