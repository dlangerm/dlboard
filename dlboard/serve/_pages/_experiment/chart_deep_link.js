// Chart deep links, entirely client-side:
// - A chart's "copy link" button (`data-dl-copy-chart-link`) copies this page's URL with
//   `?chart=<chart id>` (keeping any `?view=`), built from the browser's own location so it's right
//   behind any proxy. A `data-dl-copy-page-link` button (the compare modal's) copies the URL as-is.
// - Landing on a URL with `?chart=` (the server already opened that chart's panel and tab) scrolls
//   the chart into view and briefly highlights it, once Dash has rendered it.
(() => {
    const FOCUS_CLASS = "dl-chart-focus";
    const FOCUS_MS = 2500;
    const WAIT_FOR_CHART_MS = 15000;

    // `navigator.clipboard` only exists on secure origins; a self-hosted server on plain HTTP over a
    // LAN isn't one, so fall back to the legacy copy command there.
    const copy = (text) => {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text);
        }
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
        return Promise.resolve();
    };

    document.addEventListener("click", (event) => {
        const button =
            event.target instanceof Element &&
            event.target.closest("[data-dl-copy-chart-link], [data-dl-copy-page-link]");
        if (!button) {
            return;
        }
        const url = new URL(window.location.href);
        url.hash = "";
        if (button.dataset.dlCopyChartLink !== undefined) {
            url.searchParams.set("chart", button.dataset.dlCopyChartLink);
        }
        copy(url.toString()).then(() => {
            button.setAttribute("data-dl-copied", "");
            setTimeout(() => button.removeAttribute("data-dl-copied"), FOCUS_MS);
        });
    });

    const focusLinkedChart = () => {
        const chartId = new URLSearchParams(window.location.search).get("chart");
        if (!chartId) {
            return;
        }
        const find = () => document.querySelector(`[data-chart-id="${CSS.escape(chartId)}"]`);
        const focus = (chart) => {
            chart.scrollIntoView({ block: "center" });
            chart.classList.add(FOCUS_CLASS);
            setTimeout(() => chart.classList.remove(FOCUS_CLASS), FOCUS_MS);
        };
        const existing = find();
        if (existing) {
            focus(existing);
            return;
        }
        const observer = new MutationObserver(() => {
            const chart = find();
            if (chart) {
                observer.disconnect();
                focus(chart);
            }
        });
        observer.observe(document.body, { childList: true, subtree: true });
        setTimeout(() => observer.disconnect(), WAIT_FOR_CHART_MS);
    };
    focusLinkedChart();
    window.addEventListener("popstate", focusLinkedChart);
    window.addEventListener("_dashprivate_pushstate", focusLinkedChart);
})();
