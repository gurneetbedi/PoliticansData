/**
 * Shared state-name label overlay for any Leaflet India choropleth.
 *
 * Includes:
 *   - 2-letter state / UT abbreviations (STATE_ABBR)
 *   - Hand-picked centroid overrides for states whose geometric mean
 *     lands awkwardly (LABEL_OVERRIDES)
 *   - FORCE_ABBR set for states that always use the 2-letter code
 *     (long names, dense clusters, tiny polygons)
 *   - polygonCentroid(feature, fallbackBounds) — proper visual centre
 *     via vertex average of the largest sub-polygon (handles offshore
 *     islands correctly)
 *   - addStateLabel(feature, layer, name, indiaMap, opts) — adds a
 *     divIcon marker to the map. Pass optional findKpi() and isUT()
 *     hooks so we know which states get labels.
 *   - setupStateLabelsPane(indiaMap) — create the z-ordered pane so
 *     labels sit on top of polygons but below the tooltip layer.
 *
 * Usage in a template's map JS:
 *   setupStateLabelsPane(indiaMap);
 *   function onEachFeature(feature, layer) {
 *     const name = feature.properties.NAME_1 || ...;
 *     addStateLabel(feature, layer, name, indiaMap, {
 *       findKpi: findKpiForState,   // required
 *       isUT:    isNoAssemblyUT,    // optional; defaults to () => false
 *     });
 *     // ...your hover/click handlers...
 *   }
 *
 * CSS lives in app/templates/base.html under `.state-label-marker`.
 * Loaded automatically on every page.
 */

