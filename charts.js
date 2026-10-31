(function () {
  var el = document.getElementById("chart-data");
  if (!el || !window.Chart) { return; }
  var d = JSON.parse(el.textContent);
  Chart.defaults.color = "#8B9BB0";
  Chart.defaults.borderColor = "#233143";
  new Chart(document.getElementById("revChart"), {
    type: "line",
    data: { labels: d.labels, datasets: [{ data: d.revenue, borderColor: "#00FFF7", backgroundColor: "rgba(0,255,247,.12)", fill: true, tension: .3 }] },
    options: { plugins: { legend: { display: false } } }
  });
  new Chart(document.getElementById("statusChart"), {
    type: "doughnut",
    data: { labels: d.status_labels, datasets: [{ data: d.status_values,
      backgroundColor: ["#F59E0B", "#3B82F6", "#00FFF7", "#22C55E", "#EF4444", "#64748B", "#A855F7", "#94A3B8"] }] }
  });
})();
