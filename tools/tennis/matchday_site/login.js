"use strict";
const form = document.getElementById("login");
const error = document.getElementById("error");
const submit = document.getElementById("submit");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.textContent = "";
  submit.disabled = true;
  try {
    const response = await fetch("/matchday/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: form.email.value, password: form.password.value }),
    });
    if (response.ok) {
      // A link to one match keeps its #matches/<id> through the login.
      location.assign(`/matchday/${/^#matches\/\d+$/.test(location.hash) ? location.hash : ""}`);
      return;
    }
    const answer = await response.json().catch(() => ({}));
    error.textContent = answer.error || "Не удалось войти";
  } catch {
    error.textContent = "Нет связи с сервером";
  }
  submit.disabled = false;
});
