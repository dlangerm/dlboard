// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function chartEditorStaleDraft(drafts, target, draftIds) {
    // A draft is only ever drawn for a chart the editor is open on. One that lands once the editor has closed (a draw
    // still in flight when Save was pressed) would sit in the card beside the saved chart it was a preview of, so it
    // is emptied here. Nothing is returned while the editor is open, or when every draft is already empty.
    if (target || !drafts.some((draft) => draft)) {
        return window.dash_clientside.no_update;
    }
    return draftIds.map(() => null);
}
