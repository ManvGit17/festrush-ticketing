(function () {
  "use strict";
  var map = document.getElementById("seatmap");
  function cls(c) { return c.replace("_", " "); }
  function draw(s) {
    map.textContent = "";
    s.cells.forEach(function (c) {
      var el = document.createElement("span");
      el.className = "seat " + cls(c);
      el.setAttribute("role", "listitem");
      el.setAttribute("aria-label", cls(c));
      map.appendChild(el);
    });
    document.getElementById("summary").textContent = s.free + " of " + s.capacity + " seats free.";
  }
  if (map) {
    var id = map.dataset.event;
    if (window.EventSource) {
      new EventSource("/live/events/" + id + "/stream").onmessage = function (e) { draw(JSON.parse(e.data)); };
    }
    var hold = document.getElementById("hold");
    if (hold) hold.addEventListener("click", function () {
      var body = new URLSearchParams({ csrf_token: map.dataset.csrf });
      fetch("/register_event/" + id, { method: "POST", body: body, credentials: "same-origin" })
        .then(function () { return fetch("/live/events/" + id + "/seats", { credentials: "same-origin" }); })
        .then(function (r) { return r.json(); })
        .then(function (s) {
          draw(s);
          document.getElementById("msg").textContent = s.cells.indexOf("mine_held") >= 0
            ? "Seat held. Check out to confirm it."
            : "Could not hold a seat (sold out, or you already hold one).";
        });
    });
  }

  // System Lens: a drawer on every page for signed-in users.
  var toggle = document.getElementById("lens-toggle"), panel = document.getElementById("lens");
  if (!toggle || !panel) return;
  var timer = null;
  function poll() {
    fetch("/live/lens", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
      var list = panel.querySelector("dl");
      list.textContent = "";
      Object.keys(d).forEach(function (k) {
        var dt = document.createElement("dt"), dd = document.createElement("dd");
        dt.textContent = k.replace(/_/g, " ");
        dd.textContent = String(Math.round(d[k] * 10) / 10);
        list.appendChild(dt);
        list.appendChild(dd);
      });
    });
  }
  toggle.addEventListener("click", function () {
    var open = panel.hidden;
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    clearInterval(timer);
    if (open) { poll(); timer = setInterval(poll, 2000); }
  });
})();
