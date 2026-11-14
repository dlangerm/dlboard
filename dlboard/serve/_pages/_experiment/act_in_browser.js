// Clientside body in front of the experiment page's run actions (`_runs_table.py`'s `act_on_runs`): a run drawn or hidden
// is shown or hidden at once by every chart on screen that can (`RUN_TOGGLE_ID`), and when all of them can, and hold
// every run drawn, the server is only asked to save the choice and update the run list. Anything else (another action,
// a chart that cannot, a run some chart never drew, a link to one run) goes to the server as it is.
//
// Each chart's fingerprint says which of the runs it holds it hides (`data-dl-hiding`): the server draws such a chart
// again whenever it draws, as its fingerprint no longer says what it shows, and a chart it draws again hides none.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function actInBrowser(request, canHide, heldRuns, hidingRuns) {
    const noUpdate = window.dash_clientside.no_update;
    const asIs = hidingRuns.map(() => noUpdate);
    if (!request) {
        return [noUpdate, asIs, noUpdate];
    }
    const forward = (inBrowser) => ({ request, in_browser: inBrowser });
    // A link to one run (`?run=`) draws that run alone: leaving it draws the view again, which only the server can.
    const toggle = request.action === "draw" || request.action === "hide";
    if (!toggle || new URLSearchParams(window.location.search).has("run")) {
        return [noUpdate, asIs, forward(false)];
    }
    const asked = request.run_ids;
    const charts = heldRuns.map((held, i) => ({
        can: canHide[i] === "1",
        held: new Set(JSON.parse(held)),
        hiding: new Set(JSON.parse(hidingRuns[i])),
    }));
    const hiding = charts.map((chart) => {
        if (!chart.can) {
            return noUpdate;
        }
        const now = new Set(chart.hiding);
        for (const run of asked) {
            if (request.action === "draw") {
                now.delete(run);
            } else if (chart.held.has(run)) {
                now.add(run);
            }
        }
        return JSON.stringify([...now].sort((a, b) => a - b));
    });
    const inBrowser = charts.every(
        (chart) => chart.can && (request.action === "hide" || asked.every((run) => chart.held.has(run))),
    );
    return [{ action: request.action, run_ids: asked }, hiding, forward(inBrowser)];
}
