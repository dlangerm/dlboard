// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function liveLastFetchLabel(_nIntervals, lastFetchAtIso) {
    // No timestamp yet (page still loading) -- nothing to show.
    if (!lastFetchAtIso) {
        return window.dash_clientside.no_update;
    }
    const elapsedSeconds = Math.max(0, Math.round((Date.now() - new Date(lastFetchAtIso).getTime()) / 1000));
    if (elapsedSeconds < 2) {
        return "Fetched just now";
    }
    if (elapsedSeconds < 60) {
        return `Fetched ${elapsedSeconds}s ago`;
    }
    const elapsedMinutes = Math.round(elapsedSeconds / 60);
    return `Fetched ${elapsedMinutes}m ago`;
}
