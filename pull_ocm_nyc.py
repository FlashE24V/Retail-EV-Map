<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>NY EV Retail Map (OCM CSV)</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>

  <link rel="stylesheet" href="https://unpkg.com/leaflet/dist/leaflet.css" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />

  <script src="https://unpkg.com/leaflet/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
  <script src="https://unpkg.com/papaparse@5.4.1/papaparse.min.js"></script>

  <style>
    html, body { height: 100%; margin: 0; }
    #map { height: 100vh; width: 100%; }

    .controls {
      background: white;
      padding: 8px 10px;
      border-radius: 8px;
      box-shadow: 0 1px 6px rgba(0,0,0,0.25);
      font: 14px/1.2 system-ui, -apple-system, Arial, sans-serif;
    }
    .controls .title { font-weight: 700; margin-bottom: 6px; display:block; }
    .controls select, .controls label {
      width: 260px;
      max-width: 75vw;
      font-size: 14px;
    }
    .controls select {
      padding: 6px 8px;
      border-radius: 8px;
      border: 1px solid #e5e7eb;
      background: #fff;
    }
    .controls .hint {
      margin-top: 6px;
      font-size: 12px;
      color: #6b7280;
    }
    .controls .row {
      margin-top: 8px;
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: #111827;
      user-select: none;
    }
    .controls input[type="checkbox"]{
      width: 16px; height: 16px;
    }

    /* ⚡ circle icon */
    .bolt-circle {
      width: 26px;
      height: 26px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      border: 2px solid #222;
      color: #fff;
      font-size: 16px;
      line-height: 1;
      box-shadow: 0 1px 4px rgba(0,0,0,0.25);
      transform: translate(-1px, -1px);
      position: relative;
    }
    .bolt-l2 { background: #2563eb; }
    .bolt-l3 { background: #16a34a; }

    .op-badge {
      position: absolute;
      right: -8px;
      top: -8px;
      min-width: 18px;
      height: 18px;
      padding: 0 4px;
      border-radius: 999px;
      background: #111;
      color: #fff;
      border: 2px solid #fff;
      box-shadow: 0 1px 4px rgba(0,0,0,0.25);
      font-size: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      letter-spacing: .3px;
    }

    .leaflet-div-icon { background: transparent; border: none; }

    .popup .row { margin-top:6px; }
    .popup .k { font-weight:700; }
    .popup .addr { margin-top:6px; color:#374151; }
    .popup .links { margin-top:8px; }

    /* Footer */
    .footer-attrib {
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      z-index: 9999;
      background: rgba(255,255,255,0.92);
      border-top: 1px solid rgba(0,0,0,0.12);
      padding: 6px 12px;
      font: 12px/1.2 system-ui, -apple-system, Arial, sans-serif;
      color: #374151;
      display: flex;
      gap: 10px;
      justify-content: space-between;
      align-items: center;
      backdrop-filter: blur(4px);
    }
    .footer-attrib a { color: #111; text-decoration: none; font-weight: 600; }
    .footer-attrib a:hover { text-decoration: underline; }
    .leaflet-bottom { margin-bottom: 28px; }
  </style>
</head>

<body>
  <div id="map"></div>

  <div class="footer-attrib">
    <div>Data from <a href="https://openchargemap.org" target="_blank" rel="noopener">OpenChargeMap</a></div>
    <div style="opacity:.8;">Retail EV charging locations</div>
  </div>

  <script>
    // ======================
    // CONFIG
    // ======================
    var OCM_CSV_URL = 'ocm_nyc_retail_locations.csv?v=' + Date.now();

    var map = L.map('map').setView([40.7128, -74.0060], 10);

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);

    // ✅ Clusters + no blue outline on hover
    var clusterGroup = L.markerClusterGroup({
      showCoverageOnHover: false,
      spiderfyOnMaxZoom: true,
      disableClusteringAtZoom: 17
    });
    map.addLayer(clusterGroup);

    // ======================
    // HELPERS
    // ======================
    function cleanStr(v) {
      if (v === undefined || v === null) return '';
      return ('' + v).trim();
    }

    function escapeHtml(s) {
      return ('' + (s ?? '')).replace(/[&<>"']/g, function(m) {
        return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]);
      });
    }

    function normOperator(op) {
      return cleanStr(op).replace(/\s+/g, ' ');
    }

    function isDcFast(row) {
      var hasDc = row.has_dc_fast;
      if (hasDc === 1 || hasDc === true || ('' + hasDc).toLowerCase() === 'true') return true;

      var levels = cleanStr(row.charging_levels).toLowerCase();
      if (levels.includes('level 3')) return true;

      var maxKw = parseFloat(row.max_kw);
      if (!isNaN(maxKw) && maxKw >= 50) return true;

      return false;
    }

    function chargingLevelLabel(row) {
      return isDcFast(row) ? 'DC Fast (Level 3)' : 'Level 2';
    }

    // ✅ Heuristic: public access BUT paid parking / garage / valet / etc.
    function isPaidParkingLikely(row) {
      var parts = [
        row.name,
        row.operator,
        row.address,
        row.town,
        row.usage_type,
        row.usage_cost,
        row.access_comments,
        row.general_comments,
        row.instructions
      ].map(function(v){ return (v ?? '').toString().toLowerCase(); });

      var text = parts.join(' | ');

      // Strong indicators
      var hardBad = [
        'paid parking',
        'pay to park',
        'parking fee',
        'fee for parking',
        'paid garage',
        'parking garage',
        'garage parking',
        'inside garage',
        'in garage',
        'valet',
        'attendant',
        'parking deck',
        'car park',
        'parkade',
        'metered parking',
        'hourly parking',
        'daily parking',
        'validation required',
        'validated parking',
        'ticket required'
      ];

      if (hardBad.some(function(k){ return text.includes(k); })) return true;

      // Garage-ish words + fee-ish words
      var garageWords = ['garage','parking','deck','valet','attendant','lot'];
      var feeWords = ['$', 'usd', 'fee', 'fees', 'paid', 'rate', 'hour', 'hourly', 'per hour', 'per hr', 'daily'];

      var hasGarage = garageWords.some(function(k){ return text.includes(k); });
      var hasFee = feeWords.some(function(k){ return text.includes(k); });
      if (hasGarage && hasFee) return true;

      // $ + parking in any order
      if (text.includes('parking') && text.includes('$')) return true;

      return false;
    }

    function makeOperatorCode(op) {
      var t = (op || '').toLowerCase();
      if (t.includes('chargepoint')) return 'CP';
      if (t.includes('evgo')) return 'EVgo';
      if (t.includes('blink')) return 'BLK';
      if (t.includes('flo')) return 'FLO';
      if (t.includes('revel')) return 'RVL';
      if (t.includes('ampup')) return 'AMP';
      if (t.includes('powerflex')) return 'PFX';
      if (t.includes('ev connect')) return 'EVC';
      if (t.includes('ev gateway')) return 'EVG';
      if (t.includes('noodoe')) return 'NOD';
      if (t.includes('vialynk')) return 'VIA';
      if (t.includes('ionna')) return 'ION';
      var s = normOperator(op);
      return s ? s.replace(/[^A-Za-z]/g,'').slice(0,3).toUpperCase() : '';
    }

    function boltIconHTML(levelClass, operatorText) {
      var SHOW_BADGE = true;
      var badge = '';
      if (SHOW_BADGE) {
        var code = makeOperatorCode(operatorText);
        if (code) badge = '<span class="op-badge">' + escapeHtml(code) + '</span>';
      }
      return '<div class="bolt-circle ' + levelClass + '">⚡' + badge + '</div>';
    }

    function makePopup(row) {
      var name = cleanStr(row.name) || 'EV Charging Site';
      var op = normOperator(row.operator);
      var level = chargingLevelLabel(row);

      var plugs = cleanStr(row.plug_types);           // ✅ Plug Types
      var currents = cleanStr(row.current_types);
      var levels = cleanStr(row.charging_levels);
      var maxKw = (row.max_kw !== undefined && row.max_kw !== null && row.max_kw !== '') ? row.max_kw : '';
      var points = (row.num_points !== undefined && row.num_points !== null && row.num_points !== '') ? row.num_points : '';
      var status = cleanStr(row.status_type);
      var usage = cleanStr(row.usage_type);

      var addr = cleanStr(row.address);
      var town = cleanStr(row.town);
      var state = cleanStr(row.state) || 'NY';
      var postcode = cleanStr(row.postcode);

      var lat = row.lat, lon = row.lon;

      // ✅ Directions chooser (Apple + Google)
      var apple = 'http://maps.apple.com/?daddr=' + lat + ',' + lon;
      var gmaps = 'https://www.google.com/maps/dir/?api=1&destination=' + lat + ',' + lon;

      var addrLine = [addr, town, state, postcode].filter(Boolean).join(', ');

      var html = '<div class="popup" style="min-width:260px;">' +
        '<strong>' + escapeHtml(name) + '</strong>' +
        '<div class="row"><span class="k">Charging Level:</span> ' + escapeHtml(level) + '</div>' +
        (op ? '<div class="row"><span class="k">Operator:</span> ' + escapeHtml(op) + '</div>' : '') +
        (points ? '<div class="row"><span class="k">Ports (reported):</span> ' + escapeHtml(points) + '</div>' : '') +
        (maxKw !== '' ? '<div class="row"><span class="k">Max kW:</span> ' + escapeHtml(maxKw) + '</div>' : '') +
        (plugs ? '<div class="row"><span class="k">Plug Types:</span> ' + escapeHtml(plugs) + '</div>' : '') +
        (currents ? '<div class="row"><span class="k">Current Types:</span> ' + escapeHtml(currents) + '</div>' : '') +
        (levels ? '<div class="row"><span class="k">OCM Levels:</span> ' + escapeHtml(levels) + '</div>' : '') +
        (status ? '<div class="row"><span class="k">OCM Status:</span> ' + escapeHtml(status) + '</div>' : '') +
        (usage ? '<div class="row"><span class="k">Access:</span> ' + escapeHtml(usage) + '</div>' : '') +
        (addrLine ? '<div class="addr">' + escapeHtml(addrLine) + '</div>' : '') +
        '<div class="links">' +
          '<a href="' + apple + '" target="_blank" rel="noopener">Directions (Apple)</a> &nbsp;|&nbsp; ' +
          '<a href="' + gmaps + '" target="_blank" rel="noopener">Directions (Google)</a>' +
        '</div>' +
      '</div>';

      return html;
    }

    // ======================
    // FILTER STATE
    // ======================
    var operatorFilter = 'ALL';
    var levelFilter = 'ALL'; // ALL | Level 2 | DC Fast (Level 3)
    var hidePaidParking = true; // ✅ default ON

    function passesOperator(row) {
      if (operatorFilter === 'ALL') return true;
      return normOperator(row.operator) === operatorFilter;
    }

    function passesLevel(row) {
      if (levelFilter === 'ALL') return true;
      return chargingLevelLabel(row) === levelFilter;
    }

    function passesPaidParking(row) {
      if (!hidePaidParking) return true;
      return !isPaidParkingLikely(row);
    }

    // ======================
    // UI CONTROLS
    // ======================
    var operatorControl, levelControl;

    function addOperatorControl(operatorsSorted) {
      operatorControl = L.control({position: 'topleft'});
      operatorControl.onAdd = function () {
        var div = L.DomUtil.create('div', 'controls');
        div.innerHTML =
          '<span class="title">Operator</span>' +
          '<select id="operatorSelect"></select>' +
          '<div class="row">' +
            '<input id="paidToggle" type="checkbox" checked />' +
            '<label for="paidToggle">Hide paid parking / garages</label>' +
          '</div>' +
          '<div class="hint">Tip: pick an operator, then filter Level 2 vs DC Fast.</div>';

        L.DomEvent.disableClickPropagation(div);
        L.DomEvent.disableScrollPropagation(div);

        var sel = div.querySelector('#operatorSelect');
        var toggle = div.querySelector('#paidToggle');

        var allOpt = document.createElement('option');
        allOpt.value = 'ALL';
        allOpt.textContent = 'All Operators';
        sel.appendChild(allOpt);

        (operatorsSorted || []).forEach(function(op){
          var o = document.createElement('option');
          o.value = op;
          o.textContent = op;
          sel.appendChild(o);
        });

        sel.addEventListener('change', function(){
          operatorFilter = sel.value;
          render();
        });

        toggle.addEventListener('change', function(){
          hidePaidParking = toggle.checked;
          render();
        });

        return div;
      };
      operatorControl.addTo(map);
    }

    function addLevelControl() {
      levelControl = L.control({position: 'topright'});
      levelControl.onAdd = function () {
        var div = L.DomUtil.create('div', 'controls');
        div.innerHTML =
          '<span class="title">Charging Level</span>' +
          '<select id="levelSelect">' +
            '<option value="ALL">All Levels</option>' +
            '<option value="Level 2">Level 2</option>' +
            '<option value="DC Fast (Level 3)">DC Fast (Level 3)</option>' +
          '</select>';

        L.DomEvent.disableClickPropagation(div);
        L.DomEvent.disableScrollPropagation(div);

        var sel = div.querySelector('#levelSelect');
        sel.addEventListener('change', function(){
          levelFilter = sel.value;
          render();
        });

        return div;
      };
      levelControl.addTo(map);
    }

    // ======================
    // RENDER
    // ======================
    var allRows = [];

    function render() {
      clusterGroup.clearLayers();
      var any = false;

      allRows.forEach(function(row){
        var lat = row.lat, lon = row.lon;
        if (typeof lat !== 'number' || typeof lon !== 'number' || isNaN(lat) || isNaN(lon)) return;

        if (!passesOperator(row)) return;
        if (!passesLevel(row)) return;
        if (!passesPaidParking(row)) return; // ✅ hide paid parking/garages

        var levelClass = isDcFast(row) ? 'bolt-l3' : 'bolt-l2';

        var icon = L.divIcon({
          className: 'bolt-divicon',
          iconSize: [26, 26],
          html: boltIconHTML(levelClass, row.operator)
        });

        var marker = L.marker([lat, lon], { icon: icon })
          .bindPopup(makePopup(row));

        clusterGroup.addLayer(marker);
        any = true;
      });

      if (any) {
        var b = clusterGroup.getBounds();
        if (b && b.isValid()) map.fitBounds(b.pad(0.12));
      }
    }

    // ======================
    // LOAD CSV
    // ======================
    Papa.parse(OCM_CSV_URL, {
      download: true,
      header: true,
      dynamicTyping: true,
      complete: function(res) {
        allRows = (res && res.data ? res.data : []).filter(function(r){
          return r && typeof r.lat === 'number' && typeof r.lon === 'number' && !isNaN(r.lat) && !isNaN(r.lon);
        });

        var opsSet = {};
        allRows.forEach(function(r){
          var op = normOperator(r.operator);
          if (op) opsSet[op] = true;
        });
        var ops = Object.keys(opsSet).sort(function(a,b){ return a.localeCompare(b); });

        addOperatorControl(ops);
        addLevelControl();
        render();
      },
      error: function(err) {
        console.error('CSV parse error:', err);
        alert('Could not load ocm_nyc_retail_locations.csv. Make sure it is in the same folder as index.html.');
      }
    });
  </script>
</body>
</html>
