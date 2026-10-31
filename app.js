document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) { e.preventDefault(); }
  var btn = e.target.querySelector("button[type=submit]");
  if (btn && !e.defaultPrevented) { btn.disabled = true; btn.textContent = "...در حال انجام"; }
});
var t = document.querySelector("[data-toast]");
if (t) { setTimeout(function () { t.style.opacity = "0"; }, 3500); }
var mt = document.getElementById("menu-toggle");
if (mt) { mt.addEventListener("click", function () { document.getElementById("sidebar").classList.toggle("open"); }); }