(function () {
  // 2-letter codes (ECI convention; matches app/states.py .code fields).
  const STATE_ABBR = {
    "Andhra Pradesh": "AP",   "Arunachal Pradesh": "AR",
    "Assam": "AS",            "Bihar": "BR",
    "Chhattisgarh": "CG",     "Delhi": "DL",
    "NCT of Delhi": "DL",     "Goa": "GA",
    "Gujarat": "GJ",          "Haryana": "HR",
    "Himachal Pradesh": "HP", "Jharkhand": "JH",
    "Jammu & Kashmir": "J&K", "Jammu and Kashmir": "J&K",
    "Karnataka": "KA",        "Kerala": "KL",
    "Ladakh": "LA",           "Lakshadweep": "LD",
    "Madhya Pradesh": "MP",   "Maharashtra": "MH",
    "Manipur": "MN",          "Meghalaya": "ML",
    "Mizoram": "MZ",          "Nagaland": "NL",
    "Odisha": "OD",           "Punjab": "PB",
    "Rajasthan": "RJ",        "Sikkim": "SK",
    "Tamil Nadu": "TN",       "Telangana": "TG",
    "Tripura": "TR",          "Uttar Pradesh": "UP",
    "Uttarakhand": "UK",      "West Bengal": "WB",
    "Andaman & Nicobar": "A&N", "Andaman and Nicobar": "A&N",
    "Andaman & Nicobar Islands": "A&N",
    "Chandigarh": "CH",
    "Dadra and Nagar Haveli and Daman and Diu": "DN&DD",
    "Puducherry": "PY",
  };

  // Hand-picked centroids for states where the polygon average lands
  // awkwardly (L-shapes, offshore islands, NE cluster crowding).
  const LABEL_OVERRIDES = {
    "Maharashtra":       [19.6, 76.0],
    "Karnataka":         [15.0, 76.3],
    "Andhra Pradesh":    [15.9, 79.5],
    "Odisha":            [20.6, 84.4],
    "Bihar":             [25.6, 85.7],
    "Gujarat":           [22.7, 71.5],
    "Kerala":            [10.4, 76.4],
    "West Bengal":       [23.3, 87.8],
    "Assam":             [26.4, 93.0],
    "Jammu and Kashmir": [33.9, 75.5],
    "Himachal Pradesh":  [31.8, 77.0],
    "Uttarakhand":       [30.1, 79.3],
    "Chhattisgarh":      [21.0, 82.2],
    "Jharkhand":         [23.5, 85.5],
    "Madhya Pradesh":    [23.5, 78.5],
    "Rajasthan":         [26.5, 73.8],
    "Tamil Nadu":        [11.0, 78.5],
    "Telangana":         [17.9, 79.2],
    "Arunachal Pradesh": [28.4, 94.5],
    // NE cluster spread
    "Sikkim":            [27.6, 88.5],
    "Meghalaya":         [25.6, 91.5],
    "Manipur":           [24.6, 94.0],
    "Mizoram":           [23.2, 92.9],
    "Nagaland":          [26.1, 94.4],
    "Tripura":           [23.7, 91.7],
    // Ladakh — huge state but visually elongated; place in Leh area
    "Ladakh":            [34.2, 77.5],
  };

  // Always use abbreviation regardless of polygon width — names that
  // reliably overflow their state or crash into neighbouring labels.
  const FORCE_ABBR = new Set([
    "Madhya Pradesh", "Andhra Pradesh", "Arunachal Pradesh",
    "Sikkim", "Meghalaya", "Manipur", "Mizoram", "Nagaland", "Tripura",
    "Delhi", "Goa", "Haryana", "Himachal Pradesh", "Uttarakhand",
    "Jammu and Kashmir", "Chhattisgarh", "Jharkhand", "Telangana",
  ]);

  // Skip labels for micro-UTs entirely.
  const LABEL_SKIP = new Set([
    "Lakshadweep", "Chandigarh", "Puducherry",
    "Dadra and Nagar Haveli and Daman and Diu",
    "Dadra & Nagar Haveli and Daman and Diu",
  ]);

  // Compute the visual centroid of a GeoJSON polygon/multipolygon.
  // For MultiPolygons we use the largest sub-polygon (main landmass) to
  // avoid the label landing on an outlying island. Falls back to
  // bounding-box centre on any parsing weirdness.
  function polygonCentroid(feature, fallbackBounds) {
    try {
      const geom = feature.geometry;
      if (!geom) throw 0;
      let rings = null;
      if (geom.type === 'Polygon') {
        rings = geom.coordinates[0];
      } else if (geom.type === 'MultiPolygon') {
        let best = null, bestArea = -1;
        for (const poly of geom.coordinates) {
          const r = poly[0];
          let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
          for (const [x,y] of r) {
            if (x<minX) minX=x; if (y<minY) minY=y;
            if (x>maxX) maxX=x; if (y>maxY) maxY=y;
          }
          const a = (maxX-minX) * (maxY-minY);
          if (a > bestArea) { bestArea = a; best = r; }
        }
        rings = best;
      }
      if (!rings || rings.length === 0) throw 0;
      let sx=0, sy=0, n=0;
      for (const [x,y] of rings) { sx += x; sy += y; n++; }
      return [sy/n, sx/n];
    } catch (e) {
      const c = fallbackBounds.getCenter();
      return [c.lat, c.lng];
    }
  }

  // Create a Leaflet pane that renders state labels above the polygons.
  function setupStateLabelsPane(indiaMap) {
    if (indiaMap.getPane('stateLabelsPane')) return;   // idempotent
    indiaMap.createPane('stateLabelsPane');
    indiaMap.getPane('stateLabelsPane').style.zIndex = 620;
  }

  // Add a permanent state-name label to a polygon layer.
  // `opts.findKpi(name)` returns truthy if the state has data (label shown).
  // `opts.isUT(name)` returns true for no-assembly UTs (Ladakh etc. also get labels).
  function addStateLabel(feature, layer, name, indiaMap, opts) {
    opts = opts || {};
    const findKpi = opts.findKpi || function () { return null; };
    const isUT    = opts.isUT    || function () { return false; };

    try {
      if (LABEL_SKIP.has(name)) return;

      const kpi = findKpi(name);
      const noAssembly = isUT(name);
      // Label eligibility:
      //  • Regular states with KPI → labelled
      //  • No-assembly UTs like Ladakh → labelled as geographic reference
      //  • Anything else (untracked / not-loaded states) → no label
      if (!kpi && !noAssembly) return;

      const bounds = layer.getBounds();
      const widthDeg = Math.abs(bounds.getEast() - bounds.getWest());
      const approxCharDeg = 0.55;
      const fitsFull = !FORCE_ABBR.has(name)
                       && (name.length * approxCharDeg) <= widthDeg;
      const label = fitsFull ? name : (STATE_ABBR[name] || name);

      const center = LABEL_OVERRIDES[name]
        || polygonCentroid(feature, bounds);

      L.marker(center, {
        icon: L.divIcon({
          className: 'state-label-marker',
          html: `<span class="state-label-text">${label.toUpperCase()}</span>`,
          iconSize: null,
          iconAnchor: [0, 0],
        }),
        interactive: false,
        keyboard: false,
        pane: 'stateLabelsPane',
      }).addTo(indiaMap);
    } catch (e) { /* geometry glitch — skip label silently */ }
  }

  // Expose to the page.
  window.LokvaniStateLabels = {
    STATE_ABBR,
    LABEL_OVERRIDES,
    FORCE_ABBR,
    LABEL_SKIP,
    polygonCentroid,
    setupStateLabelsPane,
    addStateLabel,
  };
})();
