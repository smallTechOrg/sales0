// "Show password" checkbox on the login and set-password pages (a masked field is hard to check after pasting on a phone).
(function () {
  document.addEventListener("change", function (event) {
    var box = event.target;
    if (!box || !box.matches || !box.matches("input[data-show-password]")) return;
    var form = box.form;
    if (!form) return;
    form.querySelectorAll("input[data-pw]").forEach(function (field) {
      field.type = box.checked ? "text" : "password";
    });
  });
})();
