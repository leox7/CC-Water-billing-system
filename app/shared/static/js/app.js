// Small startup code for the whole app. No framework, no inline scripts (the CSP blocks them).

// htmx would add its own <style> for the loading indicator, which the Content Security
// Policy blocks. Our own CSS styles the indicator instead (components.css).
htmx.config.includeIndicatorStyles = false;
// The CSP also blocks eval and inline scripts, so htmx must not try to use them.
htmx.config.allowEval = false;
htmx.config.allowScriptTags = false;

// htmx ignores 4xx and 5xx responses by default. Our error responses are small HTML
// fragments that say what went wrong and how to fix it, so we tell htmx to show them.
// A form can name where its error message goes with data-error-target (for example a
// spot inside a dialog), so the message does not replace what the form normally updates.
document.addEventListener("htmx:beforeSwap", function (event) {
  if (event.detail.xhr.status < 400) {
    return;
  }
  event.detail.shouldSwap = true;
  event.detail.isError = false;
  var source = event.detail.requestConfig.elt.closest("[data-error-target]");
  if (source) {
    event.detail.target = document.querySelector(source.dataset.errorTarget);
    event.detail.swapOverride = "innerHTML";
  }
});

// Confirmation dialogs (DESIGN_SYSTEM.md 7.9), built on the native <dialog> element:
// <button data-open-dialog="id-of-the-dialog"> opens one, and a button with
// data-close-dialog inside it closes it. The browser keeps focus inside an open dialog
// and closes it on Escape. When it closes, focus goes back to the button that opened it.
var lastDialogOpenerId = null;

document.addEventListener("click", function (event) {
  var opener = event.target.closest("[data-open-dialog]");
  if (opener) {
    lastDialogOpenerId = opener.id || null;
    document.getElementById(opener.dataset.openDialog).showModal();
    return;
  }
  var closer = event.target.closest("[data-close-dialog]");
  if (closer) {
    closer.closest("dialog").close();
  }
});

document.addEventListener("close", function (event) {
  if (event.target.tagName === "DIALOG" && lastDialogOpenerId) {
    var opener = document.getElementById(lastDialogOpenerId);
    if (opener) {
      opener.focus();
    }
  }
}, true);

// A button outside a dialog (Reactivate, say) can start a change by itself. Remember it,
// so focus can come back to the same place when its row is replaced.
document.addEventListener("htmx:beforeRequest", function () {
  var active = document.activeElement;
  if (active && active.id && !active.closest("dialog")) {
    lastDialogOpenerId = active.id;
  }
});

// After htmx replaces a row (and the dialog inside it), put focus back on the button with
// the same id in the new row, so a keyboard user is not sent back to the top of the page.
document.addEventListener("htmx:afterSettle", function () {
  if (lastDialogOpenerId && document.activeElement === document.body) {
    var opener = document.getElementById(lastDialogOpenerId);
    if (opener) {
      opener.focus();
    }
    lastDialogOpenerId = null;
  }
});

// Success messages (DESIGN_SYSTEM.md 7.10) close themselves after about 6 seconds, or
// straight away with their close button.
htmx.onLoad(function (element) {
  // htmx calls this with each new piece of the page, which may be the message itself
  var messages = Array.from(element.querySelectorAll("[data-auto-dismiss]"));
  if (element.matches("[data-auto-dismiss]")) {
    messages.push(element);
  }
  messages.forEach(function (message) {
    setTimeout(function () {
      message.remove();
    }, 6000);
  });
});

document.addEventListener("click", function (event) {
  var closeButton = event.target.closest("[data-dismiss-message]");
  if (closeButton) {
    closeButton.closest(".message").remove();
  }
});

// Show or hide a password: <button data-toggle-password="id-of-the-password-input">
document.addEventListener("click", function (event) {
  var toggle = event.target.closest("[data-toggle-password]");
  if (!toggle) {
    return;
  }
  var input = document.getElementById(toggle.dataset.togglePassword);
  var isShowing = input.type === "text";
  input.type = isShowing ? "password" : "text";
  toggle.textContent = isShowing ? "Show" : "Hide";
  toggle.setAttribute("aria-pressed", isShowing ? "false" : "true");
});

// A normal form (not one sent by htmx) shows its working state when it is sent, and
// cannot be sent twice by a second press (SECURITY.md MON-2).
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (form.hasAttribute("hx-post")) {
    return;
  }
  if (form.dataset.submitting === "true") {
    event.preventDefault();
    return;
  }
  form.dataset.submitting = "true";
  var button = form.querySelector("button[type=submit]");
  if (button) {
    button.classList.add("is-loading");
  }
});

// A page brought back with the Back button gets its buttons back.
window.addEventListener("pageshow", function () {
  document.querySelectorAll("form[data-submitting]").forEach(function (form) {
    delete form.dataset.submitting;
    var button = form.querySelector("button[type=submit]");
    if (button) {
      button.classList.remove("is-loading");
    }
  });
});
