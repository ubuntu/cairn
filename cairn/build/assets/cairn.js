/* cairn: sorting, filtering and jumping, layered over server-rendered HTML.
 *
 * Progressive enhancement only. Every page is complete without this file:
 * tables arrive in a sensible order, filter forms stay hidden, and the jump
 * forms submit to an index page that lists everything. No build step, no
 * dependencies, no network requests; it reads what the page already holds.
 *
 * Conventions the templates follow:
 *   table.cairn-sortable          headers become sort buttons, except
 *                                 th[data-nosort]; a cell's sort value is
 *                                 its data-sort attribute, else its text.
 *   form.cairn-filters            unhidden here; each select[name] or
 *                                 input[name] filters [data-filter] items
 *                                 (table rows or list items) in <main> by
 *                                 their data-f-<name> tokens. A folded
 *                                 details[data-filter-open] opens while a
 *                                 filter has matches inside it.
 *                                 The state is mirrored in the query string,
 *                                 so a link such as all.html?component=main
 *                                 opens filtered.
 *   a[href^="?"]                  on a page with a filter form, a link
 *                                 carrying only filter fields applies them
 *                                 in place instead of reloading the page.
 *   location.hash                 a row folded inside a closed <details>
 *                                 is revealed when a link points at it.
 *   [aria-describedby^="term_"]   a label with a definition on the page:
 *                                 shown as a tooltip on hover and focus,
 *                                 hidden on Escape. The definition stays in
 *                                 the page's "What the labels mean".
 *   .cairn-nav-menu               a navigation menu: its link opens and
 *                                 closes the menu instead of navigating.
 *   form[data-jump]               an input with a datalist whose options
 *                                 carry data-href: an exact match goes
 *                                 straight there, anything else submits.
 */
