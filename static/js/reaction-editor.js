(() => {
  'use strict';

  // The backend is the source of all reaction geometry.  We keep its SVG
  // viewBox/width/height untouched and only correct typography for conditions
  // above/below the arrow: H2SO4 -> H₂SO₄ and NH4+ -> NH₄⁺.
  let latestSvg = null;
  let installed = false;

  const NS = 'http://www.w3.org/2000/svg';

  function b64Decode(value) {
    const bytes = Uint8Array.from(atob(value), c => c.charCodeAt(0));
    return new TextDecoder().decode(bytes);
  }

  function b64Encode(value) {
    const bytes = new TextEncoder().encode(value);
    let binary = '';
    for (const b of bytes) binary += String.fromCharCode(b);
    return btoa(binary);
  }

  function chemicalTspans(text, doc, fontSize) {
    // Strip ^ and raise following tokens as superscripts.
    // Digits following an element/group are subscripts. Charges are superscripts.
    const clean = String(text || '').replace(/\^([^\s\^]+)/g, 'SUPER_$1');
    const tokens = clean.match(/[A-Za-z]+|\d+|[+-]+|SUPER_[^\s]+|[^A-Za-z\d+-]+/g) || [];
    const frag = doc.createDocumentFragment();
    let previousWasFormula = false;

    for (let token of tokens) {
      if (token === '^') continue;
      let isSuper = false;
      if (token.startsWith('SUPER_')) {
        token = token.slice(6);
        isSuper = true;
      }
      const span = doc.createElementNS(NS, 'tspan');
      span.textContent = token;
      if (isSuper || (/^[+-]+$/.test(token) && previousWasFormula)) {
        span.setAttribute('font-size', String(Math.round(fontSize * 0.62)));
        span.setAttribute('baseline-shift', 'super');
      } else if (/^\d+$/.test(token) && previousWasFormula) {
        span.setAttribute('font-size', String(Math.round(fontSize * 0.62)));
        span.setAttribute('baseline-shift', 'sub');
      }
      frag.appendChild(span);
      previousWasFormula = /[A-Za-z\d]$/.test(token);
    }
    return frag;
  }

  function improveSvg(svgText) {
    try {
      const doc = new DOMParser().parseFromString(svgText, 'image/svg+xml');
      if (doc.querySelector('parsererror')) return svgText;

      doc.querySelectorAll('text').forEach(el => {
        if (el.children.length) return;
        const value = (el.textContent || '').trim();
        if (!value) return;
        const size = parseFloat(el.getAttribute('font-size') || '0');

        // In render_reaction_svg() arrow annotations are the 58 px normal text
        // and 39 px caret superscript text. Scale only these, not coefficients,
        // operators, formulas, or RDKit molecular drawings.
        if (size === 58 || size === 39) {
          const target = size === 58 ? 74 : 48;
          el.setAttribute('font-size', String(target));
          if (/[A-Za-z]\d|\d[+-]$/.test(value)) {
            el.textContent = '';
            el.appendChild(chemicalTspans(value, doc, target));
          }
        }
      });

      return new XMLSerializer().serializeToString(doc.documentElement);
    } catch (_) {
      return svgText;
    }
  }

  function applySvg() {
    const image = document.getElementById('reaction-image');
    if (!image || !latestSvg) return;
    try {
      const source = b64Decode(latestSvg);
      const improved = improveSvg(source);
      image.src = `data:image/svg+xml;base64,${b64Encode(improved)}`;
      image.style.maxWidth = '100%';
      image.style.height = 'auto';
      image.style.objectFit = 'contain';
    } catch (_) {
      // If anything goes wrong, the normal server PNG remains available.
    }
  }

  function install() {
    if (installed || !window.fetch) return;
    installed = true;
    const originalFetch = window.fetch.bind(window);

    window.fetch = async (...args) => {
      const response = await originalFetch(...args);
      try {
        const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
        if (url.includes('/api/reaction')) {
          const data = await response.clone().json();
          if (data && data.ok && data.image_svg_base64) {
            latestSvg = data.image_svg_base64;
            // app.js first installs the PNG; run after that update.
            requestAnimationFrame(() => requestAnimationFrame(applySvg));
          }
        }
      } catch (_) {
        // Never interfere with the actual reaction request.
      }
      return response;
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install, { once: true });
  } else {
    install();
  }
})();
