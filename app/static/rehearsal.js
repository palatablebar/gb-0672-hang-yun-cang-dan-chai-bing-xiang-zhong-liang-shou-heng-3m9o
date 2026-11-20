// 拆并预演：正式应用（补证确认 + 乐观版本校验，失败时后端已回滚）。
const applyBtn = document.querySelector('#apply-run');
if (applyBtn) {
  const message = document.querySelector('#apply-message');
  applyBtn.addEventListener('click', async () => {
    const acknowledged = Array.from(document.querySelectorAll('input.ack:checked')).map(cb => cb.value);
    applyBtn.disabled = true;
    message.className = 'apply-message';
    message.textContent = '正在正式应用…';
    try {
      const response = await fetch('/api/rehearsals/' + applyBtn.dataset.rehearsal + '/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: Number(applyBtn.dataset.revision), acknowledged_supplements: acknowledged })
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '应用失败');
      message.className = 'apply-message ok';
      message.innerHTML = '已生成新版本舱单 <b>' + data.target_run_id + '</b>，旧版关系保留。';
      window.location.href = '/archive?run_id=' + data.target_run_id;
    } catch (error) {
      message.className = 'apply-message err';
      message.textContent = '应用失败，已恢复旧版关系：' + error.message;
      applyBtn.disabled = false;
    }
  });
}
