// Light/dark theme, loaded blocking in <head> so data-theme is set before the first paint.
// Same storage contract as oya: localStorage "darkMode" = "true" | "false". Unlike oya, an
// unset key follows the operating system (prefers-color-scheme) instead of meaning light,
// and toggling re-themes in place instead of reloading the page.
(function () {
  "use strict";

  var KEY = "darkMode";
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function stored() {
    try {
      return localStorage.getItem(KEY);
    } catch (e) {
      return null; // storage disabled (private mode, blocked cookies)
    }
  }

  function preferredDark() {
    var value = stored();
    return value === null ? !!(media && media.matches) : value === "true";
  }

  function apply(dark) {
    root.setAttribute("data-theme", dark ? "dark" : "light");
  }

  apply(preferredDark());

  if (media && media.addEventListener) {
    media.addEventListener("change", function () {
      if (stored() === null) apply(media.matches);
    });
  }

  // Keep several open tabs in step.
  window.addEventListener("storage", function (event) {
    if (event.key === KEY) apply(preferredDark());
  });

  document.addEventListener("DOMContentLoaded", function () {
    var toggle = document.getElementById("theme-toggle");
    if (!toggle) return;
    toggle.addEventListener("click", function () {
      var dark = root.getAttribute("data-theme") !== "dark";
      try {
        localStorage.setItem(KEY, String(dark));
      } catch (e) {
        // not persisted; still switch for this page view
      }
      apply(dark);
    });
  });
})();
