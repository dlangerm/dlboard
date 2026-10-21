// The cell renderer of a table chart's artifact links (`ARTIFACT_RENDERER` in `table_chart.py`), registered through
// dash-ag-grid's component functions. A cell holds the link as text (what the column sorts and filters by) and, in the
// field beside it, what to draw it from: an image is a thumbnail linking to the full picture with the layers logged beside
// it drawn over it (`window.dlLayeredPicture`, as an image chart draws them), and any other file is a link named by its file
// name. Every element is built by React from text, never by parsing markup, so a file name is only text.
window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {};

(() => {
    const h = React.createElement;
    const AS_LOGGED = () => ({ hidden: false, opacity: 1 });

    const Thumbnail = ({ link }) => {
        const picture = React.useRef(null);
        const layers = React.useRef(null);
        const wanted = JSON.stringify(link.layers);
        React.useEffect(() => {
            // Drawn once the picture is in the cell, and again only if the layers or the picture change.
            window.dlLayeredPicture.show(
                layers.current,
                picture.current,
                link.href,
                JSON.parse(wanted),
                AS_LOGGED,
            );
        }, [link.href, wanted]);
        return h(
            "span",
            { className: "dl-layered-picture" },
            h("img", { ref: picture, src: link.href, alt: link.name }),
            h("div", { ref: layers, className: "dl-image-series-overlay" }),
        );
    };

    window.dashAgGridComponentFunctions.DlboardArtifactLink = (props) => {
        const link = props.data?.[`${props.colDef.field}__link`];
        if (!link) {
            return null;
        }
        return h(
            "a",
            { href: link.href, target: "_blank", rel: "noopener noreferrer" },
            link.image ? h(Thumbnail, { link }) : link.name,
        );
    };
})();
