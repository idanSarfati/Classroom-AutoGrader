/* Confirm destructive actions without inline handlers, so a strict CSP can be
   adopted later without touching the templates. */
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });
})();

/* Grade review: saving a score or note redirects back with a #question-… or
   #part-… fragment, so the teacher lands on the question they just marked and
   can keep going instead of being thrown to the top of the paper. The sticky
   header wraps to two rows on a phone, and its height only settles once the web
   font swaps in, so measuring once at DOMContentLoaded can land the question a
   few pixels behind the header. We measure on landing, then re-measure when the
   fonts are ready, on full load, and whenever the header changes height (a flash
   message appearing or being dismissed). The re-measure never yanks a teacher
   who has already scrolled away from the question. Without JavaScript the native
   anchor scroll still works from the CSS scroll-margin fallback. */
(function () {
  "use strict";

  function target() {
    var hash = window.location.hash;
    if (
      !hash ||
      (hash.indexOf("#question-") !== 0 && hash.indexOf("#part-") !== 0)
    ) {
      return null;
    }
    return document.getElementById(hash.slice(1));
  }

  function apply(el, scroll) {
    var header = document.querySelector(".site-header");
    var offset = header ? header.getBoundingClientRect().height + 12 : 84;
    el.style.scrollMarginTop = Math.ceil(offset) + "px";
    if (scroll) {
      el.scrollIntoView({ block: "start", behavior: "instant" });
    }
  }

  function land() {
    var el = target();
    if (el) {
      apply(el, true);
    }
  }

  /* Correct the offset after the header settles, but only scroll while the
     question is still the thing near the top of the viewport. Once the teacher
     scrolls away to keep reviewing, a later header change must not pull them
     back; we still keep the margin right for any future native anchor scroll. */
  function settle() {
    var el = target();
    if (!el) {
      return;
    }
    var top = el.getBoundingClientRect().top;
    var near = top > -48 && top < window.innerHeight * 0.6;
    apply(el, near);
  }

  function start() {
    land();
    if (document.fonts && document.fonts.ready && document.fonts.ready.then) {
      document.fonts.ready.then(settle);
    }
    window.addEventListener("load", settle);
    var header = document.querySelector(".site-header");
    if (header && typeof ResizeObserver === "function") {
      var last = header.getBoundingClientRect().height;
      new ResizeObserver(function () {
        var height = header.getBoundingClientRect().height;
        if (Math.abs(height - last) >= 1) {
          last = height;
          settle();
        }
      }).observe(header);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();

/* Show the password while typing, on request. The field itself is reused, so
   the typed value, autofill and the form name stay exactly as they were; only
   the input type changes. The pressed state drives the icon and the label. */
(function () {
  "use strict";

  function sync(button, field) {
    var visible = field.type === "text";
    button.setAttribute("aria-pressed", visible ? "true" : "false");
    var label = visible ? "הסתרת הסיסמה" : "הצגת הסיסמה";
    button.setAttribute("aria-label", label);
    button.setAttribute("title", label);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-password-toggle]");
    if (!button) {
      return;
    }
    var id = button.getAttribute("aria-controls");
    var field = document.getElementById(id);
    if (!field) {
      return;
    }
    field.type = field.type === "password" ? "text" : "password";
    sync(button, field);
    // The caret has no reason to move: the student is usually mid-word.
    if (field.type === "text" && field.value) {
      field.setSelectionRange(field.value.length, field.value.length);
    }
    field.focus();
  });
})();

/* The classroom picker: a row only submits a type once it is checked, and the
   rubric field is required only when that row becomes an assignment. */
(function () {
  "use strict";

  function syncRow(row) {
    var pick = row.querySelector('input[name="pick"]');
    var target = row.querySelector("[data-target-select]");
    var rubric = row.querySelector("[data-rubric-field]");
    var picked = !pick || (!pick.disabled && pick.checked);
    var group = row.closest("details");
    var visible = !group || group.open;

    if (rubric) {
      var isAssignment = picked && target && target.value === "assignment";
      rubric.hidden = !isAssignment;
      var input = rubric.querySelector("input");
      if (input) {
        // A required control inside a closed <details> cannot be focused, which
        // blocks submit with no visible field. The server still rejects a
        // missing rubric, so require it only while it can actually be seen.
        input.required = isAssignment && visible;
      }
    }
  }

  function updateGroupBadge(group) {
    var badge = group && group.querySelector("[data-group-selected]");
    if (!badge) {
      return;
    }
    var count = group.querySelectorAll('input[name="pick"]:checked').length;
    badge.hidden = count === 0;
    badge.textContent = "נבחרו " + count;
  }

  function syncAll() {
    document.querySelectorAll("[data-selection-row]").forEach(syncRow);
    document.querySelectorAll("[data-picker-group]").forEach(updateGroupBadge);
    document.querySelectorAll("details[data-picker-group]").forEach(function (group) {
      group.addEventListener("toggle", function () {
        group.querySelectorAll("[data-selection-row]").forEach(syncRow);
      });
    });
  }

  document.addEventListener("change", function (event) {
    var row = event.target.closest("[data-selection-row]");
    if (row) {
      syncRow(row);
      updateGroupBadge(row.closest("[data-picker-group]"));
    }
  });

  document.addEventListener("DOMContentLoaded", syncAll);
})();

/* Quick filter for long Classroom lists (courses often hold 100+ items). */
(function () {
  "use strict";

  function applyFilter(input) {
    var scope = input.closest("section") || document;
    var query = input.value.trim().toLowerCase();
    scope.querySelectorAll("[data-picker-row]").forEach(function (row) {
      row.hidden =
        query !== "" && row.textContent.toLowerCase().indexOf(query) === -1;
    });
    scope.querySelectorAll("details[data-picker-group]").forEach(function (group) {
      var rows = group.querySelectorAll("[data-picker-row]");
      var visible = Array.prototype.some.call(rows, function (row) {
        return !row.hidden;
      });
      // An empty query must keep every topic visible, including topics that
      // hold no items yet (the course tree shows the whole syllabus). Only a
      // real search hides groups without hits; the picker has no empty groups.
      group.hidden = query !== "" && !visible;
      // Match the topics for the teacher: open the groups that contain hits,
      // and go back to the collapsed default when the filter is cleared.
      group.open = visible && query !== "";
    });
  }

  document.addEventListener("input", function (event) {
    var input = event.target.closest("[data-picker-filter]");
    if (input) {
      applyFilter(input);
    }
  });
})();

/* Course-content tree: open or close every topic at once. Scoped to the
   toolbar's own section so it never touches an unrelated picker on the page.
   The quick filter above still governs which topics are visible. */
(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var button = event.target.closest(
      "[data-course-open-all], [data-course-close-all]"
    );
    if (!button) {
      return;
    }
    var scope = button.closest("section") || document;
    var open = button.hasAttribute("data-course-open-all");
    scope.querySelectorAll("details[data-picker-group]").forEach(function (group) {
      group.open = open;
    });
  });
})();

