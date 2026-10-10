// Registers a labelFormatter for dmc.BarChart's hover tooltip via DMC's "functions as props"
// mechanism (https://www.dash-mantine-components.com/functions-as-props). Recharts' default
// tooltip shows only the raw x-axis value (e.g. "5") with no indication of what it is; this
// prefixes it with the axis name (e.g. "step: 5"), reading that name off a `__x_axis_name__`
// marker each chart's data rows carry alongside the real plotted columns.
window.dashMantineFunctions = window.dashMantineFunctions || {};
window.dashMantineFunctions.barChartTooltipLabel = (label, payload) => {
    var row = payload && payload.length ? payload[0].payload : null;
    var axisName = row ? row.__x_axis_name__ : null;
    return axisName ? axisName + ": " + label : label;
};

// A value-axis tick: see `lineChartValueTick` in `line_chart_tooltip.js`.
window.dashMantineFunctions.barChartValueTick = (value) => {
    if (typeof value !== "number" || !Number.isFinite(value)) {
        return String(value);
    }
    var magnitude = Math.abs(value);
    if (magnitude !== 0 && (magnitude < 1e-3 || magnitude >= 1e5)) {
        return value.toExponential(1).replace("e+", "e");
    }
    return String(parseFloat(value.toPrecision(4)));
};
