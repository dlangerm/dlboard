// Clientside body for `_jump.py`'s results: Mantine takes a search's first option as the search changes, but the server's
// results for it arrive after that keystroke, with none taken. Take the first of them as they arrive, so Enter goes to it.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function jumpSelectFirst(data, inputId) {
    requestAnimationFrame(() => {
        const input = document.getElementById(inputId);
        const options = document.getElementById(input?.getAttribute("aria-controls") ?? "");
        if (!data?.length || document.activeElement !== input || !options) {
            return;
        }
        if (!options.querySelector("[data-combobox-selected]")) {
            // Nothing taken: the combobox's own ArrowDown takes the first option, as it does for someone pressing it.
            input.dispatchEvent(
                new KeyboardEvent("keydown", { key: "ArrowDown", code: "ArrowDown", bubbles: true }),
            );
        }
    });
}
