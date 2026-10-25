(() => {
  function themeLabel() {
    const dark = document.documentElement.dataset.theme === 'dark';
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      button.setAttribute('aria-label', dark ? 'تغییر به حالت روز' : 'تغییر به حالت شب');
      button.setAttribute('aria-pressed', String(dark));
    });
  }
  document.querySelectorAll('[data-theme-toggle]').forEach(button => button.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('kamiwa-theme', next); } catch (_) {}
    themeLabel();
  }));
  themeLabel();
  const filterPanel = document.querySelector('[data-responsive-filter]');
  if (filterPanel) {
    const desktop = window.matchMedia('(min-width: 761px)');
    const syncFilter = () => { filterPanel.open = desktop.matches; };
    desktop.addEventListener('change', syncFilter);
    syncFilter();
  }
  // Account type comes from a <select id="id_role"> or from radio cards named "role".
  const roleSelect = document.getElementById('id_role');
  const roleRadios = document.querySelectorAll('input[type=radio][name=role]');
  const currentRole = () => roleSelect ? roleSelect.value : ((document.querySelector('input[type=radio][name=role]:checked') || {}).value || 'buyer');
  function roleFields() {
    if (!roleSelect && !roleRadios.length) return;
    const value = currentRole();
    document.querySelectorAll('[data-roles]').forEach(section => {
      const visible = section.dataset.roles.split(' ').includes(value);
      section.hidden = !visible;
      section.querySelectorAll('input,select,textarea').forEach(field => field.disabled = !visible);
    });
    document.dispatchEvent(new CustomEvent('kamiwa:role'));
  }
  if (roleSelect) roleSelect.addEventListener('change', roleFields);
  roleRadios.forEach(radio => radio.addEventListener('change', roleFields));
  roleFields();

  // Step-by-step forms: [data-wizard] with .wizard-step sections; without JS every step stays visible.
  const wizard = document.querySelector('form[data-wizard]');
  if (wizard) {
    const steps = [...wizard.querySelectorAll('.wizard-step')];
    const tabs = [...wizard.querySelectorAll('[data-step-tab]')];
    const prev = wizard.querySelector('[data-wizard-prev]'), next = wizard.querySelector('[data-wizard-next]'), submit = wizard.querySelector('[data-wizard-submit]');
    const allowed = node => !node.dataset.stepRoles || node.dataset.stepRoles.split(' ').includes(currentRole());
    const active = () => steps.filter(allowed);
    let index = 0;
    const show = (target, focus) => {
      const list = active();
      index = Math.max(0, Math.min(target, list.length - 1));
      steps.forEach(step => { step.hidden = step !== list[index]; });
      tabs.forEach(tab => {
        tab.hidden = !allowed(tab);
        const position = list.findIndex(step => step.dataset.step === tab.dataset.stepTab);
        tab.classList.toggle('is-done', position > -1 && position < index);
        if (position === index) tab.setAttribute('aria-current', 'step'); else tab.removeAttribute('aria-current');
      });
      prev.hidden = index === 0;
      next.hidden = index === list.length - 1;
      submit.hidden = index !== list.length - 1;
      if (focus) { const heading = list[index].querySelector('h2'); if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus(); } }
    };
    const missing = step => {
      let first = null;
      step.querySelectorAll('input,select,textarea').forEach(field => {
        if (field.disabled || field.type === 'hidden' || field.closest('[hidden]')) return;
        const box = field.closest('.field');
        const empty = field.required && (field.type === 'file' ? !field.files.length : !String(field.value).trim());
        const bad = empty || field.getAttribute('aria-invalid') === 'true';
        if (box) {
          let note = box.querySelector('.step-required');
          if (empty && !note) { note = document.createElement('p'); note.className = 'field-errors step-required'; note.setAttribute('role', 'alert'); note.textContent = 'این مورد الزامی است.'; box.appendChild(note); }
          if (!empty && note) note.remove();
          box.classList.toggle('field-error', bad);
        }
        if (bad && !first) first = field;
      });
      return first;
    };
    next.addEventListener('click', () => { const bad = missing(active()[index]); if (bad) { bad.focus(); return; } show(index + 1, true); window.scrollTo({top: wizard.offsetTop - 100, behavior: 'smooth'}); });
    prev.addEventListener('click', () => { show(index - 1, true); window.scrollTo({top: wizard.offsetTop - 100, behavior: 'smooth'}); });
    wizard.addEventListener('keydown', event => {
      if (event.key === 'Enter' && event.target.tagName === 'INPUT' && !next.hidden) { event.preventDefault(); next.click(); }
    });
    document.addEventListener('kamiwa:role', () => show(index));
    // After a server-side error, open the first step that holds one.
    const errored = active().findIndex(step => step.querySelector('.field-error, .field-errors'));
    show(errored > -1 ? errored : 0);
  }
  document.querySelectorAll('[data-autosubmit]').forEach(field => field.addEventListener('change', () => field.form.requestSubmit()));
  document.querySelectorAll('[data-gallery-image]').forEach(button => button.addEventListener('click', () => {
    const target = document.getElementById('detail-image');
    if (target) { target.src = button.dataset.galleryImage; document.querySelectorAll('[data-gallery-image]').forEach(other => other.setAttribute('aria-pressed', String(other === button))); }
  }));
  const announcer = document.createElement('div');
  announcer.className = 'sr-only';
  announcer.setAttribute('role', 'status');
  announcer.setAttribute('aria-live', 'polite');
  document.body.appendChild(announcer);
  const announce = text => { announcer.textContent = ''; window.setTimeout(() => { announcer.textContent = text; }, 50); };

  function hint(input) {
    let node = document.getElementById(input.id + '_live');
    if (!node) {
      node = document.createElement('p');
      node.id = input.id + '_live';
      node.className = 'live-hint';
      node.setAttribute('aria-live', 'polite');
      (input.closest('.password-wrap') || input).after(node);
      input.setAttribute('aria-describedby', [input.getAttribute('aria-describedby'), node.id].filter(Boolean).join(' '));
    }
    return node;
  }
  function setHint(input, ok, text) {
    const node = hint(input);
    node.textContent = text;
    node.classList.toggle('is-ok', ok === true);
    node.classList.toggle('is-error', ok === false);
    if (ok === false) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
  }

  document.querySelectorAll('input[type=password]').forEach(input => {
    const wrap = document.createElement('div');
    wrap.className = 'password-wrap';
    input.before(wrap);
    wrap.appendChild(input);
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'password-toggle';
    toggle.textContent = 'نمایش';
    toggle.setAttribute('aria-label', 'نمایش رمز');
    toggle.setAttribute('aria-pressed', 'false');
    toggle.addEventListener('click', () => {
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      toggle.textContent = show ? 'پنهان' : 'نمایش';
      toggle.setAttribute('aria-label', show ? 'پنهان کردن رمز' : 'نمایش رمز');
      toggle.setAttribute('aria-pressed', String(show));
      input.focus();
    });
    wrap.appendChild(toggle);
  });

  const toAscii = value => value.replace(/[۰-۹]/g, d => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(d))).replace(/[٠-٩]/g, d => String('٠١٢٣٤٥٦٧٨٩'.indexOf(d)));
  document.querySelectorAll('[data-digits-only]').forEach(input => input.addEventListener('input', () => {
    const max = Number(input.getAttribute('maxlength')) || 32;
    const clean = toAscii(input.value).replace(/\D/g, '').slice(0, max);
    if (clean !== input.value) input.value = clean;
  }));

  const usernameInput = document.querySelector('[data-username-check]');
  if (usernameInput && usernameInput.form && usernameInput.form.dataset.usernameUrl) {
    let timer, latest = 0;
    usernameInput.addEventListener('input', () => {
      window.clearTimeout(timer);
      const value = usernameInput.value.trim();
      if (!value) { setHint(usernameInput, null, ''); return; }
      if (!/^[A-Za-z]+$/.test(value)) { setHint(usernameInput, false, 'فقط حروف انگلیسی (بدون عدد، فاصله یا نماد) مجاز است.'); return; }
      setHint(usernameInput, null, 'در حال بررسی…');
      timer = window.setTimeout(async () => {
        const request = ++latest;
        try {
          const response = await fetch(usernameInput.form.dataset.usernameUrl + '?username=' + encodeURIComponent(value), {headers: {'X-Requested-With': 'fetch'}});
          const data = await response.json();
          if (request === latest) setHint(usernameInput, data.ok, data.message);
        } catch (_) { if (request === latest) setHint(usernameInput, null, ''); }
      }, 400);
    });
  }

  const passwordInput = document.querySelector('[data-password-rules]');
  if (passwordInput) {
    const rules = [
      ['length', 'حداقل ۸ نویسه', v => v.length >= 8 && v.length <= 128],
      ['upper', 'یک حرف بزرگ انگلیسی', v => /[A-Z]/.test(v)],
      ['lower', 'یک حرف کوچک انگلیسی', v => /[a-z]/.test(v)],
      ['digit', 'یک عدد', v => /[0-9]/.test(v)],
      ['user', 'شبیه نام کاربری نباشد', v => { const u = (usernameInput && usernameInput.value.trim().toLowerCase()) || ''; return !v || u.length < 3 || !v.toLowerCase().includes(u); }],
    ];
    const list = document.createElement('ul');
    list.className = 'password-rules';
    list.id = passwordInput.id + '_rules';
    rules.forEach(([key, label]) => { const item = document.createElement('li'); item.dataset.rule = key; item.textContent = label; list.appendChild(item); });
    passwordInput.closest('.password-wrap').after(list);
    const staticHelp = document.getElementById(passwordInput.id + '_helptext');
    if (staticHelp) staticHelp.hidden = true;
    passwordInput.setAttribute('aria-describedby', [passwordInput.getAttribute('aria-describedby'), list.id].filter(Boolean).join(' '));
    const check = () => {
      const value = passwordInput.value;
      let all = true;
      rules.forEach(([key, label, test]) => {
        const ok = test(value);
        all = all && ok;
        const item = list.querySelector(`[data-rule="${key}"]`);
        item.classList.toggle('is-ok', Boolean(value) && ok);
        item.classList.toggle('is-error', Boolean(value) && !ok);
      });
      if (value) passwordInput.toggleAttribute('aria-invalid', !all); else passwordInput.removeAttribute('aria-invalid');
    };
    passwordInput.addEventListener('input', check);
    if (usernameInput) usernameInput.addEventListener('input', check);
    const confirm = document.querySelector('[data-password-match]');
    if (confirm) {
      const match = () => {
        if (!confirm.value) { setHint(confirm, null, ''); return; }
        const same = confirm.value === passwordInput.value;
        setHint(confirm, same, same ? 'تکرار رمز درست است.' : 'تکرار رمز با رمز عبور یکی نیست.');
      };
      confirm.addEventListener('input', match);
      passwordInput.addEventListener('input', () => { if (confirm.value) match(); });
    }
  }

  async function toggleFavorite(form) {
    const button = form.querySelector('button');
    const title = button.dataset.title || '';
    button.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form), headers: {'X-Requested-With': 'fetch'}, credentials: 'same-origin'});
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      button.classList.toggle('is-active', data.favorited);
      button.setAttribute('aria-pressed', String(data.favorited));
      const text = button.querySelector('[data-favorite-text]');
      if (text) text.textContent = data.favorited ? 'ذخیره‌شده' : 'ذخیره آگهی';
      else button.setAttribute('aria-label', (data.favorited ? 'حذف از' : 'افزودن به') + ' علاقه‌مندی‌ها' + (title ? ': ' + title : ''));
      announce(data.message);
    } catch (_) {
      form.submit();
    } finally {
      button.removeAttribute('aria-busy');
    }
  }

  document.addEventListener('submit', event => {
    const form = event.target;
    if (form.classList.contains('favorite-form') && window.fetch) { event.preventDefault(); toggleFavorite(form); return; }
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) { event.preventDefault(); return; }
    if (form.method.toLowerCase() !== 'post') return;
    form.setAttribute('aria-busy', 'true');
    const button = event.submitter;
    if (button) { button.classList.add('is-loading'); button.setAttribute('aria-disabled', 'true'); }
  });
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('[aria-busy]').forEach(form => form.removeAttribute('aria-busy'));
    document.querySelectorAll('.is-loading').forEach(button => { button.classList.remove('is-loading'); button.removeAttribute('aria-disabled'); });
  });
})();

