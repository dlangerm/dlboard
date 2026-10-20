// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function chartEditorRemount(_draftIds, target) {
    // The cards were drawn again (a panel opened, a page of charts turned, another chart was deleted) and their drafts
    // came back empty. While the dock is editing something, say so, and the editor draws its draft again. With the
    // dock closed this answers nothing, and costs no round trip to the server.
    return target ? Date.now() : window.dash_clientside.no_update;
}
