document.addEventListener('DOMContentLoaded', () => {
  const dialog = document.getElementById('project-delete-dialog');
  if (!dialog) return;

  const copy = document.getElementById('delete-dialog-copy');
  const error = document.getElementById('delete-dialog-error');
  const confirmButton = document.getElementById('confirm-project-delete');
  const cancelButton = document.getElementById('cancel-project-delete');
  let selectedProject = null;
  let cleanupNotice = false;

  document.querySelectorAll('[data-delete-project]').forEach((button) => {
    button.addEventListener('click', () => {
      selectedProject = {id: button.dataset.projectId, title: button.dataset.projectTitle || 'this project'};
      button.closest('details')?.removeAttribute('open');
      copy.textContent = `“${selectedProject.title}” and its queued work, generated scenes, and saved video will be permanently deleted.`;
      error.hidden = true;
      confirmButton.disabled = false;
      confirmButton.innerHTML = '<i class="fa-regular fa-trash-can" aria-hidden="true"></i> Delete project';
      cancelButton.textContent = 'Cancel';
      dialog.showModal();
    });
  });

  cancelButton.addEventListener('click', () => cleanupNotice ? window.location.reload() : dialog.close());
  confirmButton.addEventListener('click', async () => {
    if (cleanupNotice) { window.location.reload(); return; }
    if (!selectedProject || confirmButton.disabled) return;
    confirmButton.disabled = true;
    cancelButton.disabled = true;
    error.hidden = true;
    confirmButton.textContent = 'Deleting…';
    try {
      const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
      const response = await fetch(`/api/projects/${encodeURIComponent(selectedProject.id)}`, {
        method: 'DELETE',
        headers: {'Accept': 'application/json', 'X-CSRF-Token': csrf}
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || 'Could not delete this project. Please try again.');
      if (result.media_cleanup_pending) {
        cleanupNotice = true;
        copy.textContent = 'The project was deleted, but Supabase could not remove all of its saved media. Check the server log for cleanup details.';
        confirmButton.textContent = 'Done';
        confirmButton.disabled = false;
        confirmButton.innerHTML = 'Done';
        cancelButton.disabled = false;
        cancelButton.textContent = 'Close';
        return;
      }
      window.location.reload();
    } catch (requestError) {
      error.textContent = requestError.message;
      error.hidden = false;
      confirmButton.disabled = false;
      confirmButton.innerHTML = '<i class="fa-regular fa-trash-can" aria-hidden="true"></i> Try again';
      cancelButton.disabled = false;
    }
  });
});
