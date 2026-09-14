// Rung matching and element description, evaluated inside one frame by PlaywrightSurface.
// The recorder and the resolver both go through these functions, so a rung means the same thing
// when it is written and when it is replayed. Never returns selectors; only elements and facts.
(arg) => {
  const norm = (s) => (s || "").replace(/[\u2013\u2014]/g, "-").replace(/\s+/g, " ").trim();
  const fold = (s) => norm(s).toLowerCase();
  const tag = (el) => el.tagName.toLowerCase();
  const rect = (el) => el.getBoundingClientRect();
  // Rendered, not visibility:hidden, not under opacity:0, and not pushed entirely off the page.
  const visible = (el) => {
    if (!(el instanceof Element)) return false;
    const r = rect(el);
    if (r.width <= 0 || r.height <= 0) return false;
    if (r.right + scrollX <= 0 || r.bottom + scrollY <= 0) return false;
    return el.checkVisibility({ opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true });
  };
  const text = (el) => norm(el.innerText || "");
  const CLICK_INPUTS = ["submit", "button", "reset", "image"];
  const TEXT_INPUTS = ["text", "password", "email", "search", "tel", "url", "number"];
  const inputType = (el) =>
    tag(el) === "input" ? (el.getAttribute("type") || "text").toLowerCase() : null;
  const isInput = (el) =>
    tag(el) === "textarea" || (tag(el) === "input" && TEXT_INPUTS.includes(inputType(el)));
  const isSelect = (el) => tag(el) === "select";
  const isCheckbox = (el) => tag(el) === "input" && inputType(el) === "checkbox";
  const isFormControl = (el) => isInput(el) || isSelect(el) || isCheckbox(el);
  const isClickable = (el) =>
    el.hasAttribute("onclick") ||
    (tag(el) === "a" && el.hasAttribute("href")) ||
    tag(el) === "button" ||
    (tag(el) === "input" && CLICK_INPUTS.includes(inputType(el))) ||
    ["button", "link"].includes(el.getAttribute("role") || "");
  const isControl = (el) => isFormControl(el) || isClickable(el);
  const nativeRole = (el) => {
    const explicit = (el.getAttribute("role") || "").split(" ")[0];
    if (explicit) return explicit;
    const t = tag(el);
    if (t === "td") return "cell";
    if (t === "th") return "columnheader";
    if (t === "a" && el.hasAttribute("href")) return "link";
    if (t === "button") return "button";
    if (t === "select") return el.multiple || el.size > 1 ? "listbox" : "combobox";
    if (t === "textarea") return "textbox";
    if (t === "img") return "img";
    if (t === "input") {
      const it = inputType(el);
      if (CLICK_INPUTS.includes(it)) return "button";
      if (it === "checkbox") return "checkbox";
      if (it === "radio") return "radio";
      if (it === "search") return "searchbox";
      if (it === "number") return "spinbutton";
      if (TEXT_INPUTS.includes(it)) return "textbox";
    }
    if (/^h[1-6]$/.test(t)) return "heading";
    return null;
  };
  // A wrapper around exactly one control with the same visible text stands for that control, so
  // the accessibility tree's cell "Find" and the span that owns the click handler are one target.
  const canon = (el) => {
    if (isControl(el)) return el;
    const inner = [...el.querySelectorAll("*")].filter((c) => visible(c) && isControl(c));
    if (inner.length === 1 && text(inner[0]) === text(el)) return inner[0];
    return el;
  };
  const role = (el) => {
    const own = nativeRole(el);
    if (own) return own;
    const parent = el.parentElement;
    return parent && canon(parent) === el ? nativeRole(parent) : null;
  };
  // A label's text without the text of controls inside it (a wrapped select's options, say).
  const labelText = (lab) => {
    const copy = lab.cloneNode(true);
    copy.querySelectorAll("input, select, textarea, button").forEach((c) => c.remove());
    return norm(copy.textContent);
  };
  const labelFor = (el) =>
    el.closest("label") || (el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null);
  // A close approximation of WAI-ARIA accname: labelledby, aria-label, label, placeholder, title,
  // content (with image alt text), title.
  const accName = (el) => {
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const joined = norm(labelledBy.split(/\s+/).map((id) => document.getElementById(id))
        .filter(Boolean).map((e) => norm(e.textContent)).join(" "));
      if (joined) return joined;
    }
    const aria = norm(el.getAttribute("aria-label") || "");
    if (aria) return aria;
    if (isFormControl(el)) {
      const lab = labelFor(el);
      const fromLabel = lab ? labelText(lab) : "";
      return fromLabel || norm(el.getAttribute("placeholder") || el.getAttribute("title") || "");
    }
    if (tag(el) === "input") return norm(el.value || el.getAttribute("alt") || "");
    if (tag(el) === "img") return norm(el.getAttribute("alt") || "");
    const content = text(el) || norm([...el.querySelectorAll("img[alt]")].map((i) => i.getAttribute("alt")).join(" "));
    return content || norm(el.getAttribute("title") || "");
  };
  const uniq = (els) => [...new Set(els.filter(Boolean).map(canon))].filter(visible);
  const everything = () => (document.body ? [...document.body.querySelectorAll("*")] : []);
  // Deepest visible elements whose text equals the wanted text (case-folded).
  const textMatches = (wanted) => {
    const w = fold(wanted);
    return everything().filter(
      (el) =>
        visible(el) &&
        fold(el.innerText) === w &&
        ![...el.children].some((c) => visible(c) && fold(c.innerText) === w),
    );
  };
  const kindOk = (el, control) =>
    control === "textbox" ? isInput(el) : control === "combobox" ? isSelect(el) : isCheckbox(el);
  const following = (a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
  const overlapH = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0;
  const overlapV = (a, b) => Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0;
  const controls = (scope) =>
    [...scope.querySelectorAll("input, select, textarea")].filter((c) => visible(c));
  // Every element tied with the first one on a key, so position ties fail the one-match rule.
  const tiedWithFirst = (sorted, key) =>
    sorted.length ? sorted.filter((el) => Math.abs(key(el) - key(sorted[0])) < 1) : [];

  const byLabel = (label, relation, control) => {
    const out = [];
    if (relation === "wrapped") {
      for (const lab of document.querySelectorAll("label")) {
        if (!visible(lab) || fold(labelText(lab)) !== fold(label)) continue;
        const forId = lab.getAttribute("for");
        const found = forId ? [document.getElementById(forId)] : controls(lab);
        out.push(...found.filter((c) => c && kindOk(c, control)));
      }
      return out;
    }
    for (const lab of textMatches(label)) {
      const lr = rect(lab);
      if (relation === "same_row") {
        const row = lab.closest("tr");
        if (row) {
          const hit = controls(row).find((c) => kindOk(c, control) && following(lab, c));
          if (hit) out.push(hit);
        } else {
          const band = controls(document)
            .filter((c) => kindOk(c, control) && rect(c).left >= lr.right - 1 && overlapV(rect(c), lr))
            .sort((a, b) => rect(a).left - rect(b).left);
          out.push(...tiedWithFirst(band, (c) => rect(c).left));
        }
      } else {
        const below = controls(document)
          .filter((c) => kindOk(c, control) && rect(c).top >= lr.bottom - 1 && overlapH(rect(c), lr))
          .sort((a, b) => rect(a).top - rect(b).top);
        out.push(...tiedWithFirst(below, (c) => rect(c).top));
      }
    }
    return out;
  };

  const kindMatch = (el, kind) => {
    if (kind === "input") return isInput(el);
    if (kind === "select") return isSelect(el);
    if (kind === "clickable") return isClickable(el);
    const own = text(el);
    return own !== "" && !isControl(el) && ![...el.children].some((c) => visible(c) && text(c) === own);
  };
  const inDirection = (r, a, dir) => {
    if (dir === "right") return r.left >= a.right - 1 && overlapV(r, a);
    if (dir === "left") return r.right <= a.left + 1 && overlapV(r, a);
    if (dir === "below") return r.top >= a.bottom - 1 && overlapH(r, a);
    return r.bottom <= a.top + 1 && overlapH(r, a);
  };
  const distance = (r, a, dir) => {
    if (dir === "right") return [r.left - a.right, Math.abs(r.top - a.top)];
    if (dir === "left") return [a.left - r.right, Math.abs(r.top - a.top)];
    if (dir === "below") return [r.top - a.bottom, Math.abs(r.left - a.left)];
    return [a.top - r.bottom, Math.abs(r.left - a.left)];
  };
  // Candidates in the anchor's direction with their distance, nearest first. same_row means the
  // anchor's table row, or off tables the anchor's vertical band.
  const ranked = (anchor, dir, sameRow, kind) => {
    const row = sameRow ? anchor.closest("tr") : null;
    const scope = row || document.body;
    const a = rect(anchor);
    const pool = [...new Set([...scope.querySelectorAll("*")].filter((el) => visible(el) && kindMatch(el, kind)).map(canon))];
    return pool
      .filter((el) => visible(el) && !el.contains(anchor) && !anchor.contains(el) && inDirection(rect(el), a, dir))
      .filter((el) => !sameRow || row || overlapV(rect(el), a))
      .map((el) => [el, distance(rect(el), a, dir)])
      .sort((x, y) => x[1][0] - y[1][0] || x[1][1] - y[1][1]);
  };
  const byAnchor = (p) => {
    const anchors = textMatches(p.anchor_text);
    if (anchors.length !== 1) return [];
    const list = ranked(anchors[0], p.direction, p.same_row, p.target_kind);
    if (list.length < p.nth) return [];
    const slot = list[p.nth - 1][1];
    return list
      .filter(([, d]) => Math.abs(d[0] - slot[0]) < 1 && Math.abs(d[1] - slot[1]) < 1)
      .map(([el]) => el);
  };
  const byBox = (p) => {
    const el = document.elementFromPoint((p.x + p.w / 2) * innerWidth, (p.y + p.h / 2) * innerHeight);
    // A frame element is a window into another document, never a target in this one.
    return el && !["frame", "iframe", "frameset", "html", "body"].includes(tag(el)) ? [el] : [];
  };

  const describe = (el) => {
    const r = rect(el);
    const kind = isInput(el) ? "input" : isSelect(el) ? "select" : isCheckbox(el) ? "checkbox"
      : isClickable(el) ? "clickable" : text(el) ? "text" : "other";
    const formControl = isFormControl(el);
    const labels = [];
    const lab = formControl ? labelFor(el) : null;
    if (lab && labelText(lab)) labels.push({ label: labelText(lab), relation: "wrapped" });
    const row = el.closest("tr");
    const cells = row ? [...row.children].filter((c) => ["TD", "TH"].includes(c.tagName)) : [];
    const own = cells.findIndex((c) => c.contains(el));
    if (formControl && own > 0) {
      for (let i = own - 1; i >= 0; i--) {
        if (text(cells[i]) && !controls(cells[i]).length) {
          labels.push({ label: text(cells[i]), relation: "same_row" });
          break;
        }
      }
    }
    const textEls = everything().filter((e) => {
      const t = text(e);
      return visible(e) && t && t.length <= 80 && !isControl(e) && !e.contains(el) && !el.contains(e)
        && ![...e.children].some((c) => visible(c) && text(c) === t);
    });
    if (formControl && !row) {
      const left = textEls
        .filter((e) => rect(e).right <= r.left + 1 && overlapV(rect(e), r))
        .sort((a, b) => rect(b).right - rect(a).right);
      if (left.length) labels.push({ label: text(left[0]), relation: "same_row" });
    }
    if (formControl) {
      const above = textEls
        .filter((e) => rect(e).bottom <= r.top + 1 && r.top - rect(e).bottom < 40 && overlapH(rect(e), r))
        .sort((a, b) => rect(b).bottom - rect(a).bottom);
      if (above.length) labels.push({ label: text(above[0]), relation: "below" });
    }

    const anchors = [];
    const anchorKind = kind === "checkbox" || kind === "other" ? null : kind;
    const tryAnchor = (e, dir, sameRow) => {
      if (!anchorKind || !e || textMatches(text(e)).length !== 1 || text(e) === text(el)) return;
      const nth = ranked(e, dir, sameRow, anchorKind).findIndex(([c]) => c === el) + 1;
      if (nth >= 1) anchors.push({ anchor_text: text(e), direction: dir, same_row: sameRow, nth });
    };
    const inCell = (cell) => textEls.find((e) => cell.contains(e)) || (text(cell) ? cell : null);
    if (own >= 0) {
      for (let i = own - 1; i >= 0; i--) if (text(cells[i])) { tryAnchor(inCell(cells[i]), "right", true); break; }
      for (let i = own + 1; i < cells.length; i++) if (text(cells[i])) { tryAnchor(inCell(cells[i]), "left", true); break; }
    } else {
      const nearest = (dir) => textEls
        .filter((e) => inDirection(r, rect(e), dir))
        .sort((a, b) => distance(r, rect(a), dir)[0] - distance(r, rect(b), dir)[0])[0];
      tryAnchor(nearest("right"), "right", true);
      tryAnchor(nearest("left"), "left", true);
    }
    const aboveAnchor = textEls
      .filter((e) => rect(e).bottom <= r.top + 1 && overlapH(rect(e), r))
      .sort((a, b) => rect(b).bottom - rect(a).bottom)[0];
    tryAnchor(aboveAnchor, "below", false);
    const belowAnchor = textEls
      .filter((e) => rect(e).top >= r.bottom - 1 && overlapH(rect(e), r))
      .sort((a, b) => rect(a).top - rect(b).top)[0];
    tryAnchor(belowAnchor, "above", false);

    const w = Math.min(r.width / innerWidth, 1);
    const h = Math.min(r.height / innerHeight, 1);
    const frameBox = w > 0 && h > 0
      ? { x: Math.max(0, Math.min(r.left / innerWidth, 1)), y: Math.max(0, Math.min(r.top / innerHeight, 1)), w, h }
      : null;
    return {
      tag: tag(el),
      role: role(el),
      name: accName(el) || null,
      own_text: formControl ? "" : text(el),
      input_type: inputType(el),
      kind,
      labels,
      anchors,
      frame_box: frameBox,
      max_length: formControl && el.maxLength > 0 ? el.maxLength : null,
    };
  };

  switch (arg.op) {
    case "canon": return uniq(arg.els);
    case "label": return uniq(byLabel(arg.label, arg.relation, arg.control));
    case "text": return uniq(textMatches(arg.text)).filter((el) => !arg.role || role(el) === arg.role);
    case "anchor": return uniq(byAnchor(arg));
    case "bbox": return uniq(byBox(arg));
    case "describe": return describe(arg.el);
    case "same": return arg.a === arg.b;
    case "click_elements": return everything().filter((el) => visible(el) && isClickable(el));
    case "click_data":
      return arg.els.map((el) => {
        const r = rect(el);
        return [r.left, r.top, r.width, r.height, text(el) || norm(el.value || "")];
      });
    case "text_of": return document.body ? norm(document.body.innerText) : "";
    case "quiet_ms": return performance.now() - (window.__cuaLastMutation || 0);
    default: throw new Error(`unknown op ${arg.op}`);
  }
}
