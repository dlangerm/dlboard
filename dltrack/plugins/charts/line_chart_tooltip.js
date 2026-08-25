// Registers a labelFormatter for dmc.LineChart's hover tooltip via DMC's "functions as props"
// mechanism (https://www.dash-mantine-components.com/functions-as-props). Recharts' default
// tooltip shows only the raw x-axis value (e.g. "5") with no indication of what it is; this
// prefixes it with the axis name (e.g. "step: 5"), reading that name off a `__x_axis_name__`
// marker each chart's data rows carry alongside the real plotted columns.
window.dashMantineFunctions = window.dashMantineFunctions || {};
window.dashMantineFunctions.lineChartTooltipLabel = (label, payload) => {
    var row = payload && payload.length ? payload[0].payload : null;
    var axisName = row ? row.__x_axis_name__ : null;
    return axisName ? axisName + ": " + label : label;
};