/* A background catalog scan flips the page from "סורק…" to the cached list;
   reload only while that marker is present. */
(function () {
  "use strict";

  if (!document.querySelector("[data-catalog-pending]")) {
    return;
  }
  window.setTimeout(function () {
    window.location.reload();
  }, 4000);
})();

/* Student submission: post with fetch so the server can answer "202 Accepted"
   as soon as the job is queued, then reload into the pending state. Without
   JavaScript the form still works - the server redirects like any form. */
(function () {
  "use strict";

  document.querySelectorAll("[data-submit-form]").forEach(function (form) {
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!form.reportValidity()) {
        return;
      }
      // The autosave timer watches this flag: once a real submit is in flight
      // the draft stop saving, so an old snapshot cannot overwrite the work
      // the server is grading right now.
      form.dataset.submitting = "1";
      var status = form.querySelector("[data-submit-status]");
      var button = form.querySelector('button[type="submit"]');
      if (button) {
        button.disabled = true;
      }
      if (status) {
        status.textContent = "שולח…";
      }
      try {
        var response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          headers: { Accept: "application/json" },
          credentials: "same-origin"
        });
        var payload = {};
        try {
          payload = await response.json();
        } catch (ignored) {
          payload = {};
        }
        if (response.status === 202) {
          // An exam hand-in ends with a confirmation, never an instant score:
          // the modal says the paper was received and the teacher publishes
          // the result. Closing it reloads into the submitted page.
          if (
            form.querySelector("[data-exam-submit]") &&
            typeof window.showExamSubmittedDialog === "function"
          ) {
            if (status) {
              status.textContent = "";
            }
            window.showExamSubmittedDialog(true);
            return;
          }
          if (status) {
            status.textContent = "ההגשה נקלטה. ממתין לבדיקה…";
          }
          window.location.reload();
          return;
        }
        if (status) {
          status.textContent =
            payload.error || "השליחה נכשלה. בדקו את החיבור ונסו שוב.";
        }
      } catch (error) {
        if (status) {
          status.textContent = "השליחה נכשלה. בדקו את החיבור ונסו שוב.";
        }
      }
      form.dataset.submitting = "0";
      if (button) {
        button.disabled = false;
      }
    });
  });
})();

/* Exam hand-in confirmation. The modal opens straight after a successful
   POST, or on the page the server redirects to (?submitted=1) when the fetch
   path did not run. It never shows a score or a question - the teacher
   releases those separately. */
(function () {
  "use strict";

  var dialog = document.querySelector("[data-exam-submitted-dialog]");
  if (!dialog) {
    return;
  }
  var closeButton = dialog.querySelector("[data-exam-submitted-close]");
  var reloadOnClose = false;

  window.showExamSubmittedDialog = function (reload) {
    reloadOnClose = Boolean(reload);
    if (typeof dialog.showModal === "function") {
      try {
        dialog.showModal();
        return;
      } catch (ignored) {
        // A browser without showModal still gets the visible fallback below.
      }
    }
    dialog.setAttribute("open", "");
  };

  var close = function () {
    if (typeof dialog.close === "function" && dialog.open) {
      dialog.close();
    } else {
      dialog.removeAttribute("open");
    }
    if (reloadOnClose) {
      reloadOnClose = false;
      window.location.reload();
    }
  };

  if (closeButton) {
    closeButton.addEventListener("click", close);
  }
  dialog.addEventListener("cancel", function (event) {
    event.preventDefault();
    close();
  });
  // Clicking the dimmed backdrop closes it; a click inside the body does not.
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) {
      close();
    }
  });

  try {
    var params = new URLSearchParams(window.location.search);
    if (params.get("submitted") === "1") {
      window.showExamSubmittedDialog(false);
      params.delete("submitted");
      var query = params.toString();
      window.history.replaceState(
        null,
        "",
        window.location.pathname +
          (query ? "?" + query : "") +
          window.location.hash
      );
    }
  } catch (ignored) {
    // Browsers without URLSearchParams keep the page as-is.
  }
})();

/* Draft autosave: keep the student's work on the server while they type, so a
   dropped connection, a shared lab computer or a closed tab does not lose the
   answer. A draft never becomes a submission - only the submit button does
   that - and the server ignores a draft older than the last submission. */
(function () {
  "use strict";

  var DEBOUNCE_MS = 1500;

  function stamp(date) {
    try {
      return date.toLocaleTimeString("he-IL", {
        hour: "2-digit",
        minute: "2-digit"
      });
    } catch (ignored) {
      return "";
    }
  }

  document.querySelectorAll("form[data-draft-url]").forEach(function (form) {
    var url = form.getAttribute("data-draft-url");
    var status = form.querySelector("[data-draft-status]");
    var timer = 0;
    var inFlight = false;
    var dirty = false;

    function show(text) {
      if (status) {
        status.textContent = text;
      }
    }

    function isSubmitting() {
      return form.dataset.submitting === "1";
    }

    function schedule() {
      if (isSubmitting()) {
        return;
      }
      dirty = true;
      window.clearTimeout(timer);
      timer = window.setTimeout(send, DEBOUNCE_MS);
    }

    async function send() {
      window.clearTimeout(timer);
      if (isSubmitting() || inFlight || !dirty) {
        return;
      }
      inFlight = true;
      dirty = false;
      show("שומר…");
      try {
        var response = await fetch(url, {
          method: "POST",
          body: new FormData(form),
          headers: { Accept: "application/json" },
          credentials: "same-origin",
          cache: "no-store"
        });
        var payload = {};
        try {
          payload = await response.json();
        } catch (ignoredPayload) {
          payload = {};
        }
        if (!response.ok) {
          throw new Error(payload.error || "שמירה נכשלה");
        }
        if (payload.saved) {
          show("נשמר אוטומטית " + stamp(new Date()));
        } else {
          show("");
        }
      } catch (error) {
        dirty = true;
        show(error.message || "הטיוטה לא נשמרה. בדקו את החיבור.");
      } finally {
        inFlight = false;
      }
    }

    // Only answer fields matter; the CSRF field and buttons never change.
    form.addEventListener("input", schedule);
    form.addEventListener("change", schedule);

    // A tab closing is the likeliest way to lose progress on a phone. sendBeacon
    // survives the unload; the regular debounced save still covers everything
    // else, so a missed beacon costs at most the last second of typing.
    window.addEventListener("pagehide", function (event) {
      if (event.persisted || isSubmitting() || !dirty || !navigator.sendBeacon) {
        return;
      }
      window.clearTimeout(timer);
      navigator.sendBeacon(url, new FormData(form));
    });
  });
})();

/* Count characters in answer boxes. While a grade is pending, a separate
   poller below watches the status without reloading the page. */
