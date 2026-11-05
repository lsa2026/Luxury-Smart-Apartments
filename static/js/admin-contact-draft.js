/* Refresh only an untouched composer while a requested SMA draft is pending. */
if (document.querySelector('[data-contact-draft-pending]')) {
  let edited = false;
  document.addEventListener("input", () => { edited = true; }, {once:true});
  document.addEventListener("change", () => { edited = true; }, {once:true});
  setTimeout(() => {
    const body = document.querySelector('#id_body');
    const notes = document.querySelector('#draft-instructions');
    if (!edited && body && !body.value.trim() && (!notes || !notes.value.trim())) window.location.reload();
  }, 8000);
}
