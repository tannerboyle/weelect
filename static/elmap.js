/* Voting-locations map for the Elections panel. Leaflet (self-hosted) + OpenStreetMap tiles.
   Data comes from <script type="application/json" id="elmap-data"> rendered by the server. */
(function () {
  var el = document.getElementById("elmap"), raw = document.getElementById("elmap-data");
  if (!el || !raw || !window.L) { if (el) el.hidden = true; return; }
  var D = JSON.parse(raw.textContent);
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }

  var touch = L.Browser.mobile;
  var map = L.map(el, {scrollWheelZoom: false, dragging: !touch, tap: false, zoomControl: true, attributionControl: true, keyboard: true});
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
  }).addTo(map);
  map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');

  // Icons: voter = navy circle with a cream star; sites = red pin with a cream letter (P polling, E early, D drop-off).
  // Shape AND colour differ so the two are distinguishable without colour vision.
  var voterIcon = L.divIcon({className: "elpin", iconSize: [34, 34], iconAnchor: [17, 17], popupAnchor: [0, -16], tooltipAnchor: [18, 0],
    html: '<svg viewBox="0 0 34 34" width="34" height="34" aria-hidden="true"><circle cx="17" cy="17" r="15" fill="#1f2f5a" stroke="#f7f3e8" stroke-width="2.5"/>' +
          '<polygon points="17,6.5 20.2,13.6 28,14.5 22.2,19.7 23.8,27.4 17,23.5 10.2,27.4 11.8,19.7 6,14.5 13.8,13.6" fill="#f7f3e8"/></svg>'});
  function siteIcon(letter) {
    return L.divIcon({className: "elpin", iconSize: [30, 40], iconAnchor: [15, 38], popupAnchor: [0, -34], tooltipAnchor: [14, -20],
      html: '<svg viewBox="0 0 30 40" width="30" height="40" aria-hidden="true"><path d="M15 38C15 38 2.5 23.5 2.5 14.5a12.5 12.5 0 0 1 25 0C27.5 23.5 15 38 15 38Z" fill="#9b2335" stroke="#f7f3e8" stroke-width="2"/>' +
            '<text x="15" y="19.5" text-anchor="middle" font-family="Source Serif 4,Georgia,serif" font-weight="700" font-size="14" fill="#f7f3e8">' + esc(letter) + '</text></svg>'});
  }

  var bounds = [], nearest = [];
  var v = D.voter, vm = L.marker([v.lat, v.lng], {icon: voterIcon, title: "Your address", alt: "Your address", keyboard: true, zIndexOffset: 1000}).addTo(map);
  vm.bindTooltip("<strong>Your address</strong><br>" + esc(v.label), {direction: "right", opacity: 1, className: "eltip"});
  vm.bindPopup("<strong>Your address</strong><br>" + esc(v.label));
  bounds.push([v.lat, v.lng]);

  // Tooltips are clipped by the map edge, so after one opens we try each side and keep the one that fits best.
  var DIRS = [["top", [0, -6]], ["bottom", [0, 6]], ["right", [10, -14]], ["left", [-10, -14]]];
  function overflow(t) {
    var r = t.getElement().getBoundingClientRect(), c = el.getBoundingClientRect();
    return Math.max(0, c.top - r.top) + Math.max(0, c.left - r.left) + Math.max(0, r.right - c.right) + Math.max(0, r.bottom - c.bottom);
  }
  function fitTooltip(m) {
    var t = m.getTooltip(); if (!t || m._fitting || !t.getElement()) return;
    m._fitting = true;
    var best = null, i;
    for (i = 0; i < DIRS.length; i++) {
      t.options.direction = DIRS[i][0]; t.options.offset = DIRS[i][1];
      m.closeTooltip(); m.openTooltip();
      var o = t.getElement() ? overflow(t) : 1e9;
      if (best === null || o < best[0]) best = [o, i];
      if (o === 0) break;
    }
    if (best[1] !== i) { t.options.direction = DIRS[best[1]][0]; t.options.offset = DIRS[best[1]][1]; m.closeTooltip(); m.openTooltip(); }
    m._fitting = false;
  }
  function body(s, full) {
    var h = '<div class="elp"><strong>' + esc(s.name) + "</strong>";
    if (s.address) h += "<div>" + esc(s.address) + "</div>";
    h += '<div class="elp-d">' + s.miles + " mi from your address</div>";
    s.entries.forEach(function (e) {
      h += '<div class="elp-e"><b>' + esc(e.label) + "</b>";
      if (e.name && e.name !== s.name) h += "<div>" + esc(e.name) + "</div>";
      if (e.dates && full) h += "<div>" + esc(e.dates) + "</div>";
      if (e.hours) h += '<div class="elp-h">' + esc(e.hours) + "</div>";
      if (e.notes && full) h += '<div class="elp-n">' + esc(e.notes) + "</div>";
      h += "</div>";
    });
    if (full && s.map) h += '<div><a href="' + esc(s.map) + '" target="_blank" rel="noopener">Open in Google Maps &rarr;</a></div>';
    return h + "</div>";
  }
  D.sites.forEach(function (s, i) {
    var label = s.name + (s.address ? ", " + s.address : "");
    var m = L.marker([s.lat, s.lng], {icon: siteIcon(s.letter), title: label, alt: label, keyboard: true}).addTo(map);
    m.bindTooltip(body(s, false), {direction: "top", opacity: 1, className: "eltip", offset: [0, -4]});
    m.on("tooltipopen", function () { fitTooltip(m); });
    m.bindPopup(body(s, true), {maxWidth: 280, autoPanPaddingTopLeft: [56, 14], autoPanPaddingBottomRight: [14, 14]});
    m.on("add", function () { var n = m.getElement(); if (n) { n.setAttribute("role", "button"); n.setAttribute("aria-label", label + ". Press Enter for details."); } });
    m.on("popupopen", function () { m.closeTooltip(); });
    if (i < (D.fit || 12)) bounds.push([s.lat, s.lng]);     // sites arrive sorted nearest-first
  });
  map.fitBounds(bounds, {padding: [28, 28], maxZoom: 15});
  // Leaflet's marker elements are created on add(); wire focus events to the DOM nodes (Leaflet does not emit them)
  map.eachLayer(function (l) { if (l instanceof L.Marker && l.getElement) { var n = l.getElement(); if (n) { n.addEventListener("focus", function () { if (!l.isPopupOpen()) l.openTooltip(); }); n.addEventListener("blur", function () { l.closeTooltip(); }); } } });
})();
