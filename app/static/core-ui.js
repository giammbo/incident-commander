/* Core controls and bounded HTML error recovery. No browser storage for drafts. */
(function () {
  var mobileFiltersOpen;
  var filterMedia = window.matchMedia('(max-width: 620px)');
  function initialiseFilters() {
    var panel = document.querySelector('.board-filter-panel');
    if (!panel) return;
    if (mobileFiltersOpen === undefined) mobileFiltersOpen = Number(panel.dataset.filterCount) > 0;
    panel.open = !filterMedia.matches || mobileFiltersOpen;
    if (!panel.dataset.bound) {
      panel.dataset.bound = 'true';
      panel.querySelector('summary').addEventListener('click', function () {
        // Record native pointer/keyboard activation before the default toggle.
        // Programmatic desktop expansion must not overwrite the mobile choice.
        if (filterMedia.matches) mobileFiltersOpen = !panel.open;
      });
    }
  }
  filterMedia.addEventListener('change', initialiseFilters);
  function focusError() {
    var error = document.querySelector('#global-error:not(:empty), .form-error');
    if (error && !error.dataset.focused) {
      error.setAttribute('tabindex', '-1'); error.focus(); error.dataset.focused = 'true';
    }
  }
  function initialise() {
    initialiseFilters();
    document.querySelectorAll('script.core-draft').forEach(function (node) {
      var draft = JSON.parse(node.textContent);
      var form = Array.from(document.forms).find(function (f) { return new URL(f.action).pathname === draft.action; });
      if (form) {
        Array.from(form.elements).forEach(function (input) {
          var values = draft.values[input.name];
          if (input.type === 'password' || input.type === 'file') return;
          if (input.type === 'checkbox' || input.type === 'radio') input.checked = (values || []).includes(input.value);
          else if (input.tagName === 'SELECT' && (values || input.multiple)) Array.from(input.options).forEach(function (o) { o.selected = (values || []).includes(o.value); });
          else if (values) input.value = values[0] || '';
        });
        var parent = form.parentElement;
        while (parent) { if (parent.tagName === 'DETAILS') parent.open = true; parent = parent.parentElement; }
      }
      node.remove();
    });
    document.querySelectorAll('.table-scroll').forEach(function (region) {
      region.tabIndex = 0;
      region.setAttribute('role', 'region');
      region.setAttribute('aria-label', 'Scrollable table');
    });
    document.querySelectorAll('.card > table:not(.tbl):not(:has(td[data-label]))').forEach(function (table) {
      table.parentElement.tabIndex = 0;
      table.parentElement.setAttribute('aria-label', 'Scrollable table');
    });
    focusError();
  }
  document.addEventListener('click', function (event) {
    var toggle = event.target.closest('.menu-toggle');
    if (!toggle) return;
    var expanded = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(expanded));
    document.querySelector('.sidebar').classList.toggle('menu-open', expanded);
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && document.querySelector('.sidebar.menu-open')) {
      document.querySelector('.sidebar').classList.remove('menu-open');
      var toggle = document.querySelector('.menu-toggle'); toggle.setAttribute('aria-expanded', 'false'); toggle.focus();
    }
  });
  document.addEventListener('htmx:beforeSwap', function (event) {
    var xhr = event.detail.xhr;
    if (xhr.status >= 400 && xhr.getResponseHeader('HX-Retarget') && (xhr.getResponseHeader('Content-Type') || '').includes('text/html')) {
      event.detail.shouldSwap = true; event.detail.isError = false;
    }
  });
  ['htmx:sendError', 'htmx:timeout'].forEach(function (name) {
    document.addEventListener(name, function () {
      var error = document.querySelector('#global-error');
      if (error) { error.className = 'form-error'; error.textContent = 'Connection lost. Your changes were not confirmed. Check your connection and try again.'; error.focus(); }
    });
  });
  document.addEventListener('htmx:afterSwap', function (event) {
    if (event.detail.target && event.detail.target.id === 'global-error') delete event.detail.target.dataset.focused;
  });
  document.addEventListener('htmx:afterSettle', initialise);
  initialise();
})();