(function () {
  "use strict";

  /* Sorting ------------------------------------------------------------ */

  function cellValue(row, index) {
    var cell = row.cells[index];
    if (!cell) return "";
    var value = cell.getAttribute("data-sort");
    return (value === null ? cell.textContent : value).trim();
  }

  function isNumeric(values) {
    var seen = false;
    for (var i = 0; i < values.length; i++) {
      if (values[i] === "") continue;
      if (isNaN(Number(values[i]))) return false;
      seen = true;
    }
    return seen;
  }

  function sortBy(table, index, th) {
    var ascending = th.getAttribute("aria-sort") !== "ascending";
    table.querySelectorAll("th[aria-sort]").forEach(function (other) {
      other.removeAttribute("aria-sort");
    });
    th.setAttribute("aria-sort", ascending ? "ascending" : "descending");

    Array.prototype.forEach.call(table.tBodies, function (body) {
      var rows = Array.prototype.slice.call(body.rows);
      var values = rows.map(function (row) {
        return cellValue(row, index);
      });
      var numeric = isNumeric(values);
      var keyed = rows.map(function (row, i) {
        return { row: row, value: values[i], at: i };
      });
      keyed.sort(function (a, b) {
        // Empty values trail in either direction: absent data must never
        // look like the most urgent row.
        if (a.value === "" && b.value !== "") return 1;
        if (b.value === "" && a.value !== "") return -1;
        var order = numeric
          ? Number(a.value) - Number(b.value)
          : a.value.localeCompare(b.value);
        if (!ascending) order = -order;
        return order || a.at - b.at;
      });
      keyed.forEach(function (k) {
        body.appendChild(k.row);
      });
    });
  }

  function makeSortable(table) {
    var head = table.tHead && table.tHead.rows[0];
    if (!head) return;
    Array.prototype.forEach.call(head.cells, function (th, index) {
      if (th.hasAttribute("data-nosort")) return;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "cairn-sort";
      // The label's last word and the arrow (drawn after .cairn-sort__tail)
      // share a no-wrap span, so the arrow never wraps onto a line alone.
      var label = th.textContent.trim();
      var cut = label.lastIndexOf(" ");
      if (cut !== -1) button.appendChild(document.createTextNode(label.slice(0, cut + 1)));
      var tail = document.createElement("span");
      tail.className = "cairn-sort__tail";
      tail.textContent = label.slice(cut + 1);
      button.appendChild(tail);
      th.textContent = "";
      var hint = document.createElement("span");
      hint.className = "u-off-screen";
      hint.textContent = " (sort)";
      button.appendChild(hint);
      th.appendChild(button);
      button.addEventListener("click", function () {
        sortBy(table, index, th);
      });
    });
  }

  /* Filtering ---------------------------------------------------------- */

  function tokens(row, name) {
    return (row.getAttribute("data-f-" + name) || "").toLowerCase();
  }

  function matches(row, controls) {
    for (var i = 0; i < controls.length; i++) {
      var control = controls[i];
      var wanted = control.value.trim().toLowerCase();
      if (!wanted) continue;
      var have = tokens(row, control.name);
      if (control.tagName === "SELECT") {
        if ((" " + have + " ").indexOf(" " + wanted + " ") === -1) return false;
      } else {
        var terms = wanted.split(/\s+/);
        for (var t = 0; t < terms.length; t++) {
          if (have.indexOf(terms[t]) === -1) return false;
        }
      }
    }
    return true;
  }

  function setUpFilters(form) {
    var controls = Array.prototype.slice.call(
      form.querySelectorAll("select[name], input[name]")
    );
    var rows = Array.prototype.slice.call(
      document.querySelectorAll("main [data-filter]")
    );
    var count = form.querySelector(".cairn-filter-count");
    var params = new URLSearchParams(window.location.search);

    controls.forEach(function (control) {
      var value = params.get(control.name);
      if (value === null) return;
      if (control.tagName === "SELECT") {
        var known = Array.prototype.some.call(control.options, function (o) {
          return o.value === value;
        });
        if (!known) return;
      }
      control.value = value;
    });

    function apply() {
      var shown = 0;
      rows.forEach(function (row) {
        var keep = matches(row, controls);
        row.hidden = !keep;
        if (keep) shown++;
      });
      // A folded section whose rows are all filtered out says so, rather
      // than opening onto an empty table.
      document.querySelectorAll("main [data-filter-group]").forEach(function (group) {
        var visible = group.querySelectorAll("[data-filter]:not([hidden])").length;
        var label = group.querySelector("[data-filter-group-count]");
        if (label) label.textContent = String(visible);
        // A section that only exists to hold the list goes with it.
        if (group.hasAttribute("data-filter-hide-empty")) group.hidden = visible === 0;
      });
      var active = controls.some(function (c) {
        return c.value.trim() !== "";
      });
      // Matches folded away would look like no matches at all.
      document.querySelectorAll("main details[data-filter-open]").forEach(function (group) {
        if (active && group.querySelector("[data-filter]:not([hidden])")) group.open = true;
      });
      if (count) {
        count.textContent = active
          ? "Showing " + shown + " of " + rows.length
          : rows.length + " in total";
      }
      var query = new URLSearchParams();
      controls.forEach(function (c) {
        if (c.value.trim()) query.set(c.name, c.value.trim());
      });
      var search = query.toString();
      window.history.replaceState(
        null,
        "",
        window.location.pathname + (search ? "?" + search : "") + window.location.hash
      );
    }

    controls.forEach(function (control) {
      control.addEventListener(control.tagName === "SELECT" ? "change" : "input", apply);
    });

    // Stat and breakdown links such as "?reason=regression#the_list":
    // set the filter and scroll, without reloading a page of this size.
    var names = controls.map(function (c) {
      return c.name;
    });
    document.querySelectorAll('main a[href^="?"]').forEach(function (link) {
      var href = link.getAttribute("href");
      var hash = href.indexOf("#");
      var query = new URLSearchParams(href.slice(1, hash === -1 ? undefined : hash));
      var ours = Array.from(query.keys()).every(function (key) {
        return names.indexOf(key) !== -1;
      });
      if (!ours) return;
      link.addEventListener("click", function (event) {
        event.preventDefault();
        controls.forEach(function (c) {
          c.value = query.get(c.name) || "";
        });
        apply();
        var target = hash === -1 ? null : document.getElementById(href.slice(hash + 1));
        if (target) target.scrollIntoView();
      });
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      apply();
    });
    form.addEventListener("reset", function () {
      window.setTimeout(apply, 0);
    });
    form.hidden = false;
    apply();
  }

  /* Jumping ------------------------------------------------------------ */

  function setUpJump(form) {
    var input = form.querySelector("input[list]");
    if (!input) return;
    var list = document.getElementById(input.getAttribute("list"));
    if (!list) return;
    form.addEventListener("submit", function (event) {
      var wanted = input.value.trim().toLowerCase();
      if (!wanted) return;
      var found = Array.prototype.find.call(list.options, function (option) {
        return option.value.toLowerCase() === wanted;
      });
      if (found && found.getAttribute("data-href")) {
        event.preventDefault();
        window.location.href = found.getAttribute("data-href");
      }
    });
  }

  /* Revealing a linked row ----------------------------------------------- */

  function reveal() {
    var id = decodeURIComponent(window.location.hash.slice(1));
    var target = id && document.getElementById(id);
    if (!target) return;
    var folded = target.closest("details:not([open])");
    if (!folded) return;
    folded.open = true;
    target.scrollIntoView();
  }

  window.addEventListener("hashchange", reveal);

  /* Tooltips -------------------------------------------------------------- */

  // One tooltip element for the page. aria-hidden: screen readers already
  // hear the definition through aria-describedby.
  var tip = document.createElement("div");
  tip.className = "cairn-tip";
  tip.setAttribute("aria-hidden", "true");
  tip.hidden = true;
  document.body.appendChild(tip);
  var shownFor = null;
  var hideTimer = null;

  function termOf(node) {
    return node && node.closest ? node.closest('[aria-describedby^="term_"]') : null;
  }

  function showTip(el) {
    var definition = document.getElementById(el.getAttribute("aria-describedby"));
    if (!definition) return;
    window.clearTimeout(hideTimer);
    shownFor = el;
    tip.textContent = definition.textContent.trim();
    tip.hidden = false;
    var box = el.getBoundingClientRect();
    var width = tip.offsetWidth;
    var left = Math.min(Math.max(8, box.left), window.innerWidth - width - 8);
    var top = box.bottom + 6;
    if (top + tip.offsetHeight > window.innerHeight - 8) {
      top = box.top - tip.offsetHeight - 6;
    }
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }

  function hideTip() {
    tip.hidden = true;
    shownFor = null;
  }

  // A short delay lets the pointer move onto the tooltip without it
  // vanishing, so it can be read or selected (WCAG 1.4.13).
  function hideSoon() {
    window.clearTimeout(hideTimer);
    hideTimer = window.setTimeout(hideTip, 150);
  }

  document.addEventListener("mouseover", function (event) {
    if (tip.contains(event.target)) {
      window.clearTimeout(hideTimer);
      return;
    }
    var el = termOf(event.target);
    if (el && el !== shownFor) showTip(el);
    else if (!el && shownFor) hideSoon();
  });
  document.addEventListener("focusin", function (event) {
    var el = termOf(event.target);
    if (el) showTip(el);
    else hideTip();
  });
  document.addEventListener("focusout", hideSoon);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") hideTip();
  });
  window.addEventListener("scroll", hideTip, { passive: true });

  /* Navigation menus -------------------------------------------------------- */

  function closeMenus(except) {
    document.querySelectorAll(".cairn-nav-menu.is-active").forEach(function (menu) {
      if (menu === except) return;
      menu.classList.remove("is-active");
      menu.querySelector(".p-navigation__link").setAttribute("aria-expanded", "false");
    });
  }

  document.querySelectorAll(".cairn-nav-menu").forEach(function (menu) {
    var toggle = menu.querySelector(".p-navigation__link");
    toggle.addEventListener("click", function (event) {
      // On a small screen every sub-page is already listed: let the
      // heading navigate as a plain link.
      if (!window.matchMedia("(min-width: 1036px)").matches) return;
      event.preventDefault();
      var open = !menu.classList.contains("is-active");
      closeMenus(menu);
      menu.classList.toggle("is-active", open);
      toggle.setAttribute("aria-expanded", String(open));
    });
  });
  document.addEventListener("click", function (event) {
    if (!event.target.closest(".cairn-nav-menu")) closeMenus(null);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeMenus(null);
  });

  document.querySelectorAll("table.cairn-sortable").forEach(makeSortable);
  document.querySelectorAll("form.cairn-filters").forEach(setUpFilters);
  document.querySelectorAll("form[data-jump]").forEach(setUpJump);
  reveal();
})();