/* v3 motion: reveal on scroll and counting numbers (skipped under reduced motion). */
(() => {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const revealed = document.querySelectorAll('[data-reveal]');
  if (reduce || !('IntersectionObserver' in window)) { revealed.forEach(node => node.classList.add('is-visible')); }
  else {
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) { entry.target.classList.add('is-visible'); observer.unobserve(entry.target); }
    }), {rootMargin: '0px 0px -8% 0px', threshold: 0.12});
    revealed.forEach((node, index) => { node.style.transitionDelay = Math.min(index % 4, 3) * 80 + 'ms'; observer.observe(node); });
    // Never leave content hidden if the observer is throttled (background tab, odd layouts).
    window.setTimeout(() => revealed.forEach(node => node.classList.add('is-visible')), 1500);
  }
  const format = new Intl.NumberFormat('fa-IR');
  const counters = document.querySelectorAll('[data-count]');
  if (reduce || !('IntersectionObserver' in window)) return;
  const countObserver = new IntersectionObserver(entries => entries.forEach(entry => {
    if (!entry.isIntersecting) return;
    countObserver.unobserve(entry.target);
    const node = entry.target, target = Number(node.dataset.count) || 0, start = performance.now(), duration = 1200;
    const step = now => {
      const progress = Math.min(1, (now - start) / duration), eased = 1 - Math.pow(1 - progress, 3);
      node.textContent = format.format(Math.round(target * eased));
      if (progress < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }), {threshold: 0.4});
  counters.forEach(node => countObserver.observe(node));
})();
