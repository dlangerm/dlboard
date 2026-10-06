// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function activeTabDisablesRename(value) {
    // Mirrors `_experiment_page_state.py`'s `_UNGROUPED_TAB_VALUE` -- the sentinel `dmc.Tabs` uses
    // in place of the real (empty-string) ungrouped tab name, since Mantine's `Tabs` rejects an
    // empty-string `value` outright. There's nothing to rename on that tab.
    return value === "ungrouped";
}
