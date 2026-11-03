// The button of a pin's notice that compares every run pinned in its colour (`_row_pins.py`). A notice is drawn by Mantine
// outside the layout Dash wires callbacks to, so a click on its button is heard here, and written to the run list's request
// store (named by the button) as the bar's own buttons write theirs.
(() => {
    document.addEventListener("click", (event) => {
        const button =
            event.target instanceof Element ? event.target.closest("[data-dl-compare-pins]") : null;
        if (button) {
            window.dash_clientside.set_props(button.dataset.dlRequestStore, {
                data: {
                    action: "compare_pins",
                    color: button.dataset.dlComparePins,
                    run_ids: [],
                    at: Date.now(),
                },
            });
        }
    });
})();