(function () {
  "use strict";

  document.querySelectorAll("[data-code-editor]").forEach(function (editor) {
    // A mixed item can hold several IDEs, so the counter must belong to this
    // editor's own toolbar, not to the first one in the item.
    var ide = editor.closest("[data-ide]");
    var scope =
      ide || editor.closest("[data-answer-block]") || editor.closest("form");
    var counter = scope ? scope.querySelector("[data-char-count]") : null;
    if (!counter) {
      return;
    }
    // The code IDE is an English surface; plain answer boxes stay Hebrew.
    var unit = ide ? " chars" : " תווים";
    var update = function () {
      counter.textContent = editor.value.length + unit;
    };
    editor.addEventListener("input", update);
    update();
  });
})();

/* Pending grade: poll a tiny status endpoint instead of reloading the page on
   a timer. A blind reload jumps to the top of the page and wipes code the
   student is still typing. Here the page reloads only when the grade actually
   leaves the queue, and never while someone is editing - in that case a button
   offers the refresh instead, so no keystroke is ever lost. */
(function () {
  "use strict";

  var marker = document.querySelector("[data-grading-pending]");
  if (!marker) {
    return;
  }
  var url = marker.getAttribute("data-status-url");
  if (!url || typeof window.fetch !== "function") {
    // Older browser: the page stays fully usable, just without auto-update.
    return;
  }

  var POLL_MS = 5000;
  var EDIT_WINDOW_MS = 10000;
  var stopped = false;
  var lastEdit = 0;

  // Any typing anywhere on the page counts: worksheet answers, the code IDE
  // and a teacher's feedback box must all survive until their owner is done.
  document.addEventListener(
    "input",
    function () {
      lastEdit = Date.now();
    },
    true
  );
  document.addEventListener(
    "change",
    function () {
      lastEdit = Date.now();
    },
    true
  );

  function isEditing() {
    var active = document.activeElement;
    if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) {
      return true;
    }
    if (active && active.isContentEditable) {
      return true;
    }
    if (Date.now() - lastEdit < EDIT_WINDOW_MS) {
      return true;
    }
    return Boolean(document.querySelector('form[data-submitting="1"]'));
  }

  function offerRefresh() {
    stopped = true;
    var note = document.createElement("div");
    note.className = "grading-done-note";
    var text = document.createElement("p");
    text.textContent = "הבדיקה הסתיימה. אפשר להציג את התוצאה כשמסיימים לכתוב.";
    var button = document.createElement("button");
    button.type = "button";
    button.className = "button button-primary button-small";
    button.textContent = "הצגת התוצאה";
    button.addEventListener("click", function () {
      window.location.reload();
    });
    note.appendChild(text);
    note.appendChild(button);
    marker.appendChild(note);
  }

  function poll() {
    if (stopped) {
      return;
    }
    fetch(url, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      cache: "no-store"
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("status probe failed");
        }
        return response.json();
      })
      .then(function (payload) {
        if (stopped) {
          return;
        }
        if (payload && payload.pending === false) {
          if (isEditing()) {
            offerRefresh();
          } else {
            window.location.reload();
          }
          return;
        }
        window.setTimeout(poll, POLL_MS);
      })
      .catch(function () {
        // A dropped probe is not a reason to reload or to stop watching;
        // back off a little so a struggling server is not hammered.
        if (!stopped) {
          window.setTimeout(poll, POLL_MS * 3);
        }
      });
  }

  window.setTimeout(poll, POLL_MS);
})();

/* Worksheet progress: tell the student how many questions already have an
   answer. Submitting remains allowed with gaps - the grader reports them. */
(function () {
  "use strict";

  document.querySelectorAll("[data-submit-form]").forEach(function (form) {
    var display = form.querySelector("[data-answered-count]");
    // Count whole questions, not individual fields: one x.3 item can be a
    // table with six answers or three code parts, and it is still one item.
    var blocks = form.querySelectorAll("[data-answer-block]");
    if (!display || !blocks.length) {
      return;
    }
    var update = function () {
      var answered = 0;
      blocks.forEach(function (block) {
        var fields = block.querySelectorAll("input, textarea, select");
        var filled = Array.prototype.some.call(fields, function (field) {
          if (field.type === "radio" || field.type === "checkbox") {
            return field.checked;
          }
          return Boolean((field.value || "").trim());
        });
        if (filled) {
          answered += 1;
        }
      });
      display.textContent = "נענו " + answered + " מתוך " + blocks.length;
    };
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    update();
  });
})();

/* Exam clock: it mirrors the server's closing time, corrected for a device
   clock that is off, and sends the paper automatically just before that
   instant. The bar sits under the sticky header and follows the answers down
   the page; inside the last ten minutes it turns red. The server still refuses
   a late hand-in on its own, so nothing here can extend the window. */
(function () {
  "use strict";

  var displays = document.querySelectorAll("[data-exam-countdown]");
  if (!displays.length) {
    return;
  }

  // The header wraps on a narrow screen, so its height is measured instead of
  // guessed: the clock must not slide underneath it.
  var header = document.querySelector(".site-header");
  var placeClock = function () {
    document.documentElement.style.setProperty(
      "--sticky-header",
      (header ? header.offsetHeight : 0) + "px"
    );
  };
  placeClock();
  window.addEventListener("resize", placeClock);

  // Anchor on the server's clock: a phone that is a few minutes fast must not
  // hand the paper in early, and a slow one must not believe it has extra time.
  // One offset per page load is enough for a sitting.
  var clockBox = displays[0].closest("[data-exam-clock]");
  var serverNow = clockBox
    ? Date.parse(clockBox.getAttribute("data-server-now") || "")
    : NaN;
  var skew = isNaN(serverNow) ? 0 : serverNow - Date.now();
  var now = function () {
    return Date.now() + skew;
  };

  var form = document.querySelector("form[data-submit-form]");
  var pad = function (value) {
    return String(value).padStart(2, "0");
  };
  var SOON = 10 * 60 * 1000;
  // The hand-in must reach the server while its window is still open, so it
  // starts a few seconds before the visible deadline.
  var LEAD = 5 * 1000;
  var autoSent = false;

  // The server demands at least one answer; sending an untouched paper would
  // only be refused and confuse the student.
  var hasAnswer = function () {
    if (!form) {
      return false;
    }
    var fields = form.querySelectorAll("input, textarea, select");
    for (var index = 0; index < fields.length; index += 1) {
      var field = fields[index];
      var type = (field.getAttribute("type") || "").toLowerCase();
      if (
        field.disabled ||
        type === "hidden" ||
        type === "submit" ||
        type === "button" ||
        type === "reset"
      ) {
        continue;
      }
      if (type === "radio" || type === "checkbox") {
        if (field.checked) {
          return true;
        }
        continue;
      }
      if (String(field.value || "").trim() !== "") {
        return true;
      }
    }
    return false;
  };

  var sendAtDeadline = function (clock) {
    if (autoSent) {
      return;
    }
    autoSent = true;
    if (!form || form.dataset.submitting === "1" || !hasAnswer()) {
      return;
    }
    var title = clock ? clock.querySelector(".exam-clock-title") : null;
    if (title) {
      title.textContent = "הזמן נגמר - שולח את המבחן";
    }
    // The submit handler posts the form, stops the draft and reloads into the
    // locked paper on success; a failure leaves its message in the submit bar.
    form.requestSubmit();
  };

  var tickOne = function (display) {
    var deadline = Date.parse(display.getAttribute("data-deadline") || "");
    if (isNaN(deadline)) {
      return;
    }
    var clock = display.closest("[data-exam-clock]");
    var left = deadline - now();
    if (left > 0 && left <= LEAD) {
      sendAtDeadline(clock);
    }
    if (left <= 0) {
      display.textContent = "הזמן תם";
      if (clock) {
        clock.classList.remove("is-soon");
        clock.classList.add("is-over");
      }
      return;
    }
    var total = Math.floor(left / 1000);
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var seconds = total % 60;
    display.textContent =
      (hours ? hours + ":" : "") + pad(minutes) + ":" + pad(seconds);
    // The teacher can extend the window while papers are open, so a page that
    // already read "time is up" has to come back when the deadline moves.
    if (clock) {
      clock.classList.remove("is-over");
      clock.classList.toggle("is-soon", left <= SOON);
      var title = clock.querySelector(".exam-clock-title");
      // A failed automatic send leaves the form ready for a manual retry.
      if (title && autoSent && form && form.dataset.submitting !== "1") {
        title.textContent = "נותר זמן למבחן";
      }
    }
  };

  var tick = function () {
    for (var index = 0; index < displays.length; index += 1) {
      tickOne(displays[index]);
    }
  };

  tick();
  window.setInterval(tick, 1000);
  document.addEventListener("visibilitychange", tick);
})();

