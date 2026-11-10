// @ts-check
(function () {
  "use strict";
  // Argus dashboard plugin. Hand-written, no build step.
  // Tab: the Argus UI in a same-origin iframe (keeps its own CSS and hash routing out of the host).
  // skills:top slot: a one-line banner with what needs review.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.__HERMES_PLUGINS__;
  if (!SDK || !PLUGINS) return;
  const React = SDK.React;
  const { useState, useEffect, useRef } = SDK.hooks;
  const fetchJSON = SDK.fetchJSON;
  const h = React.createElement;

  // .../dashboard-plugins[/@profile]/argus/dist/index.js -> .../dashboard-plugins[/@profile]/argus/
  const script = document.currentScript instanceof HTMLScriptElement && document.currentScript.src;
  const assetBase = script ? new URL("../", script).href
    : (window.__HERMES_BASE_PATH__ || "") + "/dashboard-plugins/argus/";
  const appUrl = assetBase + "app/index.html";
  const tabHref = (window.__HERMES_BASE_PATH__ || "") + "/argus";

  function ArgusPage() {
    /** @type {Hermes.RefObject<HTMLIFrameElement | null>} */
    const ref = useRef(null);
    const [height, setHeight] = useState(600);
    useEffect(function () {
      function fit() {
        if (!ref.current) return;
        const top = ref.current.getBoundingClientRect().top;
        setHeight(Math.max(320, window.innerHeight - top - 8));
      }
      fit();
      window.addEventListener("resize", fit);
      return function () { window.removeEventListener("resize", fit); };
    }, []);
    return h("iframe", {
      ref: ref,
      src: appUrl,
      title: "Argus",
      style: { width: "100%", height: height + "px", border: 0, display: "block", background: "transparent" },
    });
  }

  function SkillsBanner() {
    const [s, setS] = useState(/** @type {Argus.Status | null} */ (null));
    useEffect(function () {
      let live = true;
      // the /status payload (argus/exporttypes.py Status), as the backend types it
      fetchJSON("/api/plugins/argus/status").then(function (r) { if (live) setS(/** @type {Argus.Status} */ (r)); }).catch(function () {});
      return function () { live = false; };
    }, []);
    if (!s || (!s.edits_without_evidence && !s.pending_proposals)) return null;
    // one line, numbers first: what waits for review, and the link to it
    /** @type {string[]} */
    const parts = [];
    if (s.edits_without_evidence) parts.push(s.edits_without_evidence + " skill edit" + (s.edits_without_evidence === 1 ? "" : "s") + " without evidence");
    if (s.pending_proposals) parts.push(s.pending_proposals + " proposed edit" + (s.pending_proposals === 1 ? "" : "s"));
    return h("div", {
      style: { display: "flex", gap: "10px", alignItems: "baseline", padding: "4px 10px", margin: "0 0 12px",
        borderLeft: "2px solid currentColor", opacity: 0.85, fontSize: "13px", whiteSpace: "nowrap" },
    }, h("b", null, "Argus"), h("span", { style: { overflow: "hidden", textOverflow: "ellipsis" } }, parts.join(" \u00b7 ")),
      h("a", { href: tabHref, style: { marginLeft: "auto", textDecoration: "underline" } }, "review \u2192"));
  }

  PLUGINS.register("argus", ArgusPage);
  PLUGINS.registerSlot("argus", "skills:top", SkillsBanner);
})();
