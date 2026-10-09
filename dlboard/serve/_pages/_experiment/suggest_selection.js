// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function suggestSelectionSummary(selected) {
    // The "Add selected" button's label and whether it is disabled, from the checkboxes ticked so
    // far. Pure browser-side: nothing here needs the server, and it runs on every tick.
    const count = selected ? selected.length : 0;
    return [`Add selected (${count})`, count === 0];
}
