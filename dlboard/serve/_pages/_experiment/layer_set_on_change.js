// Clientside body for `_chart_settings_layers.py`: a setting changed in the chart settings form is one to set, so its
// box is ticked. A value changed with the box left clear would otherwise be quietly left out.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function layerSetOnChange() {
    return true;
}
