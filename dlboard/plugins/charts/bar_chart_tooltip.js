// Registers a labelFormatter for dmc.BarChart's hover tooltip via DMC's "functions as props"
// mechanism (https://www.dash-mantine-components.com/functions-as-props). Recharts' default
// tooltip shows only the raw x-axis value (e.g. "5") with no indication of what it is; this
// prefixes it with the axis name (e.g. "step: 5"), reading that name off a `__x_axis_name__`
// marker each chart's data rows carry alongside the real plotted columns.
window.dashMantineFunctions = window.dashMantineFunctions || {};
// A bar of one run over a metric x-axis also carries its own x value, `__x_detail__` (e.g. "lr: 0.001").
window.dashMantineFunctions.barChartTooltipLabel = (label, payload) => {
    var row = payload && payload.length ? payload[0].payload : null;
    var axisName = row ? row.__x_axis_name__ : null;
    var named = axisName ? axisName + ": " + label : label;
    return row && row.__x_detail__ ? named + " (" + row.__x_detail__ + ")" : named;
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
