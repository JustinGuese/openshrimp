(function () {
  'use strict';

  function tokenQS() {
    return (typeof DASHBOARD_TOKEN !== 'undefined' && DASHBOARD_TOKEN)
      ? '&token=' + encodeURIComponent(DASHBOARD_TOKEN)
      : '';
  }

  function urlWithToken(path, hasQuery) {
    var sep = hasQuery ? '&' : '?';
    return path + ((typeof DASHBOARD_TOKEN !== 'undefined' && DASHBOARD_TOKEN)
      ? sep + 'token=' + encodeURIComponent(DASHBOARD_TOKEN)
      : '');
  }

  const projectSelect = document.getElementById('project-select');
  const editTaskModal = document.getElementById('editTaskModal');
  const editTaskId = document.getElementById('edit-task-id');
  const editTitle = document.getElementById('edit-title');
  const editDescription = document.getElementById('edit-description');
  const editPriority = document.getElementById('edit-priority');
  const editAssignee = document.getElementById('edit-assignee');
  const editSaveBtn = document.getElementById('edit-save');
  const editDeleteBtn = document.getElementById('edit-delete');

  if (projectSelect) {
    projectSelect.addEventListener('change', function () {
      const id = this.value;
      var params = [];
      if (id) params.push('project_id=' + encodeURIComponent(id));
      if (typeof DASHBOARD_TOKEN !== 'undefined' && DASHBOARD_TOKEN) {
        params.push('token=' + encodeURIComponent(DASHBOARD_TOKEN));
      }
      window.location.href = '/' + (params.length ? '?' + params.join('&') : '');
    });
  }

  function getTaskById(id) {
    const n = Number(id);
    return TASKS_JSON.find(function (t) { return t.id === n; });
  }

  function patchTask(taskId, body) {
    return fetch(urlWithToken('/tasks/' + taskId, false), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) throw new Error('PATCH failed');
      return r.json();
    });
  }

  function deleteTask(taskId) {
    return fetch(urlWithToken('/tasks/' + taskId, false), { method: 'DELETE' }).then(function (r) {
      if (!r.ok) throw new Error('Delete failed');
    });
  }

  function openEditModal(taskId) {
    const task = getTaskById(taskId);
    if (!task) return;
    editTaskId.value = taskId;
    editTitle.value = task.title || '';
    editDescription.value = task.description || '';
    editPriority.value = task.priority || 'medium';
    editAssignee.value = task.assignee_id != null ? String(task.assignee_id) : '';
    new bootstrap.Modal(editTaskModal).show();
  }

  document.querySelectorAll('.task-card').forEach(function (card) {
    card.addEventListener('click', function (e) {
      if (e.target.closest('button')) return;
      openEditModal(this.getAttribute('data-task-id'));
    });
    card.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openEditModal(this.getAttribute('data-task-id'));
      }
    });
  });

  document.querySelectorAll('.edit-task').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      openEditModal(this.getAttribute('data-task-id'));
    });
  });

  if (editSaveBtn) {
    editSaveBtn.addEventListener('click', function () {
      const id = editTaskId.value;
      if (!id) return;
      const body = {
        title: editTitle.value,
        description: editDescription.value,
        priority: editPriority.value,
        assignee_id: editAssignee.value ? parseInt(editAssignee.value, 10) : null
      };
      editSaveBtn.disabled = true;
      patchTask(id, body)
        .then(function () { window.location.reload(); })
        .catch(function () { alert('Failed to update task'); })
        .finally(function () { editSaveBtn.disabled = false; });
    });
  }

  function refreshPage() {
    window.location.href = window.location.pathname + (window.location.search || '');
  }

  if (editDeleteBtn) {
    editDeleteBtn.addEventListener('click', function () {
      const id = editTaskId.value;
      if (!id) return;
      if (!confirm('Delete this task?')) return;
      editDeleteBtn.disabled = true;
      deleteTask(id)
        .then(function () {
          refreshPage();
        })
        .catch(function () {
          alert('Failed to delete task');
          editDeleteBtn.disabled = false;
        });
    });
  }

  var sortables = [];
  document.querySelectorAll('.board-column-cards').forEach(function (el) {
    var status = el.getAttribute('data-status');
    var s = Sortable.create(el, {
      group: 'tasks',
      animation: 150,
      onEnd: function (evt) {
        var taskId = evt.item.getAttribute('data-task-id');
        var newStatus = evt.to.getAttribute('data-status');
        if (!taskId || !newStatus) return;
        patchTask(taskId, { status: newStatus })
          .then(function () { window.location.reload(); })
          .catch(function () { evt.from.appendChild(evt.item); });
      }
    });
    sortables.push(s);
  });

  if ($.fn.DataTable && document.getElementById('tasks-table')) {
    $('#tasks-table').DataTable({
      order: [[5, 'desc']],
      pageLength: 25
    });
  }
})();
