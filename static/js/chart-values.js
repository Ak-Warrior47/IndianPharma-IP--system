/* Exact numbers on every chart in the app:
   - bar charts: the value on top of each bar (or at the end of a sideways bar)
   - pie / doughnut charts: the value inside each slice big enough to hold it,
     and the legend beside the pie reads "Name — 12 (40%)".
   Works whether Chart.js is already on the page or loads later. */
(function () {
  "use strict";
  function fmt(v) {
    var n = Number(v);
    if (!isFinite(n)) return String(v);
    return Math.round(n) === n ? String(n) : n.toFixed(1);
  }
  function sum(a) { return (a || []).reduce(function (s, v) { return s + (Number(v) || 0); }, 0); }
  function round(t) { return t === "doughnut" || t === "pie"; }

  function setup(C) {
    if (!C || C.__ipValues) return;
    C.__ipValues = true;

    C.register({
      id: "ipValues",
      beforeUpdate: function (chart) {
        // change the chart's own settings (not the live resolved options,
        // which are a Proxy and loop if written to)
        var t = chart.config.type, raw = chart.config.options;
        if (!raw) return;
        if (round(t)) {
          raw.plugins = raw.plugins || {};
          raw.plugins.legend = raw.plugins.legend || {};
          // beside the pie when there is room, under it on a narrow phone
          raw.plugins.legend.position = (chart.width || 0) >= 340 ? "right" : "bottom";
        } else if (t === "bar" && raw.indexAxis !== "y") {
          raw.layout = raw.layout || {};
          var p = raw.layout.padding;
          if (typeof p !== "object" || p === null) raw.layout.padding = { top: 18 };
          else if ((p.top || 0) < 18) p.top = 18;
        }
      },
      afterDatasetsDraw: function (chart) {
        var t = chart.config.type, ctx = chart.ctx;
        if (t !== "bar" && !round(t)) return;
        var dark = document.documentElement.getAttribute("data-theme") === "dark";
        ctx.save();
        ctx.font = "700 11px system-ui, -apple-system, Segoe UI, Roboto, sans-serif";
        chart.data.datasets.forEach(function (ds, di) {
          var meta = chart.getDatasetMeta(di);
          if (!meta || meta.hidden) return;
          var total = sum(ds.data);
          meta.data.forEach(function (el, i) {
            if (!el || (el.hidden)) return;
            var v = ds.data[i];
            if (v == null || isNaN(Number(v))) return;
            if (t === "bar") {
              if (!Number(v)) return;              // no label on an empty bar
              ctx.fillStyle = dark ? "#e5e7eb" : "#111827";
              if (chart.options.indexAxis === "y") {
                ctx.textAlign = "left"; ctx.textBaseline = "middle";
                ctx.fillText(fmt(v), el.x + 4, el.y);
              } else {
                ctx.textAlign = "center"; ctx.textBaseline = "bottom";
                ctx.fillText(fmt(v), el.x, el.y - 3);
              }
            } else {
              if (!total || !Number(v) || Number(v) / total < 0.07) return;
              var p = el.tooltipPosition();
              ctx.font = "800 13px system-ui, -apple-system, Segoe UI, Roboto, sans-serif";
              ctx.textAlign = "center"; ctx.textBaseline = "middle";
              ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,.35)";
              ctx.strokeText(fmt(v), p.x, p.y);
              ctx.fillStyle = "#fff";
              ctx.fillText(fmt(v), p.x, p.y);
            }
          });
        });
        ctx.restore();
      }
    });

    ["doughnut", "pie"].forEach(function (k) {
      var o = C.overrides && C.overrides[k];
      if (!o || !o.plugins || !o.plugins.legend) return;
      var orig = o.plugins.legend.labels.generateLabels;
      if (!orig || orig.__ip) return;      // pie and doughnut can share one function: wrap it once
      var wrapped = o.plugins.legend.labels.generateLabels = function (chart) {
        var items = orig.call(this, chart), ds = chart.data.datasets[0] || { data: [] }, tot = sum(ds.data);
        items.forEach(function (it) {
          var v = ds.data[it.index];
          it.text = it.text + " — " + fmt(v) + (tot ? " (" + Math.round(100 * Number(v) / tot) + "%)" : "");
        });
        return items;
      };
      wrapped.__ip = true;
    });
  }

  window.ipChartValues = function () { setup(window.Chart); };
  if (window.Chart) { setup(window.Chart); return; }
  var tries = 0, t = setInterval(function () {
    if (window.Chart) { clearInterval(t); setup(window.Chart); }
    else if (++tries > 600) clearInterval(t);          // stop after 2 minutes
  }, 200);
})();
