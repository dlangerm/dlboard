// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function chartEditorCancel(clicks, draftIds) {
    if (!clicks) {
        return window.dash_clientside.no_update;
    }
    // Close the dock, forget what it was editing, and empty every card's draft: the charts underneath were never
    // touched, so they are what is shown again, exactly as they were.
    return [true, null, draftIds.map(() => null), null, ""];
}
