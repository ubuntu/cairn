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
 *   [data-tabs]                   links in .p-tabs__list to .cairn-tab-panel
 *                                 sections become tabs: one panel shows at
 *                                 a time, chosen by the URL's #hash. Without
 *                                 this, every panel shows in turn.
 *                                 [data-tab-shared] (the filter above the
 *                                 lists) hides for a panel marked
 *                                 [data-tab-without-shared].
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

    // Rows inside a tab that is not showing are not counted: "Showing 12
    // of 40" is about the list in front of the reader.
    function inView(row) {
      return !row.closest(".cairn-tab-panel[hidden]");
    }

    function apply() {
      var shown = 0;
      var total = 0;
      rows.forEach(function (row) {
        var keep = matches(row, controls);
        row.hidden = !keep;
        if (!inView(row)) return;
        total++;
        if (keep) shown++;
      });
      var filtering = controls.some(function (c) {
        return c.value.trim() !== "";
      });
      // A folded section whose rows are all filtered out says so, rather
      // than opening onto an empty table.
      document.querySelectorAll("main [data-filter-group]").forEach(function (group) {
        var visible = group.querySelectorAll("[data-filter]:not([hidden])").length;
        var label = group.querySelector("[data-filter-group-count]");
        if (label) label.textContent = String(visible);
        // "Nothing matches the filter" only when a filter hid every row: a
        // panel that had none to begin with already says so itself.
        var none = group.querySelector(".cairn-tab-panel__none");
        var any = group.querySelector("[data-filter]") !== null;
        if (none) none.hidden = !(filtering && any && visible === 0);
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
          ? "Showing " + shown + " of " + total
          : total + " in total";
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
        if (hash === -1) return;
        var id = href.slice(hash + 1);
        var target = document.getElementById(id);
        if (!target) return;
        // Going through the URL's #hash lets a tab holding the target open
        // itself (see setUpTabs) before the page scrolls to it.
        // Same hash twice in a row fires no hashchange; send one, so the
        // tabs still pick the panel the new filter's matches are in.
        if (window.location.hash === "#" + id) {
          window.dispatchEvent(new HashChangeEvent("hashchange"));
        } else {
          window.location.hash = id;
        }
      });
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      apply();
    });
    // Tabs ask for a recount when the reader switches.
    form.addEventListener("cairn:refilter", apply);
    form.addEventListener("reset", function () {
      window.setTimeout(apply, 0);
    });
    form.hidden = false;
    apply();
  }

  /* Jumping ------------------------------------------------------------ */

  // Suggestions under a search field, drawn by the page rather than the
  // browser: the datalist's own dropdown cannot be styled and looks
  // different in every browser. A combobox in the ARIA sense: arrows move
  // through the list, Enter opens the highlighted entry, Escape closes.
  var MAX_SUGGESTIONS = 8;
  var suggestCount = 0;

  function setUpJump(form) {
    var input = form.querySelector("input[list]");
    if (!input) return;
    var list = document.getElementById(input.getAttribute("list"));
    if (!list) return;
    var entries = Array.prototype.map.call(list.options, function (option) {
      var name = option.getAttribute("data-name") || option.value;
      return {
        value: option.value,
        name: name,
        lower: name.toLowerCase(),
        note: option.getAttribute("data-note") || "",
        href: option.getAttribute("data-href"),
      };
    });

    input.removeAttribute("list");
    var listId = "suggest-" + ++suggestCount;
    var box = document.createElement("ul");
    box.className = "cairn-suggest";
    box.id = listId;
    box.setAttribute("role", "listbox");
    box.hidden = true;
    input.parentNode.appendChild(box);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", listId);
    input.setAttribute("aria-expanded", "false");

    var shown = [];
    var active = -1;

    // Names that start with what was typed, then names with a word that
    // does (the "core" in "ubuntu-core"), then any that contain it.
    function rank(typed) {
      var starts = [], words = [], contains = [];
      entries.forEach(function (entry) {
        var at = entry.lower.indexOf(typed);
        if (at === 0) starts.push(entry);
        else if (at > 0 && /[-.+ ]/.test(entry.lower.charAt(at - 1))) words.push(entry);
        else if (at > 0) contains.push(entry);
      });
      return starts.concat(words, contains).slice(0, MAX_SUGGESTIONS);
    }

    function highlight(entry, typed) {
      var at = entry.lower.indexOf(typed);
      var frag = document.createDocumentFragment();
      frag.appendChild(document.createTextNode(entry.name.slice(0, at)));
      var strong = document.createElement("strong");
      strong.textContent = entry.name.slice(at, at + typed.length);
      frag.appendChild(strong);
      frag.appendChild(document.createTextNode(entry.name.slice(at + typed.length)));
      return frag;
    }

    function close() {
      box.hidden = true;
      active = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }

    function mark(index) {
      active = index;
      Array.prototype.forEach.call(box.children, function (li, i) {
        li.setAttribute("aria-selected", String(i === index));
      });
      if (index === -1) input.removeAttribute("aria-activedescendant");
      else input.setAttribute("aria-activedescendant", box.children[index].id);
    }

    function go(entry) {
      if (entry && entry.href) window.location.href = entry.href;
    }

    function render() {
      var typed = input.value.trim().toLowerCase();
      shown = typed ? rank(typed) : [];
      box.textContent = "";
      shown.forEach(function (entry, i) {
        var li = document.createElement("li");
        li.className = "cairn-suggest__item";
        li.id = listId + "-" + i;
        li.setAttribute("role", "option");
        // One element for the name: as loose text and <strong> they were
        // separate flex items, which space-between spread across the row.
        var label = document.createElement("span");
        label.className = "cairn-suggest__name";
        label.appendChild(highlight(entry, typed));
        li.appendChild(label);
        if (entry.note) {
          var note = document.createElement("span");
          note.className = "cairn-suggest__note";
          note.textContent = entry.note;
          li.appendChild(note);
        }
        // mousedown, not click: the field's blur would close the list first.
        li.addEventListener("mousedown", function (event) {
          event.preventDefault();
          go(entry);
        });
        li.addEventListener("mousemove", function () {
          if (active !== i) mark(i);
        });
        box.appendChild(li);
      });
      if (!shown.length) return close();
      // As wide as the field, not the field and its button.
      box.style.width = input.offsetWidth + "px";
      box.hidden = false;
      input.setAttribute("aria-expanded", "true");
      mark(-1);
    }

    input.addEventListener("input", render);
    input.addEventListener("focus", render);
    input.addEventListener("blur", function () {
      window.setTimeout(close, 100);
    });
    input.addEventListener("keydown", function (event) {
      if (box.hidden) {
        if (event.key === "ArrowDown") render();
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        // Positions run from -1 (back in the field) to the last entry, and
        // wrap at both ends.
        var next = active + (event.key === "ArrowDown" ? 1 : -1);
        if (next >= shown.length) next = -1;
        if (next < -1) next = shown.length - 1;
        mark(next);
      } else if (event.key === "Escape") {
        event.preventDefault();
        close();
      } else if (event.key === "Enter" && active !== -1) {
        event.preventDefault();
        go(shown[active]);
      }
    });

    form.addEventListener("submit", function (event) {
      var wanted = input.value.trim().toLowerCase();
      if (!wanted) return;
      // The labelled value ("ubuntu-desktop (team)") names one entry. A
      // bare name counts only if it is unique: ubuntu-desktop is both a
      // team and a package set, and must not be routed to whichever is
      // listed first. Otherwise the form submits to the index page, which
      // shows every match.
      var exact = entries.filter(function (entry) {
        return entry.value.toLowerCase() === wanted;
      });
      var bare = entries.filter(function (entry) {
        return entry.lower === wanted;
      });
      var found = exact.length === 1 ? exact[0] : bare.length === 1 ? bare[0] : null;
      if (found && found.href) {
        event.preventDefault();
        go(found);
      }
    });
  }

  /* Tabs ------------------------------------------------------------------ */

  function setUpTabs(root) {
    var links = Array.prototype.slice.call(root.querySelectorAll(".p-tabs__link[data-tab]"));
    var panels = links.map(function (link) {
      return document.getElementById(link.getAttribute("data-tab"));
    });
    if (!links.length || panels.indexOf(null) !== -1) return;
    var form = root.querySelector("form.cairn-filters");
    root.classList.add("is-tabbed");
    root.querySelector(".p-tabs__list").setAttribute("role", "tablist");

    // What sits above the panels for the lists (the filter, the glossary)
    // steps aside for a panel that is not a list, such as an overview.
    var shared = Array.prototype.slice.call(root.querySelectorAll("[data-tab-shared]"));

    function select(index, focus) {
      links.forEach(function (link, i) {
        var on = i === index;
        link.setAttribute("aria-selected", String(on));
        link.tabIndex = on ? 0 : -1;
        panels[i].hidden = !on;
      });
      var bare = panels[index].hasAttribute("data-tab-without-shared");
      shared.forEach(function (el) {
        el.hidden = bare;
      });
      if (focus) links[index].focus();
      if (form) form.dispatchEvent(new Event("cairn:refilter"));
    }

    links.forEach(function (link, i) {
      link.setAttribute("role", "tab");
      link.setAttribute("aria-controls", panels[i].id);
      link.id = "tab_" + panels[i].id;
      panels[i].setAttribute("role", "tabpanel");
      panels[i].setAttribute("aria-labelledby", link.id);
      link.addEventListener("click", function (event) {
        event.preventDefault();
        select(i, false);
        window.history.replaceState(
          null,
          "",
          window.location.pathname + window.location.search + "#" + panels[i].id
        );
      });
      link.addEventListener("keydown", function (event) {
        var step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
        if (!step) return;
        event.preventDefault();
        select((i + step + links.length) % links.length, true);
      });
    });

    // The tab the URL names; else, when a filter came with the URL, the
    // first tab with a match; else the first.
    // The panel the URL's #hash names, or the one holding the row it
    // names: a link to a settling upload opens the settling tab.
    // A link to the whole tabbed section (a filter link such as
    // "?reason=regression#the_list") opens the first list with a match:
    // the matches may all be in Still settling.
    function panelFor(hash) {
      var id = decodeURIComponent(hash.slice(1));
      if (!id) return -1;
      var target = document.getElementById(id);
      if (target && (target === root || target.contains(root))) {
        var withRows = panels.findIndex(function (panel) {
          return (
            !panel.hasAttribute("data-tab-without-shared") &&
            panel.querySelector("[data-filter]:not([hidden])")
          );
        });
        return Math.max(withRows, 0);
      }
      return panels.findIndex(function (panel) {
        return panel === target || (target && panel.contains(target));
      });
    }

    window.addEventListener("hashchange", function () {
      var found = panelFor(window.location.hash);
      if (found === -1) return;
      select(found, false);
      var target = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
      if (target) target.scrollIntoView();
    });

    var wanted = panelFor(window.location.hash);
    if (wanted === -1 && window.location.search) {
      if (form) form.dispatchEvent(new Event("cairn:refilter"));
      wanted = panels.findIndex(function (panel) {
        return panel.querySelector("[data-filter]:not([hidden])");
      });
    }
    select(Math.max(wanted, 0), false);
    if (wanted !== -1) {
      var row = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
      if (row && row !== panels[wanted]) row.scrollIntoView();
    }
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

  // A menu opens three ways: CSS on hover, CSS on keyboard focus, and a
  // click handled here. aria-expanded must follow all three, so it is
  // recomputed from what is shown rather than flipped on click. On a small
  // screen every sub-page is always listed and the heading is a plain
  // link, so it is not a disclosure and carries no aria-expanded at all.
  var wide = window.matchMedia("(min-width: 1036px)");
  var menus = Array.prototype.slice.call(document.querySelectorAll(".cairn-nav-menu"));

  function syncMenu(menu) {
    var toggle = menu.querySelector(".p-navigation__link");
    if (!wide.matches) {
      toggle.removeAttribute("aria-expanded");
      return;
    }
    var open =
      menu.classList.contains("is-active") ||
      (!menu.classList.contains("is-closed") &&
        (menu.matches(":hover") || menu.matches(":focus-within")));
    toggle.setAttribute("aria-expanded", String(open));
  }

  function closeMenus(except) {
    menus.forEach(function (menu) {
      if (menu !== except) menu.classList.remove("is-active");
      syncMenu(menu);
    });
  }

  menus.forEach(function (menu) {
    var toggle = menu.querySelector(".p-navigation__link");
    var list = menu.querySelector(".p-navigation__dropdown");
    if (list && list.id) toggle.setAttribute("aria-controls", list.id);
    toggle.addEventListener("click", function (event) {
      if (!wide.matches) return;
      event.preventDefault();
      var open = !menu.classList.contains("is-active");
      closeMenus(menu);
      menu.classList.toggle("is-active", open);
      // A click closes a menu that hover or focus is still holding open.
      if (!open) {
        menu.classList.add("is-closed");
        toggle.blur();
      }
      syncMenu(menu);
    });
    menu.addEventListener("mouseenter", function () {
      syncMenu(menu);
    });
    menu.addEventListener("mouseleave", function () {
      menu.classList.remove("is-closed");
      syncMenu(menu);
    });
    menu.addEventListener("focusin", function () {
      syncMenu(menu);
    });
    // Focus has not moved yet while focusout runs; check once it has.
    menu.addEventListener("focusout", function () {
      window.setTimeout(function () {
        if (!menu.matches(":focus-within")) menu.classList.remove("is-closed");
        syncMenu(menu);
      }, 0);
    });
    syncMenu(menu);
  });
  wide.addEventListener("change", function () {
    closeMenus(null);
  });
  document.addEventListener("click", function (event) {
    if (!event.target.closest(".cairn-nav-menu")) closeMenus(null);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var open = menus.filter(function (m) {
      return m.matches(":focus-within") || m.classList.contains("is-active");
    });
    open.forEach(function (menu) {
      menu.classList.remove("is-active");
      menu.classList.add("is-closed");
    });
    closeMenus(null);
  });

  document.querySelectorAll("table.cairn-sortable").forEach(makeSortable);
  document.querySelectorAll("form.cairn-filters").forEach(setUpFilters);
  document.querySelectorAll("form[data-jump]").forEach(setUpJump);
  document.querySelectorAll("[data-tabs]").forEach(setUpTabs);
  reveal();
})();