/* A real-IDE feel for Python answers: syntax colours, line numbers, Tab and
   auto-indent. The visible colours live in a <pre> behind the real textarea,
   which stays the submitted value; both scroll together. */
(function () {
  "use strict";

  var KEYWORDS = new Set([
    "and", "as", "assert", "async", "await", "break", "case", "class",
    "continue", "def", "del", "elif", "else", "except", "finally", "for",
    "from", "global", "if", "import", "in", "is", "lambda", "match",
    "nonlocal", "not", "or", "pass", "raise", "return", "try", "while",
    "with", "yield"
  ]);
  var CONSTANTS = new Set(["None", "True", "False", "self"]);
  var BUILTINS = new Set([
    "abs", "all", "any", "bool", "callable", "chr", "dict", "dir",
    "divmod", "enumerate", "eval", "exec", "filter", "float", "format",
    "getattr", "globals", "hasattr", "hash", "help", "id", "input", "int",
    "isinstance", "iter", "len", "list", "locals", "map", "max", "min",
    "next", "open", "ord", "pow", "print", "range", "repr", "reversed",
    "round", "set", "setattr", "sorted", "str", "sum", "super", "tuple",
    "type", "vars", "zip"
  ]);
  var TOKEN_RE = /(#[^\n]*)|((?:[rRbBuUfF]{1,2})?(?:"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'))|(@[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)|(\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|([A-Za-z_]\w*)|([+\-*/%=<>!&|^~]+)|(\s+)|([\s\S])/g;

  function esc(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function highlight(source) {
    var html = "";
    var expectName = "";
    var match;
    TOKEN_RE.lastIndex = 0;
    while ((match = TOKEN_RE.exec(source)) !== null) {
      var token = match[0];
      if (match[7]) {
        html += esc(token);
        continue;
      }
      var cls = "";
      if (match[1]) {
        cls = "tok-comment";
        expectName = "";
      } else if (match[2]) {
        cls = "tok-string";
        expectName = "";
      } else if (match[3]) {
        cls = "tok-decorator";
        expectName = "";
      } else if (match[4]) {
        cls = "tok-number";
        expectName = "";
      } else if (match[5]) {
        if (expectName) {
          cls = "tok-def";
          expectName = "";
        } else if (CONSTANTS.has(token)) {
          cls = "tok-constant";
        } else if (KEYWORDS.has(token)) {
          cls = "tok-keyword";
        } else if (BUILTINS.has(token)) {
          cls = "tok-builtin";
        } else if (/^[ \t]*\(/.test(source.slice(TOKEN_RE.lastIndex))) {
          cls = "tok-call";
        }
        if (token === "def" || token === "class") {
          expectName = token;
        }
      } else if (match[6]) {
        cls = "tok-operator";
        expectName = "";
      }
      html += cls ? '<span class="' + cls + '">' + esc(token) + "</span>" : esc(token);
    }
    return html;
  }

  function sync(input, ide) {
    // The <pre> is the scrolling layer; the <code> inside it is inline and
    // cannot be scrolled on its own. Copying the offsets to the wrong element
    // left the colours still while the caret moved, so a long line drifted out
    // of alignment as soon as the textarea scrolled.
    var highlight = ide.querySelector(".ide-highlight");
    var gutter = ide.querySelector("[data-ide-gutter]");
    if (highlight) {
      highlight.scrollTop = input.scrollTop;
      highlight.scrollLeft = input.scrollLeft;
    }
    if (gutter) {
      gutter.scrollTop = input.scrollTop;
    }
  }

  function render(input, ide) {
    var code = ide.querySelector("[data-ide-highlight]");
    var gutter = ide.querySelector("[data-ide-gutter]");
    if (code) {
      code.innerHTML = highlight(input.value) + "\n";
    }
    if (gutter) {
      var lines = input.value.split("\n").length;
      var numbers = [];
      for (var index = 1; index <= lines; index += 1) {
        numbers.push(index);
      }
      gutter.textContent = numbers.join("\n");
    }
    sync(input, ide);
  }

  function insert(input, text) {
    input.setRangeText(text, input.selectionStart, input.selectionEnd, "end");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  document.querySelectorAll("[data-ide]").forEach(function (ide) {
    var input = ide.querySelector("[data-code-editor]");
    if (!input) {
      return;
    }
    var scheduled = 0;
    var update = function () {
      if (scheduled) {
        return;
      }
      scheduled = window.requestAnimationFrame(function () {
        scheduled = 0;
        render(input, ide);
      });
    };

    render(input, ide);
    input.addEventListener("input", update);
    input.addEventListener("scroll", function () {
      sync(input, ide);
    });
    window.addEventListener("resize", function () {
      sync(input, ide);
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "Tab") {
        event.preventDefault();
        if (!event.shiftKey) {
          insert(input, "    ");
          return;
        }
        var start = input.selectionStart;
        var lineStart = input.value.lastIndexOf("\n", start - 1) + 1;
        var before = input.value.slice(lineStart, start);
        var spaces = (before.match(/ +$/) || [""])[0].length;
        if (spaces) {
          input.setRangeText("", start - Math.min(4, spaces), start, "end");
          input.dispatchEvent(new Event("input", { bubbles: true }));
        }
        return;
      }

      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        var run = ide.querySelector("[data-run-code]");
        if (run) {
          event.preventDefault();
          run.click();
        }
        return;
      }

      if (
        event.key === "Enter" &&
        !event.shiftKey &&
        !event.ctrlKey &&
        !event.metaKey &&
        input.selectionStart === input.selectionEnd
      ) {
        var caret = input.selectionStart;
        var currentStart = input.value.lastIndexOf("\n", caret - 1) + 1;
        var line = input.value.slice(currentStart, caret);
        var indent = (line.match(/^[ \t]*/) || [""])[0];
        if (line.replace(/[ \t]+$/, "").endsWith(":")) {
          indent += "    ";
        }
        if (indent) {
          event.preventDefault();
          insert(input, "\n" + indent);
        }
      }
    });
  });
})();

/* IDE console: run the editor's code in an interactive sandbox session.
   Output is polled incrementally and input() prompts are answered from the
   line at the bottom of the console, without a page reload. */
(function () {
  "use strict";

  var POLL_MS = 150;

  function setStatus(output, text, kind) {
    var status = output.querySelector("[data-sandbox-status]");
    if (!status) {
      return;
    }
    status.textContent = text;
    status.classList.remove("terminal-status-ok", "terminal-status-error");
    if (kind) {
      status.classList.add("terminal-status-" + kind);
    }
  }

  function outputPlaceholder(output) {
    return output.querySelector("[data-ide-output-empty]");
  }

  function refreshPlaceholder(output) {
    var empty = outputPlaceholder(output);
    if (!empty) {
      return;
    }
    var stdout = output.querySelector("[data-sandbox-stdout]");
    var stderr = output.querySelector("[data-sandbox-stderr]");
    var hasText =
      (stdout && stdout.textContent) || (stderr && stderr.textContent);
    empty.hidden = Boolean(hasText);
  }

  function readJson(response) {
    return response.json().catch(function () {
      return {};
    });
  }

  function scrollToEnd(output) {
    output.scrollTop = output.scrollHeight;
  }

  function formatDuration(ms) {
    if (typeof ms !== "number" || isNaN(ms)) {
      return "";
    }
    if (ms < 1000) {
      return ms + " ms";
    }
    return (ms / 1000).toFixed(1) + " s";
  }

  function showResult(output, payload) {
    if (payload.timed_out) {
      setStatus(output, "Timed out", "error");
    } else if (payload.exit_code === 0) {
      setStatus(
        output,
        "Success · " + formatDuration(payload.execution_time_ms),
        "ok"
      );
    } else {
      setStatus(output, "Exited with code " + payload.exit_code, "error");
    }
    if (payload.truncated) {
      var stdout = output.querySelector("[data-sandbox-stdout]");
      if (stdout) {
        stdout.hidden = false;
        stdout.textContent += "\n[output truncated]";
      }
    }
  }

  document.querySelectorAll("[data-ide]").forEach(function (ide) {
    var form = ide.closest("form");
    var runButton = ide.querySelector("[data-run-code]");
    var editor = ide.querySelector("[data-code-editor]");
    var output = ide.querySelector("[data-sandbox-output]");
    if (!form || !runButton || !editor || !output) {
      return;
    }

    var sessionsUrl = form.getAttribute("data-sandbox-sessions-url");
    var tokenField = form.querySelector('input[name="csrf_token"]');
    var stdoutBox = output.querySelector("[data-sandbox-stdout]");
    var stderrBox = output.querySelector("[data-sandbox-stderr]");
    var inputRow = output.querySelector("[data-console-input]");
    var lineInput = output.querySelector("[data-console-line]");
    var clearButton = output.querySelector("[data-sandbox-clear]");
    if (!sessionsUrl || !tokenField || !lineInput) {
      return;
    }

    var sessionId = null;
    var active = false;
    var pollTimer = null;
    var stdoutOffset = 0;
    var stderrOffset = 0;

    function clearTimer() {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function showInput(show) {
      inputRow.hidden = !show;
      if (!show) {
        lineInput.value = "";
      }
    }

    function resetConsole() {
      clearTimer();
      stdoutOffset = 0;
      stderrOffset = 0;
      if (stdoutBox) {
        stdoutBox.textContent = "";
        stdoutBox.hidden = true;
      }
      if (stderrBox) {
        stderrBox.textContent = "";
        stderrBox.hidden = true;
      }
      output.hidden = false;
      var empty = outputPlaceholder(output);
      if (empty) {
        empty.hidden = true;
      }
    }

    function stopRemote(id) {
      var data = new FormData();
      data.append("csrf_token", tokenField.value);
      // Best effort: an abandoned session also expires on its own.
      return fetch(sessionsUrl + "/" + encodeURIComponent(id) + "/stop", {
        method: "POST",
        body: data,
        headers: { Accept: "application/json" },
        credentials: "same-origin"
      }).catch(function () {});
    }

    function applyPayload(payload) {
      if (!active) {
        return true;
      }
      if (payload.stdout && stdoutBox) {
        stdoutBox.hidden = false;
        stdoutBox.textContent += payload.stdout;
      }
      if (payload.stderr && stderrBox) {
        stderrBox.hidden = false;
        stderrBox.textContent += payload.stderr;
      }
      stdoutOffset =
        typeof payload.stdout_offset === "number"
          ? payload.stdout_offset
          : stdoutOffset + (payload.stdout || "").length;
      stderrOffset =
        typeof payload.stderr_offset === "number"
          ? payload.stderr_offset
          : stderrOffset + (payload.stderr || "").length;
      scrollToEnd(output);
      if (payload.done) {
        active = false;
        sessionId = null;
        clearTimer();
        showInput(false);
        runButton.disabled = false;
        showResult(output, payload);
        return true;
      }
      return false;
    }

    async function poll() {
      if (!active || !sessionId) {
        return;
      }
      var url =
        sessionsUrl +
        "/" +
        encodeURIComponent(sessionId) +
        "?stdout_since=" +
        stdoutOffset +
        "&stderr_since=" +
        stderrOffset;
      try {
        var response = await fetch(url, {
          headers: { Accept: "application/json" },
          credentials: "same-origin",
          cache: "no-store"
        });
        var payload = await readJson(response);
        if (!response.ok) {
          throw new Error(payload.error || "Run failed. Try again.");
        }
        if (!active) {
          return;
        }
        if (applyPayload(payload)) {
          return;
        }
        pollTimer = setTimeout(poll, POLL_MS);
      } catch (error) {
        if (!active) {
          return;
        }
        active = false;
        clearTimer();
        showInput(false);
        runButton.disabled = false;
        var failedId = sessionId;
        sessionId = null;
        if (failedId) {
          stopRemote(failedId);
        }
        setStatus(
          output,
          error.message || "Run failed. Check your connection and try again.",
          "error"
        );
      }
    }

    async function startRun() {
      if (active) {
        return;
      }
      if (!editor.value.trim()) {
        output.hidden = false;
        setStatus(output, "Write some code before running.", "error");
        editor.focus();
        return;
      }

      active = true;
      runButton.disabled = true;
      resetConsole();
      showInput(false);
      setStatus(output, "Running…");

      var data = new FormData();
      data.append("csrf_token", tokenField.value);
      data.append("code", editor.value);

      try {
        var response = await fetch(sessionsUrl, {
          method: "POST",
          body: data,
          headers: { Accept: "application/json" },
          credentials: "same-origin",
          cache: "no-store"
        });
        var payload = await readJson(response);
        if (!active) {
          if (payload.session_id) {
            stopRemote(payload.session_id);
          }
          return;
        }
        if (!response.ok || !payload.session_id) {
          throw new Error(payload.error || "Run failed. Try again.");
        }
        sessionId = payload.session_id;
        showInput(true);
        poll();
      } catch (error) {
        active = false;
        runButton.disabled = false;
        setStatus(output, error.message || "Run failed. Try again.", "error");
      }
    }

    async function sendLine() {
      if (!active || !sessionId) {
        return;
      }
      var line = lineInput.value;
      lineInput.value = "";
      // Echo like a terminal. Offsets track server output only, so this local
      // text is never subtracted from the next poll.
      if (stdoutBox) {
        stdoutBox.hidden = false;
        stdoutBox.textContent += line + "\n";
        scrollToEnd(output);
      }
      lineInput.focus();

      var data = new FormData();
      data.append("csrf_token", tokenField.value);
      data.append("line", line);
      try {
        var response = await fetch(
          sessionsUrl + "/" + encodeURIComponent(sessionId) + "/input",
          {
            method: "POST",
            body: data,
            headers: { Accept: "application/json" },
            credentials: "same-origin"
          }
        );
        var payload = await readJson(response);
        if (!response.ok) {
          throw new Error(payload.error || "Could not send input.");
        }
      } catch (error) {
        if (active) {
          setStatus(
            output,
            error.message || "Could not send input.",
            "error"
          );
        }
      }
    }

    runButton.addEventListener("click", startRun);
    // Leaving the page would otherwise keep a sandbox slot busy until the
    // server-side idle timeout. sendBeacon survives the unload. A page going
    // into the back/forward cache is only hidden, so its run stays alive.
    window.addEventListener("pagehide", function (event) {
      if (event.persisted || !sessionId || !navigator.sendBeacon) {
        return;
      }
      var data = new FormData();
      data.append("csrf_token", tokenField.value);
      navigator.sendBeacon(
        sessionsUrl + "/" + encodeURIComponent(sessionId) + "/stop",
        data
      );
    });
    lineInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        sendLine();
      }
    });
    if (clearButton) {
      clearButton.addEventListener("click", function () {
        var id = sessionId;
        active = false;
        sessionId = null;
        clearTimer();
        showInput(false);
        runButton.disabled = false;
        if (id) {
          stopRemote(id);
        }
        resetConsole();
        refreshPlaceholder(output);
        setStatus(output, "Ready");
      });
    }
  });
})();

