// This frame has no credentials or access to the parent's document.
const report = (status) => parent.postMessage({ type: "pptx-preview-status", status }, "*");
window.addEventListener("error", () => report("error"));
window.addEventListener("unhandledrejection", () => report("error"));
let started = false;
window.addEventListener("message", async (event) => {
  if (event.source !== parent || event.data?.type !== "pptx-preview-render" || started) return;
  started = true;
  try {
    const host = document.getElementById("slides");
    const fit = () => {
      host.style.zoom = String(Math.min(1, Math.max(1, document.documentElement.clientWidth - 24) / 960));
    };
    window.addEventListener("resize", fit);
    fit();
    const previewer = window.pptxPreview.init(host, { width: 960, mode: "list" });
    await previewer.preview(event.data.buffer);
    if (!previewer.slideCount) throw new Error("Empty presentation");
    fit();
    report("ready");
  } catch {
    report("error");
  }
});
