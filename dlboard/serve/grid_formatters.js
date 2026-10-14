// Value formatters for the ag-grid tables (the table chart, the file list, the Runs table), registered
// through dash-ag-grid's "functions as props" mechanism: a column def calls one as
// `{"function": "<name>(params)"}`, with the cell's params. Unlike a JS expression string,
// these need no `dangerously_allow_code`, and they run as ordinary JS rather than dash-ag-grid's parser.
window.dashAgGridFunctions = window.dashAgGridFunctions || {};

// A numeric cell: whole numbers as they are, otherwise three decimals from 1 up and four significant
// digits below it (so a learning rate of 1e-4 is not `0.000`).
window.dashAgGridFunctions.dlboardNumber = (params) => {
    var value = params.value;
    if (value == null) {
        return "";
    }
    if (Number.isInteger(value)) {
        return value.toFixed(0);
    }
    return Math.abs(value) >= 1 ? value.toFixed(3) : Number(value.toPrecision(4)).toString();
};

// A file-list score: four significant digits.
window.dashAgGridFunctions.dlboardScore = (params) =>
    params.value == null ? "" : Number(params.value).toPrecision(4);