/* Lecture slides: fetch the next slide and swap only the viewer. A full page
   load used to send the student back to the top of the page on every click;
   here the page keeps its scroll position and then lands on the new slide.
   Slide moves replace the current history entry instead of pushing a new one,
   so the browser Back button leaves the lesson viewer and returns to the page
   the student came from (for example the class tab), not to the previous
   slide. */
(function () {
  "use strict";

  var frame = document.querySelector("[data-slide-frame]");
  if (!frame || !window.fetch || !window.DOMParser || !window.history.replaceState) {
    return;
  }

  var busy = false;

  function scrollToSlide() {
    var viewer = document.getElementById("slide-viewer");
    if (!viewer) {
      return;
    }
    // The sticky header wraps to two rows on a phone, so a fixed CSS margin
    // can still hide the progress row. Measure it before scrolling.
    var header = document.querySelector(".site-header");
    var offset = header ? header.getBoundingClientRect().height + 12 : 84;
    viewer.style.scrollMarginTop = Math.ceil(offset) + "px";
    viewer.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function swap(url) {
    if (busy) {
      return;
    }
    busy = true;
    frame.setAttribute("aria-busy", "true");
    var currentIndex = frame.querySelector("details[data-slide-index]");
    var indexWasOpen = !!(currentIndex && currentIndex.open);

    try {
      var response = await fetch(url, {
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin"
      });
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      var html = await response.text();
      var next = new DOMParser()
        .parseFromString(html, "text/html")
        .querySelector("[data-slide-frame]");
      if (!next) {
        throw new Error("Slide frame missing");
      }
      frame.innerHTML = next.innerHTML;
      var nextIndex = frame.querySelector("details[data-slide-index]");
      if (nextIndex && indexWasOpen) {
        nextIndex.open = true;
      }
      if (url !== window.location.href) {
        window.history.replaceState({ slide: true }, "", url);
      }
      scrollToSlide();
    } catch (error) {
      // Any failure falls back to a normal navigation, which always works.
      // replace() keeps the Back button pointed at the previous page instead
      // of at the slide the student just left.
      window.location.replace(url);
    } finally {
      busy = false;
      frame.removeAttribute("aria-busy");
    }
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest("a[data-slide-link]");
    if (!link || event.defaultPrevented) {
      return;
    }
    if (event.button !== 0 || event.metaKey || event.ctrlKey) {
      return;
    }
    if (event.shiftKey || event.altKey) {
      return;
    }
    if (link.target && link.target !== "_self") {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    var url = link.href;
    swap(url);
  }, true);

  // No popstate handler: slide moves never add history entries, so Back is
  // handled by the browser leaving the viewer for the previous page.
})();

/* Pick lists: a master checkbox toggles every row in the form, and a live
   count tells the teacher how many rows the save will cover. The field name
   comes from data-check-all (empty = the model-check "submission_ids"), so
   the same behaviour serves the hand-in picker and the personal-window
   student picker. No inline handlers, so a strict CSP can be adopted later
   without touching this. */
(function () {
  "use strict";

  function fieldName(form) {
    var master = form.querySelector("[data-check-all]");
    return (master && master.getAttribute("data-check-all")) || "submission_ids";
  }

  function sync(form) {
    var master = form.querySelector("[data-check-all]");
    var name = fieldName(form);
    var all = form.querySelectorAll('input[name="' + name + '"]');
    var checked = form.querySelectorAll(
      'input[name="' + name + '"]:checked'
    ).length;
    if (master) {
      master.checked = all.length > 0 && checked === all.length;
      master.indeterminate = checked > 0 && checked < all.length;
    }
    var count = form.querySelector("[data-pick-count]");
    if (count) {
      var empty = count.getAttribute("data-pick-empty") || "לא נבחרו הגשות";
      count.textContent = checked ? "נבחרו " + checked : empty;
    }
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLInputElement)) {
      return;
    }
    var form = target.closest("form");
    if (!form || !form.querySelector("[data-check-all]")) {
      return;
    }
    if (target.hasAttribute("data-check-all")) {
      var name = fieldName(form);
      form
        .querySelectorAll('input[name="' + name + '"]')
        .forEach(function (box) {
          box.checked = target.checked;
        });
    }
    sync(form);
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form [data-check-all]").forEach(function (master) {
      var form = master.closest("form");
      if (form) {
        sync(form);
      }
    });
  });
})();

