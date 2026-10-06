// Opens the jump palette (`_jump.py`) on Ctrl/Cmd+K anywhere, on "/" when not already typing, or on
// a click of the header's trigger button -- all without a server round trip. The modal's id comes
// from the trigger's `data-dl-jump-modal` attribute, so it's never duplicated here.
(() => {
    const trigger = () => document.querySelector("[data-dl-jump-modal]");
    const open = () => {
        const button = trigger();
        if (button) {
            window.dash_clientside.set_props(button.dataset.dlJumpModal, { opened: true });
        }
    };
    const isTyping = (target) =>
        target instanceof HTMLElement &&
        (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));

    document.addEventListener("keydown", (event) => {
        const combo = event.key.toLowerCase() === "k" && (event.ctrlKey || event.metaKey);
        const slash = event.key === "/" && !event.ctrlKey && !event.metaKey && !isTyping(event.target);
        if (combo || slash) {
            event.preventDefault();
            open();
        }
    });
    document.addEventListener("click", (event) => {
        if (event.target instanceof Element && event.target.closest("[data-dl-jump-modal]")) {
            open();
        }
    });
})();
