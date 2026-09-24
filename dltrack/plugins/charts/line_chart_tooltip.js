// Registers dmc.LineChart's hover-tooltip labelFormatter and XAxis tickFormatter functions via
// DMC's "functions as props" mechanism (https://www.dash-mantine-components.com/functions-as-props).
//
// Recharts' XAxis only supports "number"/"category" scales -- no real time/date scale -- so a
// "date" x-axis (see `LineChartSettings.x_axis_type` in `line_chart.py`) is rendered as a plain
// numeric one, epoch-milliseconds-valued, and a "time" x-axis as a plain numeric one valued in
// elapsed seconds. These formatters turn those raw numbers back into something readable, both on
// the axis ticks and in the tooltip label (which also gets the axis name prefixed, e.g. "step: 5",
// reading it off a `__x_axis_name__` marker each chart's data rows carry alongside the real
// plotted columns).
window.dashMantineFunctions = window.dashMantineFunctions || {};

function dltrackFormatDuration(totalSeconds) {
    var seconds = Math.round(totalSeconds);
    var sign = seconds < 0 ? "-" : "";
    seconds = Math.abs(seconds);
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var secs = seconds % 60;
    var pad = (n) => String(n).padStart(2, "0");
    return hours > 0 ? `${sign}${hours}:${pad(minutes)}:${pad(secs)}` : `${sign}${minutes}:${pad(secs)}`;
}

function dltrackAxisNamePrefix(payload) {
    var row = payload && payload.length ? payload[0].payload : null;
    var axisName = row ? row.__x_axis_name__ : null;
    return axisName ? axisName + ": " : "";
}

// A hovered x is only ever exact for the run(s) that actually logged there -- every other run's
// shown value is its *nearest* logged value instead (see `_nearest_fill_pivot` in `line_chart.py`),
// which can be from a different x than the one in the tooltip's shared label. Each row carries a
// "<run_id>__x" companion field recording where a given series' value actually came from; this
// notes it for any series whose value didn't come from the hovered x, so a filled-in value never
// reads as if it were logged exactly where the cursor happens to be. Folded into the *label*
// (Mantine's `ChartTooltip` doesn't forward a per-item `formatter`/`valueFormatter` the way it
// forwards `labelFormatter` -- confirmed by hand, not documented -- so per-item annotation isn't
// available; this is the one hook that reliably reaches the rendered tooltip).
function dltrackSourceAnnotations(payload) {
    var axisName =
        payload && payload.length && payload[0].payload ? payload[0].payload.__x_axis_name__ : null;
    if (!axisName) {
        return "";
    }
    var notes = (payload || [])
        .map((item) => {
            var row = item.payload;
            if (!row) {
                return null;
            }
            var sourceX = row[item.dataKey + "__x"];
            var hoveredX = row[axisName];
            return sourceX != null && hoveredX != null && sourceX !== hoveredX
                ? `${item.name}@${axisName}=${sourceX}`
                : null;
        })
        .filter((note) => note != null);
    return notes.length ? ` (${notes.join(", ")})` : "";
}

window.dashMantineFunctions.lineChartTooltipLabel = (label, payload) => {
    return dltrackAxisNamePrefix(payload) + label + dltrackSourceAnnotations(payload);
};

window.dashMantineFunctions.lineChartTooltipLabelDate = (label, payload) => {
    return (
        dltrackAxisNamePrefix(payload) + new Date(label).toLocaleString() + dltrackSourceAnnotations(payload)
    );
};

window.dashMantineFunctions.lineChartTooltipLabelTime = (label, payload) => {
    return dltrackAxisNamePrefix(payload) + dltrackFormatDuration(label) + dltrackSourceAnnotations(payload);
};

window.dashMantineFunctions.lineChartDateTick = (value) => new Date(value).toLocaleString();

window.dashMantineFunctions.lineChartTimeTick = (value) => dltrackFormatDuration(value);
