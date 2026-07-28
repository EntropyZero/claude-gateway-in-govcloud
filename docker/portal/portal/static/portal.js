/* Claude Code download portal - first-party progressive enhancement only.
 * No frameworks, no network calls. Everything here is convenience: every
 * page works (less conveniently) with JavaScript disabled, and all
 * enforcement stays server-side.
 *
 * 1. Download page: upgrade the two-step cost-center form into chained
 *    Cost Center -> Team dropdowns (mapping read from the #cc-map JSON data
 *    block) and submit straight to /portal/download.
 * 2. Tables with class="sortable": client-side sort of the CURRENT page's
 *    rows by clicking a column header. Cells may carry data-sort for a
 *    numeric key; headers may carry data-numeric to default to numeric
 *    comparison.
 */
(function () {
  "use strict";

  /* ---------------------------------------------- chained dropdowns */
  function initChainedDropdowns() {
    var mapEl = document.getElementById("cc-map");
    var form = document.getElementById("cc-stage1");
    if (!mapEl || !form) return;
    var map;
    try {
      map = JSON.parse(mapEl.textContent);
    } catch (e) {
      return; /* fall back to the noscript two-step flow */
    }
    var cc = form.querySelector("#cost_center");
    var submit = form.querySelector("#cc-submit");
    if (!cc || !submit) return;

    var label = document.createElement("label");
    label.setAttribute("for", "team");
    label.textContent = "Team";
    var team = document.createElement("select");
    team.id = "team";
    team.name = "team";
    team.required = true;

    function fillTeams() {
      while (team.firstChild) team.removeChild(team.firstChild);
      var teams = map[cc.value] || [];
      for (var i = 0; i < teams.length; i++) {
        var o = document.createElement("option");
        o.value = teams[i];
        o.textContent = teams[i];
        team.appendChild(o);
      }
    }
    cc.addEventListener("change", fillTeams);
    fillTeams();

    /* Keep Team next to Cost center (before the Platform select, when the
     * page ships one); fall back to just-before-submit otherwise. */
    var anchor = document.getElementById("platform-label") || submit;
    form.insertBefore(label, anchor);
    form.insertBefore(team, anchor);
    /* Server-side validate_selection still re-checks the pair. */
    form.setAttribute("action", form.getAttribute("data-download-action"));
    submit.textContent = "Download pre-configured installer";
  }

  /* ---------------------------------------------- table sort */
  function cellKey(row, idx) {
    var cell = row.cells[idx];
    if (!cell) return "";
    var ds = cell.getAttribute("data-sort");
    return ds !== null ? ds : cell.textContent.trim();
  }

  function asNumber(text) {
    var n = parseFloat(String(text).replace(/[$,%\s]/g, ""));
    return isNaN(n) ? null : n;
  }

  function sortTable(table, th) {
    var idx = th.cellIndex;
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows);
    /* Skip placeholder rows (a single cell spanning the table). */
    if (rows.length < 2 || (rows.length === 1 && rows[0].cells.length === 1)) return;

    var dir = th.classList.contains("sorted-asc") ? -1 : 1;
    var numeric = th.hasAttribute("data-numeric");
    if (!numeric) {
      /* Auto-detect: numeric when every non-empty key parses as a number. */
      numeric = rows.every(function (r) {
        var k = cellKey(r, idx);
        return k === "" || asNumber(k) !== null;
      });
    }
    rows.sort(function (a, b) {
      var ka = cellKey(a, idx);
      var kb = cellKey(b, idx);
      if (numeric) {
        var na = asNumber(ka);
        var nb = asNumber(kb);
        if (na === null && nb === null) return 0;
        if (na === null) return 1; /* empties last, either direction */
        if (nb === null) return -1;
        return (na - nb) * dir;
      }
      return ka.localeCompare(kb, undefined, { sensitivity: "base" }) * dir;
    });

    var ths = table.tHead ? table.tHead.rows[0].cells : [];
    for (var i = 0; i < ths.length; i++) {
      ths[i].classList.remove("sorted-asc", "sorted-desc");
    }
    th.classList.add(dir === 1 ? "sorted-asc" : "sorted-desc");
    for (var j = 0; j < rows.length; j++) tbody.appendChild(rows[j]);
  }

  function initSortableTables() {
    var tables = document.querySelectorAll("table.sortable");
    for (var i = 0; i < tables.length; i++) {
      (function (table) {
        if (!table.tHead || !table.tHead.rows.length) return;
        var ths = table.tHead.rows[0].cells;
        for (var j = 0; j < ths.length; j++) {
          (function (th) {
            th.addEventListener("click", function () {
              sortTable(table, th);
            });
          })(ths[j]);
        }
      })(tables[i]);
    }
  }

  initChainedDropdowns();
  initSortableTables();
})();
