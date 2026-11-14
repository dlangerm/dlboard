// Clientside body for the line charts' run toggle (`line_chart.py`): each plot holds the series of every run it was drawn
// with (`tooltipProps.allSeries`), and a run hidden or drawn in the browser (`RUN_TOGGLE_ID`) leaves its lines, legend
// and tooltip or comes back to them. A plot that holds none of the runs is left as it is.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function lineChartRunToggle(toggle, series, tooltipProps) {
    const noUpdate = window.dash_clientside.no_update;
    const asked = new Set(toggle.run_ids.map(String));
    const shown = tooltipProps.map((props, i) => {
        const showing = new Set(series[i].map((s) => s.name));
        const now = props.allSeries.filter((s) =>
            asked.has(s.name) ? toggle.action === "draw" : showing.has(s.name),
        );
        // Only ever runs it holds, of which it shows some: as many as it showed is what it showed.
        return now.length === series[i].length ? noUpdate : now;
    });
    return [
        shown,
        shown.map((now, i) => (now === noUpdate ? noUpdate : { ...tooltipProps[i], series: now })),
    ];
}
