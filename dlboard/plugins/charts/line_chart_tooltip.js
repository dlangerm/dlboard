// Registers dmc.LineChart's hover tooltip and its x-value formatters via DMC's "functions as props"
// mechanism (https://www.dash-mantine-components.com/functions-as-props).
//
// Recharts' XAxis only supports "number"/"category" scales -- no real time/date scale -- so a
// "date" x-axis (see `LineChartSettings.x_axis_type` in `line_chart.py`) is rendered as a plain
// numeric one, epoch-milliseconds-valued, and a "time" x-axis as a plain numeric one valued in
// elapsed seconds. The formatters below turn those raw numbers back into something readable, both
// on the axis ticks and in the tooltip; `line_chart.py` passes the matching one as the tooltip's
// `labelFormatter`, which `lineChartTooltip` then uses for every x-value it shows.
window.dashMantineFunctions = window.dashMantineFunctions || {};

// More rows than this and the tooltip outgrows the chart it describes; the rest are summarized.
var DLBOARD_TOOLTIP_MAX_ROWS = 10;

function dlboardFormatDuration(totalSeconds) {
    var seconds = Math.round(totalSeconds);
    var sign = seconds < 0 ? "-" : "";
    seconds = Math.abs(seconds);
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var secs = seconds % 60;
    var pad = (n) => String(n).padStart(2, "0");
    return hours > 0 ? `${sign}${hours}:${pad(minutes)}:${pad(secs)}` : `${sign}${minutes}:${pad(secs)}`;
}

// Five significant digits reads well for losses and accuracies alike; very large or very small
// magnitudes switch to exponent form rather than growing a long run of zeros.
function dlboardFormatValue(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
        return String(value);
    }
    var magnitude = Math.abs(value);
    if (magnitude !== 0 && (magnitude < 1e-4 || magnitude >= 1e7)) {
        return value.toExponential(3);
    }
    return value.toLocaleString(undefined, { maximumSignificantDigits: 5 });
}

// The custom `content` of the tooltip: the hovered x-value under its axis name, then one row per
// run, highest value first, under the chart's own title (the full metric name -- every value below
// is a value of it). `line_chart.py` passes the chart's `series`, `xAxisName` and `chartTitle`
// alongside the usual tooltip props. Every run shows its *nearest* logged value ("<run_id>__y", see
// `_nearest_fill_pivot` in `line_chart.py`), not just runs that logged exactly at the hovered x,
// and "<run_id>__x" says where that value came from -- shown beside the run's name whenever it
// differs, so a filled-in value never reads as if it were logged right at the cursor. A run with no
// value here (the cursor is outside the range it logged over) is left out entirely.
window.dashMantineFunctions.lineChartTooltip = (props) => {
    var h = window.React.createElement;
    var row = props.active && props.payload && props.payload.length ? props.payload[0].payload : null;
    if (!row) {
        return null;
    }
    var axisName = props.xAxisName;
    var hoveredX = row[axisName];
    var formatX = (x) => (props.labelFormatter ? props.labelFormatter(x, props.payload) : String(x));
    var runs = (props.series || [])
        .map((s) => ({ series: s, value: row[`${s.name}__y`], sourceX: row[`${s.name}__x`] }))
        .filter((run) => run.value != null)
        .sort((a, b) => b.value - a.value);
    var shown = runs.slice(0, DLBOARD_TOOLTIP_MAX_ROWS);
    var hidden = runs.length - shown.length;

    return h(
        "div",
        { className: "dl-chart-tooltip" },
        props.chartTitle ? h("div", { className: "dl-chart-tooltip-title" }, props.chartTitle) : null,
        h(
            "div",
            { className: "dl-chart-tooltip-header" },
            h("span", { className: "dl-chart-tooltip-axis" }, axisName),
            h("span", { className: "dl-chart-tooltip-x" }, formatX(hoveredX)),
        ),
        shown.map((run) =>
            h(
                "div",
                { className: "dl-chart-tooltip-row", key: run.series.name },
                h("span", { className: "dl-chart-tooltip-swatch", style: { background: run.series.color } }),
                h(
                    "span",
                    { className: "dl-chart-tooltip-label" },
                    h("span", { className: "dl-chart-tooltip-name" }, run.series.label),
                    run.sourceX !== hoveredX
                        ? h("span", { className: "dl-chart-tooltip-source" }, `@ ${formatX(run.sourceX)}`)
                        : null,
                ),
                h("span", { className: "dl-chart-tooltip-value" }, dlboardFormatValue(run.value)),
            ),
        ),
        hidden > 0 ? h("div", { className: "dl-chart-tooltip-more" }, `+${hidden} more`) : null,
    );
};

window.dashMantineFunctions.lineChartTooltipLabel = (label) =>
    typeof label === "number" ? label.toLocaleString() : String(label);

window.dashMantineFunctions.lineChartTooltipLabelDate = (label) => new Date(label).toLocaleString();

window.dashMantineFunctions.lineChartTooltipLabelTime = (label) => dlboardFormatDuration(label);

window.dashMantineFunctions.lineChartDateTick = (value) => new Date(value).toLocaleString();

window.dashMantineFunctions.lineChartTimeTick = (value) => dlboardFormatDuration(value);

// A y-axis tick: four significant digits at most (float noise like 0.30000000000000004 is what
// pushed ticks into the rotated axis title), exponent form for the very small and very large.
window.dashMantineFunctions.lineChartValueTick = (value) => {
    if (typeof value !== "number" || !Number.isFinite(value)) {
        return String(value);
    }
    var magnitude = Math.abs(value);
    if (magnitude !== 0 && (magnitude < 1e-3 || magnitude >= 1e5)) {
        return value.toExponential(1).replace("e+", "e");
    }
    return String(parseFloat(value.toPrecision(4)));
};
