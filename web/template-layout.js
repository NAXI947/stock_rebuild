/* The archived research/admin shell, connected to the existing page router. */
(() => {
  "use strict";
  const body = document.body;
  const side = document.getElementById("workspace-navigation");
  const workspace = document.getElementById("workspace");
  const menu = document.getElementById("shell-menu");
  const collapse = document.getElementById("shell-collapse");
  const close = document.getElementById("shell-close");
  const backdrop = document.getElementById("shell-backdrop");
  const mobile = window.matchMedia("(max-width: 900px)");
  const modules = [...document.querySelectorAll("[data-module]")];
  const pages = [...side.querySelectorAll("[data-page]")];
  const lastPage = { research: "overview", admin: "data-updates" };
  let activeModule = "research";
  let sideOpen = false;

  function updateSession() {
    const connected = !workspace.hidden;
    const dot = document.getElementById("shell-session-dot");
    dot.classList.toggle("on", connected);
    dot.classList.toggle("admin-on", connected && activeModule === "admin");
    document.getElementById("shell-session-label").textContent = `${activeModule === "admin" ? "管理" : "研究"}会话${connected ? "已连接" : "未连接"}`;
  }

  function setModule(module) {
    activeModule = module;
    const admin = module === "admin";
    body.classList.toggle("research-shell", !admin);
    body.classList.toggle("admin-shell", admin);
    document.getElementById("research-navigation").hidden = admin;
    document.getElementById("admin-navigation").hidden = !admin;
    document.getElementById("shell-module-name").textContent = admin ? "系统管理" : "个人研究";
    document.getElementById("shell-side-label").textContent = admin ? "系统管理" : "股票研究";
    for (const button of modules) {
      const selected = button.dataset.module === module;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    }
    updateSession();
  }

  function syncPage() {
    const selected = pages.find(button => button.getAttribute("aria-current") === "page");
    if (!selected || workspace.hidden) return;
    const module = selected.closest("#admin-navigation") ? "admin" : "research";
    lastPage[module] = selected.dataset.page;
    setModule(module);
  }

  function setDrawer(open, restoreFocus = true) {
    sideOpen = open && mobile.matches;
    side.classList.toggle("open", sideOpen);
    backdrop.classList.toggle("open", sideOpen);
    body.classList.toggle("shell-drawer-open", sideOpen);
    menu.setAttribute("aria-expanded", String(sideOpen));
    menu.textContent = sideOpen ? "关闭菜单" : "菜单";
    if (sideOpen) {
      side.setAttribute("role", "dialog");
      side.setAttribute("aria-modal", "true");
      side.setAttribute("aria-label", activeModule === "admin" ? "管理导航" : "研究导航");
      close.focus();
    } else {
      side.removeAttribute("role");
      side.removeAttribute("aria-modal");
      side.removeAttribute("aria-label");
      if (restoreFocus && mobile.matches) menu.focus();
    }
  }

  menu.addEventListener("click", () => setDrawer(!sideOpen));
  close.addEventListener("click", () => setDrawer(false));
  backdrop.addEventListener("click", () => setDrawer(false));
  collapse.addEventListener("click", () => {
    const collapsed = body.classList.toggle("nav-collapsed");
    collapse.textContent = collapsed ? "展开导航" : "收起导航";
    collapse.setAttribute("aria-expanded", String(!collapsed));
  });
  for (const button of modules) {
    button.addEventListener("click", () => {
      setModule(button.dataset.module);
      if (sideOpen) setDrawer(false);
      if (!workspace.hidden) pages.find(page => page.dataset.page === lastPage[activeModule])?.click();
    });
  }
  for (const button of pages) {
    button.addEventListener("click", () => {
      if (sideOpen) setDrawer(false);
    });
  }
  document.addEventListener("keydown", event => {
    if (!sideOpen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      setDrawer(false);
    } else if (event.key === "Tab") {
      const focusable = [...side.querySelectorAll("button:not(:disabled)")].filter(node => node.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first?.focus();
      }
    }
  });
  mobile.addEventListener("change", () => setDrawer(false, false));
  new MutationObserver(syncPage).observe(side, { subtree: true, attributes: true, attributeFilter: ["aria-current"] });
  new MutationObserver(() => {
    if (workspace.hidden) {
      lastPage.research = "overview";
      lastPage.admin = "data-updates";
      setModule("research");
      if (sideOpen) setDrawer(false);
    } else syncPage();
    updateSession();
  }).observe(workspace, { attributes: true, attributeFilter: ["hidden"] });
  updateSession();
  syncPage();
})();
