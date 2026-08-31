// Sets the add/edit-chart modal's submit button into its spinner state the instant it's clicked.
//
// `add_chart` (`_panel_controls.py`) can take a couple of seconds -- it re-renders the whole panel
// tree -- and without this the button just sits there looking unresponsive for that whole stretch.
// This can't be a Dash `clientside_callback` targeting the button's own `loading` prop: that
// callback's only Input would be the button's own `n_clicks`, which is exactly what `add_chart`
// itself already listens to for that same output, and Dash's duplicate-output check rejects two
// separate callbacks sharing both an identical trigger and an identical target (even with
// `allow_duplicate=True` on both) since it can't order them. `set_props` sidesteps that: it's an
// imperative update outside Dash's declared callback graph, mirroring
// `_experiment_page_dragdrop.js`'s use of the same escape hatch for its own drop handling.
document.addEventListener("click", (event) => {
    if (event.target.closest("#add-chart-submit")) {
        window.dash_clientside.set_props("add-chart-submit", { loading: true });
    }
});