/* Exam re-check: the model marks each open question with its own call, so one
   paper can take a minute or two. Instead of reloading the whole page every few
   seconds (which flickered and still looked frozen at "0 מתוך 1"), poll a small
   JSON status route and update the running card in place: which paper, which
   question, how long. When the run leaves "running" we reload once to show the
   results. Without JavaScript the template's <noscript> meta refresh still
   advances the coarse count. */
(function () {
  "use strict";

  function start() {
    var card = document.querySelector("[data-llm-status]");
    if (!card || typeof window.fetch !== "function") {
      return;
    }
    var url = card.getAttribute("data-llm-status");
    var submissionEl = card.querySelector("[data-llm-submission]");
    var nameEl = card.querySelector("[data-llm-name]");
    var questionEl = card.querySelector("[data-llm-question]");
    var barEl = card.querySelector("[data-llm-bar]");
    var countEl = card.querySelector("[data-llm-count]");
    var elapsedEl = card.querySelector("[data-llm-elapsed]");
    var stopped = false;

    function clock(sec) {
      var m = Math.floor(sec / 60);
      var s = sec % 60;
      return m + ":" + (s < 10 ? "0" : "") + s;
    }

    function paint(data) {
      var p = data.progress || {};
      var total = data.total || 0;
      var done = data.done || 0;
      var subTotal = p.submission_total || total;
      var subIndex = p.submission_index || Math.min(done + 1, subTotal || 1);
      if (submissionEl) {
        submissionEl.textContent =
          "הגשה " + subIndex + " מתוך " + (subTotal || "?");
      }
      if (nameEl) {
        nameEl.textContent = p.student_name ? "· " + p.student_name : "";
      }
      var qTotal = p.question_total || 0;
      var qIndex = p.question_index || 0;
      if (questionEl) {
        if (qTotal && p.question_label) {
          var label = p.question_label;
          if (label.length > 80) {
            label = label.slice(0, 79) + "…";
          }
          /* The last tick is the single model pass over all the open questions,
             so a per-question counter would be misleading there; show it only
             while the deterministic marking still advances question by question. */
          questionEl.textContent =
            qIndex < qTotal ? label + " · " + qIndex + "/" + qTotal : label;
        } else {
          questionEl.textContent = "המודל עובר על השאלות הפתוחות…";
        }
      }
      if (barEl) {
        /* Advance through the deterministic marking question by question, then
           hold near-complete during the single model pass over the open ones. */
        var within = qTotal ? Math.max(0, qIndex - 1) / qTotal : 0;
        var frac = total ? (done + within) / total : 0;
        var pct = Math.max(4, Math.min(99, Math.round(frac * 100)));
        barEl.style.width = pct + "%";
      }
      if (countEl) {
        countEl.textContent = done + " מתוך " + (total || "?") + " הגשות נבדקו";
      }
      if (elapsedEl && typeof p.elapsed_seconds === "number") {
        elapsedEl.textContent = "חלפו " + clock(p.elapsed_seconds);
      }
    }

    function tick() {
      if (stopped) {
        return;
      }
      window
        .fetch(url, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        })
        .then(function (res) {
          return res.ok ? res.json() : null;
        })
        .then(function (data) {
          if (stopped || !data) {
            return;
          }
          if (data.state !== "running") {
            stopped = true;
            window.location.reload();
            return;
          }
          paint(data);
        })
        .catch(function () {
          /* A transient network blip must not stop the watch. */
        })
        .then(function () {
          if (!stopped) {
            window.setTimeout(tick, 1500);
          }
        });
    }

    tick();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();

/* Student↔teacher chat, in the style of a support widget on a service site:
   - a floating bubble on a task page, whose messages are automatically tagged
     with the assignment the student is looking at;
   - the full chat tab (conversation list + open thread);
   - a live unread badge on the nav "צ'אט" link.
   One conversation is a (teacher, student) pair. The server renders the first
   paint; this module rebuilds the same bubble markup from the JSON thread on
   every poll, always with textContent so a message can never inject HTML. */
(function () {
  "use strict";

  if (typeof window.fetch !== "function") {
    return;
  }

  var THREAD_MS = 4000;
  var BADGE_MS = 20000;

  function setBadge(count) {
    var el = document.querySelector("[data-chat-badge]");
    if (!el) {
      return;
    }
    var n = Number(count) || 0;
    el.textContent = n > 99 ? "99+" : String(n);
    el.classList.toggle("is-hidden", n <= 0);
  }

  /* The server stores naive UTC; message_view sends it without a zone, so add
     the Z before parsing or the browser would read it as local time. */
  function localTime(iso) {
    if (!iso) {
      return "";
    }
    var text = iso;
    if (text.indexOf("Z") === -1 && text.indexOf("+") === -1) {
      text += "Z";
    }
    var d = new Date(text);
    if (isNaN(d.getTime())) {
      return "";
    }
    try {
      return d.toLocaleTimeString("he-IL", {
        hour: "2-digit",
        minute: "2-digit"
      });
    } catch (ignored) {
      return "";
    }
  }

  function renderThread(container, messages, forceScroll) {
    var nearBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight < 48;
    container.textContent = "";
    if (!messages || !messages.length) {
      var empty = document.createElement("p");
      empty.className = "chat-empty muted";
      empty.setAttribute("data-chat-empty", "");
      empty.textContent =
        "אין עדיין הודעות בשיחה הזו. כתבו את ההודעה הראשונה.";
      container.appendChild(empty);
      return;
    }
    messages.forEach(function (m) {
      var wrap = document.createElement("div");
      wrap.className =
        "chat-msg " + (m.mine ? "chat-msg-mine" : "chat-msg-theirs");
      if (m.context_label) {
        var ctx = document.createElement("div");
        ctx.className = "chat-msg-context";
        var tag = document.createElement("span");
        tag.className = "chat-context-tag";
        tag.textContent = "בהקשר: " + m.context_label;
        ctx.appendChild(tag);
        wrap.appendChild(ctx);
      }
      var body = document.createElement("div");
      body.className = "chat-msg-body";
      body.textContent = m.body;
      wrap.appendChild(body);
      var time = document.createElement("time");
      time.className = "chat-msg-time";
      time.setAttribute("data-iso", m.created_at || "");
      time.textContent = localTime(m.created_at);
      wrap.appendChild(time);
      container.appendChild(wrap);
    });
    /* Never yank a reader who scrolled up to re-read history; only follow the
       conversation when they were already at the bottom (or just sent). */
    if (forceScroll || nearBottom) {
      container.scrollTop = container.scrollHeight;
    }
  }

  function fetchJSON(url, options) {
    return window
      .fetch(url, options)
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (payload) {
            return { ok: response.ok, payload: payload };
          });
      });
  }

  /* A live thread: polls while active, exposes refresh() for right after a
     send. Every response also carries the viewer's total unread, so the nav
     badge stays current without a second request. */
  function makeThread(container, threadUrl, peerParam, peer, isActive) {
    var stopped = false;

    function load(forceScroll) {
      var url =
        threadUrl + "?" + peerParam + "=" + encodeURIComponent(peer);
      return fetchJSON(url, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store"
      })
        .then(function (res) {
          if (!res.ok || !res.payload || !res.payload.ok) {
            return;
          }
          renderThread(container, res.payload.messages, forceScroll);
          if (typeof res.payload.unread === "number") {
            setBadge(res.payload.unread);
          }
        })
        .catch(function () {
          /* A transient network blip must not stop the watch. */
        });
    }

    function tick() {
      if (stopped) {
        return;
      }
      if (isActive && !isActive()) {
        window.setTimeout(tick, THREAD_MS);
        return;
      }
      load(false).then(function () {
        if (!stopped) {
          window.setTimeout(tick, THREAD_MS);
        }
      });
    }

    tick();
    return {
      refresh: function () {
        return load(true);
      },
      stop: function () {
        stopped = true;
      }
    };
  }

  function bindCompose(form, onSent) {
    var input = form.querySelector("[data-chat-input]");
    var status = form.querySelector("[data-chat-status]");
    var sendUrl = form.getAttribute("data-send-url");
    var peerParam = form.getAttribute("data-peer-param");
    var peer = form.getAttribute("data-peer");
    var csrf = form.getAttribute("data-csrf");
    var assignment = form.getAttribute("data-assignment") || "";
    var busy = false;

    function show(text) {
      if (status) {
        status.textContent = text;
      }
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (busy || !peer || !input) {
        return;
      }
      var text = (input.value || "").trim();
      if (!text) {
        show("כתבו הודעה לפני השליחה.");
        return;
      }
      busy = true;
      show("שולח…");
      var fd = new FormData();
      fd.set(peerParam + "_id", peer);
      fd.set("body", text);
      fd.set("csrf_token", csrf);
      if (assignment) {
        fd.set("assignment_id", assignment);
      }
      fetchJSON(sendUrl, {
        method: "POST",
        body: fd,
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        cache: "no-store"
      })
        .then(function (res) {
          if (!res.ok || !res.payload || !res.payload.ok) {
            throw new Error(
              (res.payload && res.payload.error) || "השליחה נכשלה."
            );
          }
          input.value = "";
          show("");
          if (onSent) {
            onSent(res.payload);
          }
        })
        .catch(function (error) {
          show(error.message || "השליחה נכשלה. בדקו את החיבור.");
        })
        .then(function () {
          busy = false;
        });
    });

    /* Enter sends; Shift+Enter keeps a line break, like every chat widget. */
    if (input) {
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          if (typeof form.requestSubmit === "function") {
            form.requestSubmit();
          } else {
            form.dispatchEvent(
              new Event("submit", { cancelable: true, bubbles: true })
            );
          }
        }
      });
    }
  }

  function start() {
    /* The chat tab: server-rendered first paint, then live updates. */
    var page = document.querySelector("[data-chat-page]");
    if (page) {
      var container = page.querySelector("[data-chat-thread]");
      var peer = page.getAttribute("data-active");
      if (container && peer) {
        container.scrollTop = container.scrollHeight;
        var pageThread = makeThread(
          container,
          page.getAttribute("data-thread-url"),
          page.getAttribute("data-peer-param"),
          peer,
          function () {
            return !document.hidden;
          }
        );
        var pageForm = page.querySelector("[data-chat-compose]");
        if (pageForm) {
          bindCompose(pageForm, function () {
            pageThread.refresh();
          });
        }
      }
    }

    /* The task-page bubble: opens above the toggle, polls only while open. */
    document.querySelectorAll("[data-chat-bubble]").forEach(function (root) {
      var toggle = root.querySelector("[data-chat-toggle]");
      var panel = root.querySelector("[data-chat-panel]");
      var bubbleContainer = root.querySelector("[data-chat-thread]");
      var closeBtn = root.querySelector("[data-chat-close]");
      var teacherId = root.getAttribute("data-teacher");
      var threadUrl = root.getAttribute("data-thread-url");
      if (!toggle || !panel || !bubbleContainer || !teacherId) {
        return;
      }
      var open = false;
      var bubbleThread = null;

      function setOpen(value) {
        open = value;
        panel.hidden = !value;
        toggle.setAttribute("aria-expanded", value ? "true" : "false");
        if (value) {
          if (!bubbleThread) {
            bubbleThread = makeThread(
              bubbleContainer,
              threadUrl,
              "teacher",
              teacherId,
              function () {
                return open && !document.hidden;
              }
            );
          } else {
            bubbleThread.refresh();
          }
          var input = root.querySelector("[data-chat-input]");
          if (input) {
            input.focus();
          }
        }
      }

      toggle.addEventListener("click", function () {
        setOpen(!open);
      });
      if (closeBtn) {
        closeBtn.addEventListener("click", function () {
          setOpen(false);
          toggle.focus();
        });
      }
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && open) {
          setOpen(false);
          toggle.focus();
        }
      });
      var bubbleForm = root.querySelector("[data-chat-compose]");
      if (bubbleForm) {
        bindCompose(bubbleForm, function () {
          if (bubbleThread) {
            bubbleThread.refresh();
          }
        });
      }
    });

    /* The nav badge on every other page: a light poll of the unread count. */
    var badge = document.querySelector("[data-chat-badge]");
    var badgeUrl = badge ? badge.getAttribute("data-unread-url") : "";
    if (badgeUrl) {
      var pollBadge = function () {
        if (!document.hidden) {
          fetchJSON(badgeUrl, {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
            cache: "no-store"
          })
            .then(function (res) {
              if (res.ok && res.payload && typeof res.payload.unread === "number") {
                setBadge(res.payload.unread);
              }
            })
            .catch(function () {});
        }
        window.setTimeout(pollBadge, BADGE_MS);
      };
      window.setTimeout(pollBadge, BADGE_MS);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
