// Clientside body for `_density.py`'s callback: the account menu's density control and the stored choice, kept as one.
// A change of the control is stored and stamped onto <html> (`data-dl-density`, which `_shell.css` reads); anything
// else (the page loading, the store reading what the browser kept) only shows the stored choice in the control, since
// `preload.js` stamped it before the page painted.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function dlboardDensity(stored, chosen) {
    const noUpdate = window.dash_clientside.no_update;
    const triggered = window.dash_clientside.callback_context.triggered;
    if (chosen && triggered.some((input) => input.prop_id.endsWith(".value"))) {
        document.documentElement.setAttribute("data-dl-density", chosen);
        return [chosen, noUpdate];
    }
    return [noUpdate, stored];
}
