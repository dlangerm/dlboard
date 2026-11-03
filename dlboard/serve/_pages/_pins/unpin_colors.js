// What the Unpin menu of a run list's bar offers: the colours the viewer holds on at least one selected run (a row's `_pins`
// has a `mine` flag), and a note when there are none. The menu's items are drawn only while it is open, in a portal, so it is
// said in CSS variables on the page, which each item reads as its `display` (hidden unless one says otherwise).
// Called by the scripts that know the selection or a pin changed (`selection_bar.js`, `apply_row_updates.js`) with the selected
// rows. Until ag-grid reports rows (the table is seeded with its selection as `{ids: [...]}`) their pins are not known, and
// nothing is offered.
window.dlSetUnpinColors = (selectedRows) => {
    const style = document.documentElement.style;
    for (const name of [...style].filter((name) => name.startsWith("--dl-unpin-"))) {
        style.removeProperty(name);
    }
    if (!Array.isArray(selectedRows)) {
        return;
    }
    const held = new Set(
        selectedRows.flatMap((row) => (row._pins || []).filter((pin) => pin.mine).map((pin) => pin.color)),
    );
    for (const color of held) {
        style.setProperty(`--dl-unpin-${color}`, "flex");
    }
    if (held.size === 0) {
        style.setProperty("--dl-unpin-none", "block");
    }
};
